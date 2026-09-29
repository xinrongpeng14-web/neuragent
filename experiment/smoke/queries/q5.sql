SELECT max(a.production_year), count(*) FROM sm_title AS a, sm_cast AS b, sm_person AS c WHERE a.id = b.movie_id AND b.person_id = c.id AND c.id < 3000 AND a.production_year > 1980
