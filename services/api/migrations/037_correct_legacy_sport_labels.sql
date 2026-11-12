-- The station client maps an upstream competition id (csid) to a sport. Five
-- ids had no mapping at all, so their rows were stored as source_3, source_6,
-- source_8, source_9 and source_37, and csid 7 was labelled baseball. Every row
-- csid 7 produced was 冠中冠 between 奥沙利文, 特鲁姆普, 罗伯森 and 墨菲, which is
-- snooker; the real baseball leagues arrived under csid 3 and fell through to
-- source_3. The mapping is corrected in collector.leyu_protocol.SPORTS.
--
-- Fixing the mapping only helps rows the source still reports: the fixture
-- upsert compares content and skips unchanged rows, so a finished match keeps
-- whatever label it was first written with. Measured before this migration:
-- 1666 rows still carried a source_N token and 9 snooker matches sat under
-- baseball. Both sets are corrected here from evidence already in the row, the
-- competition name the same source reported alongside it, not from a guessed
-- id-to-sport table.
--
-- Only the sport label changes. Scores, phases, kickoff times, odds and expert
-- signals are untouched: no expert signal references any of these fixtures
-- (verified count 0 before writing this), and quotes are keyed by fixture id.
--
-- Rollback: the previous labels are not recoverable from the new value, so a
-- rollback must restore from a backup taken before this migration. That is
-- acceptable because the corrected label is the accurate one; nothing in the
-- product depends on the misspelling.

UPDATE v2_fixtures SET sport = 'table_tennis' WHERE sport = 'source_8';
UPDATE v2_fixtures SET sport = 'volleyball' WHERE sport = 'source_9';
UPDATE v2_fixtures SET sport = 'cricket' WHERE sport = 'source_37';
UPDATE v2_fixtures SET sport = 'american_football' WHERE sport = 'source_6';
UPDATE v2_fixtures SET sport = 'baseball' WHERE sport = 'source_3';

-- 冠中冠 is a snooker invitational; the nine rows under baseball are that event,
-- not baseball leagues.
UPDATE v2_fixtures SET sport = 'snooker'
WHERE sport = 'baseball' AND competition_name IN ('冠中冠', '冠中冠杯');
