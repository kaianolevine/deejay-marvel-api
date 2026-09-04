"""GET /v1/identity/whoami — what this enforcement point sees about the caller.

Authorization is normally invisible: a caller gets a 200 or a 403 and has no
way to find out which of the four contract steps produced it. That is fine in
production and miserable when wiring a new principal, because the one string
you need — the subject Clerk actually reports — is the one string nothing
shows you.

This endpoint runs verify and resolve and reports both, without authorizing
anything. It is the tool for answering "what do I put in the principal row",
and afterwards for "why is this caller getting a 403".

It is deliberately NOT scope-guarded. Any caller holding a valid credential
can see their own identity and nothing else — no other principal, no roles
they do not hold, no store contents. Requiring a scope to discover your own
identity would make it useless for the case it exists to serve: a caller that
has no principal yet.

Read-only, and the only place a caller can inspect its own identity. Machine
principals come from ``identity_registry.MACHINES`` at boot, so nothing here
creates or changes one.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header
from sqlalchemy.ext.asyncio import AsyncSession

from .. import auth
from ..auth import ENFORCEMENT_POINT, resolve_principal
from ..config import Settings, get_settings
from ..database import get_db_session
from ..schemas import WhoamiOut

router = APIRouter()


@router.get(
    "/identity/whoami",
    summary="Report the caller's verified subject and resolved principal",
    description=(
        "Diagnostic. Runs verify and resolve and reports what this "
        "enforcement point sees, without making an authorization decision. "
        "`subject` is the exact string to seed into identity_principals; "
        "`principal` is null when this ecosystem has no row for that subject "
        "yet, which is the expected state for a newly created Clerk machine."
    ),
    response_model=WhoamiOut,
)
async def whoami(
    authorization: str | None = Header(default=None, alias="Authorization"),
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    """Report what verify and resolve saw about the calling credential.

    Runs the first two steps of the four-function contract and returns
    both, without authorizing anything — the endpoint is a mirror, not a
    guard. ``verified.subject`` is the string to seed into
    identity_principals; ``principal`` is null until that row exists.

    Deliberately not scope-guarded: a caller with no principal yet is
    exactly who needs this, and requiring a scope to discover your own
    identity would make it useless for that case.
    """
    # Module-qualified so the call resolves at request time, not import time.
    subject = await auth.verify_bearer(authorization, settings)
    principal = await resolve_principal(subject, session)

    return {
        "enforcement_point": ENFORCEMENT_POINT,
        "verified": {
            "issuer": subject.issuer,
            "subject": subject.subject,
            "kind": subject.kind,
        },
        "principal": (
            None
            if principal is None
            else {
                "id": str(principal.id),
                "kind": principal.kind,
                "display_name": principal.display_name,
                "status": principal.status,
                "roles": list(principal.roles),
            }
        ),
        "hint": (
            "No principal for this subject yet. Seed it with the `subject` value above."
            if principal is None
            else "Resolved."
        ),
    }
