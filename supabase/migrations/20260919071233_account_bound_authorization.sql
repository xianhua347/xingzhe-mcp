-- Legacy owner grants cannot safely be assigned to a verified Xingzhe account.
-- Users must reconnect once after upgrading from the single-owner version.
ALTER TABLE xingzhe.records ADD COLUMN subject_hash text;
CREATE INDEX records_subject_idx ON xingzhe.records (subject_hash)
    WHERE subject_hash IS NOT NULL;
DELETE FROM xingzhe.records;
