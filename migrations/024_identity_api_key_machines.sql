-- Migration 024: named API keys for machines
--
-- Machines stop authenticating through Clerk and hold their own named key,
-- kept in deployment configuration. The key identifies the machine, so a
-- machine's principal can exist before it ever calls: `identity_registry`
-- creates it at boot from the declaration.
--
-- No key material is stored here, or anywhere in this schema. The store holds
-- names, status and roles; configuration holds what proves them. A database
-- dump therefore exposes no credentials, and rotating a key is a config
-- change rather than a migration.
--
-- Humans are unaffected and keep authenticating through Clerk.

-- A key-based issuer has no key set to fetch: verification is a local
-- comparison, not a signature check.
ALTER TABLE identity_issuers ALTER COLUMN jwks_url DROP NOT NULL;

INSERT INTO identity_issuers (issuer, display_name, jwks_url)
VALUES ('apikey', 'Named machine API keys (configuration)', NULL)
ON CONFLICT (issuer) DO NOTHING;
