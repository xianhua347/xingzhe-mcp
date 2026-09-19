-- Private server-side state. Never add this schema to Supabase's exposed schemas.
CREATE SCHEMA xingzhe;
REVOKE ALL ON SCHEMA xingzhe FROM PUBLIC;

CREATE TABLE xingzhe.records (
    kind text NOT NULL CHECK (kind IN ('connection', 'state', 'consent', 'code', 'access', 'refresh')),
    key_hash text NOT NULL,
    payload bytea NOT NULL,
    expires_at timestamptz,
    grant_id text,
    PRIMARY KEY (kind, key_hash)
);
CREATE INDEX records_expiry_idx ON xingzhe.records (expires_at) WHERE expires_at IS NOT NULL;
CREATE INDEX records_grant_idx ON xingzhe.records (grant_id) WHERE grant_id IS NOT NULL;
ALTER TABLE xingzhe.records ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE xingzhe.records FROM PUBLIC;

-- Supabase roles are absent in a plain PostgreSQL development database.
DO $$
DECLARE role_name text;
BEGIN
    FOREACH role_name IN ARRAY ARRAY['anon', 'authenticated', 'service_role'] LOOP
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = role_name) THEN
            EXECUTE format('REVOKE ALL ON SCHEMA xingzhe FROM %I', role_name);
            EXECUTE format('REVOKE ALL ON TABLE xingzhe.records FROM %I', role_name);
        END IF;
    END LOOP;
END $$;
