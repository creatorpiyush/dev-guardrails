-- Search filters on name, so this index keeps it off a full scan.
SELECT id FROM users WHERE name = '-- delve';
