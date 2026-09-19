-- Persist dynamically registered clients without expiring their credentials.
ALTER TABLE xingzhe.records DROP CONSTRAINT records_kind_check;
ALTER TABLE xingzhe.records ADD CONSTRAINT records_kind_check
    CHECK (kind IN ('client', 'connection', 'state', 'consent', 'code', 'access', 'refresh'));
