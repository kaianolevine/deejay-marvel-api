"""Authentication and authorization — a conformant identity enforcement point.

This module used to carry its own Clerk verification: JWKS fetch and cache,
RS256 decode, M2M opaque verification via Clerk BAPI. Its own docstring said
what to do when a second service needed the same logic —

    "If a second service ever needs to verify Clerk tokens, upstream this
     logic ... and convert this module into a thin consumer rather than
     copying it."

A second enforcement point now exists, so that is what happened. Verification
lives in ``identity.clerk``; the decision lives in ``identity.policy``; the
store lives in ``identity.store``. What remains here is what the contract says
should remain in a service: configuration, and thin FastAPI adapters.

Two paths coexist during migration, deliberately:

  * The legacy dependencies (``get_current_owner``, ``get_current_caller``,
    ``require_wcs_admin``, ``require_wcs_service``) keep their exact return
    contracts, so the nine routers that depend on them are unchanged. They now
    verify through the identity binding rather than through inline code, so
    there is one verification implementation, not two.

  * ``require_scope(...)`` is the identity-native path: verify, resolve,
    authorize, emit audit — all four functions, per request. New endpoints
    should use it; existing ones move over one at a time.

One behavioural change worth naming: ``require_wcs_service`` previously
accepted only M2M *opaque* tokens and rejected M2M *JWTs*, because the
opaque/JWT split was standing in for machine/human. The identity binding
classifies by subject instead, so an M2M JWT is now correctly treated as a
machine caller. This is the intent the old code was approximating.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from functools import lru_cache

from fastapi import Depends, Header, Request
from identity.apikey import ApiKeyVerifier
from identity.chain import ChainVerifier
from identity.clerk import ClerkIssuer, ClerkVerifier
from identity.errors import CredentialInvalid, IdentityError, IssuerNotTrusted
from identity.policy import authorize as decide
from identity.store import (
    SqlAlchemyAuditSink,
    SqlAlchemyPrincipalStore,
    new_audit_event,
)
from identity.types import Principal, PrincipalKind, VerifiedSubject
from mini_app_polis.logger import (
    LOG_WARNING,
    get_logger,
    with_log_prefix,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from . import identity_registry as registry
from .config import Settings, get_settings
from .database import get_db_session
from .models import WcsUserProfile
from .schemas import api_error

logger = get_logger()

ENFORCEMENT_POINT = "api-kaianolevine-com"


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def _issuers_from_settings(settings: Settings) -> list[ClerkIssuer]:
    """Trusted Clerk issuers.

    Multi-issuer via ``CLERK_ISSUERS`` (a JSON array of
    ``{issuer, jwks_url, secret_key}``); the singular ``CLERK_ISSUER`` /
    ``CLERK_JWKS_URL`` / ``CLERK_SECRET_KEY`` vars remain the one-tenant
    shorthand this service deploys with.

    ``secret_key`` is what lets Clerk verify an opaque M2M token, and it is
    still required: cogs that have not yet been given their own API key
    authenticate that way. Dropping it 401s every one of them.
    """
    raw = getattr(settings, "CLERK_ISSUERS", None)
    if raw:
        entries = json.loads(raw) if isinstance(raw, str) else raw
        return [
            ClerkIssuer(
                issuer=e["issuer"],
                jwks_url=e["jwks_url"],
                secret_key=e.get("secret_key"),
            )
            for e in entries
        ]
    if settings.CLERK_ISSUER and settings.CLERK_JWKS_URL:
        return [
            ClerkIssuer(
                issuer=settings.CLERK_ISSUER,
                jwks_url=settings.CLERK_JWKS_URL,
                secret_key=settings.CLERK_SECRET_KEY,
            )
        ]
    return []


def build_verifier(settings: Settings, env: Mapping[str, str]) -> ChainVerifier:
    """Assemble the verifier for both populations.

    Machines authenticate with named keys read from configuration; humans with
    Clerk sessions. Routing between them is structural, so machine traffic
    never produces a failed Clerk verification and never leaves the process.
    """
    issuers = _issuers_from_settings(settings)
    return ChainVerifier(
        ApiKeyVerifier(registry.machine_keys(env)),
        ClerkVerifier(issuers) if issuers else None,
    )


@lru_cache(maxsize=1)
def _cached_verifier(cache_key: str) -> ChainVerifier:
    """Cached across requests: the Clerk JWKS cache lives inside it."""
    del cache_key
    return build_verifier(get_settings(), os.environ)


def get_verifier(settings: Settings | None = None) -> ChainVerifier:
    """Return the process verifier, rebuilt when configuration changes."""
    settings = settings or get_settings()
    issuers = _issuers_from_settings(settings)
    # Keyed on configuration identity only. Enumerating machine keys here
    # would re-read the environment — and re-log its warnings — on every
    # request; the declaration and the environment both change only on deploy.
    key = json.dumps(
        {
            "issuers": [[i.issuer, i.jwks_url, bool(i.secret_key)] for i in issuers],
            "machines": [m.name for m in registry.MACHINES],
        },
        sort_keys=True,
    )
    return _cached_verifier(key)


# ---------------------------------------------------------------------------
# Step 1 — verify
# ---------------------------------------------------------------------------


async def verify_bearer(
    authorization: str | None, settings: Settings
) -> VerifiedSubject:
    """Verify the Authorization header. Raises 401 on any failure.

    The failure reason is deliberately not returned to the caller — they are
    unauthenticated by definition at this point. It is logged, and where a
    scope is in play it is recorded on the audit event.
    """
    if authorization:
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() == "bearer" and token:
            try:
                return await get_verifier(settings).verify(token)
            except IssuerNotTrusted as exc:
                logger.warning(with_log_prefix(LOG_WARNING, f"untrusted issuer: {exc}"))
            except (CredentialInvalid, IdentityError) as exc:
                logger.warning(
                    with_log_prefix(LOG_WARNING, f"credential rejected: {exc!r}")
                )

    raise api_error(401, "unauthorized", "Valid Bearer token required")


# ---------------------------------------------------------------------------
# Legacy dependencies — unchanged contracts, identity-backed internals
# ---------------------------------------------------------------------------


async def get_current_owner(
    authorization: str | None = Header(default=None, alias="Authorization"),
    settings: Settings = Depends(get_settings),
) -> str:
    """Return the issuer subject for the authenticated caller.

    Still the Clerk ``sub``, not the principal id, on purpose: existing rows
    across this database store ``owner_id`` as the Clerk subject, and changing
    what this returns would silently orphan them. Re-keying to principal ids
    is a data migration, not a refactor.
    """
    subject = await verify_bearer(authorization, settings)
    return subject.subject


async def get_current_caller(
    authorization: str | None = Header(default=None, alias="Authorization"),
    settings: Settings = Depends(get_settings),
) -> tuple[str, PrincipalKind]:
    """Return ``(subject, kind)`` where kind is ``human`` or ``machine``."""
    subject = await verify_bearer(authorization, settings)
    return subject.subject, subject.kind


async def require_wcs_admin(
    owner_id: str = Depends(get_current_owner),
    session: AsyncSession = Depends(get_db_session),
) -> str:
    """Ensure the caller is a WCS admin.

    Still reads ``wcs_user_profiles.is_admin`` rather than resolving a
    principal. Migration 023 backfills those same humans into the principal
    store with a ``wcs-admin`` role, so this dependency can move to
    ``require_scope("wcs.notes.write")`` once the store is populated in
    production — but flipping it before then would lock out every admin.
    """
    result = await session.execute(
        select(WcsUserProfile).where(WcsUserProfile.user_id == owner_id)
    )
    profile = result.scalars().first()
    if profile is None or not profile.is_admin:
        raise api_error(403, "forbidden", "WCS admin access required")
    return owner_id


async def require_wcs_service(
    caller: tuple[str, PrincipalKind] = Depends(get_current_caller),
) -> str:
    """Ensure the caller is a machine (a cog), not a human session."""
    subject, kind = caller
    if kind != "machine":
        raise api_error(403, "forbidden", "WCS service (cog) caller required")
    return subject


# ---------------------------------------------------------------------------
# The identity-native path — all four contract functions
# ---------------------------------------------------------------------------


async def resolve_principal(
    subject: VerifiedSubject, session: AsyncSession
) -> Principal | None:
    """Look the verified subject up in this ecosystem's principal store.

    Machines are created by ``identity_registry.reconcile`` at boot, so a
    machine that resolves to nothing is a configuration problem — its key
    authenticated but the declaration never made it a principal — rather than
    a caller that has yet to introduce itself. There is no provisioning here.
    """
    store = SqlAlchemyPrincipalStore(session, enforcement_point=ENFORCEMENT_POINT)
    return await store.resolve(subject)


def require_scope(scope: str, *, resource_param: str | None = None):
    """Build a FastAPI dependency enforcing one scope.

    Runs the full contract: verify, resolve, authorize, emit audit. The audit
    event is written for allow and deny alike — a trail that records only
    denials cannot answer who did the thing.

    ``resource_param`` names a path parameter to use as the instance-level
    resource, for endpoints where an explicit grant can apply.
    """

    async def _dependency(
        request: Request,
        authorization: str | None = Header(default=None, alias="Authorization"),
        settings: Settings = Depends(get_settings),
        session: AsyncSession = Depends(get_db_session),
    ) -> Principal:
        subject = await verify_bearer(authorization, settings)

        store = SqlAlchemyPrincipalStore(session, enforcement_point=ENFORCEMENT_POINT)
        sink = SqlAlchemyAuditSink(session)

        principal = await store.resolve(subject)
        roles = await store.load_roles()
        resource = (
            str(request.path_params.get(resource_param))
            if resource_param and resource_param in request.path_params
            else None
        )
        grants = (
            await store.load_explicit_grants(principal.id)
            if principal is not None and resource is not None
            else set()
        )

        decision = decide(
            principal, scope, roles, resource=resource, explicit_grants=grants or None
        )

        await sink.emit_audit(
            new_audit_event(
                enforcement_point=ENFORCEMENT_POINT,
                scope=scope,
                allowed=decision.allowed,
                reason=decision.reason,
                principal=principal,
                subject=subject,
                resource=resource,
                request_id=request.headers.get("X-Request-Id"),
            )
        )

        if not decision.allowed:
            # An unknown-but-validly-authenticated caller is a 403, not a 401:
            # the credential was good, the ecosystem simply does not know them.
            raise api_error(403, "forbidden", f"Scope {scope} required")

        assert principal is not None  # noqa: S101 - guaranteed by decision.allowed
        return principal

    return _dependency
