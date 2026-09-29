-- Small stand-in for the JOB workload, only for checking that the experiment
-- program works end to end. Table aliases in the queries are a, b and c.
SET client_min_messages = warning;
DROP TABLE IF EXISTS sm_cast, sm_title, sm_person;
SELECT setseed(0.5);
CREATE TABLE sm_title (id int PRIMARY KEY, kind_id int, production_year int, title text);
INSERT INTO sm_title
  SELECT g, 1 + (g % 7), 1950 + (g % 70), 'title ' || g FROM generate_series(1, 60000) g;
CREATE TABLE sm_person (id int PRIMARY KEY, gender text, name text);
INSERT INTO sm_person
  SELECT g, CASE WHEN g % 3 = 0 THEN 'f' ELSE 'm' END, 'person ' || g FROM generate_series(1, 30000) g;
CREATE TABLE sm_cast (id int PRIMARY KEY, movie_id int, person_id int, role_id int);
INSERT INTO sm_cast
  SELECT g, 1 + (random() * 59999)::int, 1 + (random() * 29999)::int, 1 + (g % 11)
  FROM generate_series(1, 300000) g;
CREATE INDEX ON sm_cast (movie_id);
CREATE INDEX ON sm_cast (person_id);
ANALYZE sm_title; ANALYZE sm_person; ANALYZE sm_cast;
CREATE EXTENSION IF NOT EXISTS nram;
CREATE EXTENSION IF NOT EXISTS nr_molqo;
CREATE EXTENSION IF NOT EXISTS pg_hint_plan;
