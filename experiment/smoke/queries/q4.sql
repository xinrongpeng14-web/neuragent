SELECT count(*) FROM sm_title AS a, sm_cast AS b WHERE a.id = b.movie_id AND a.production_year BETWEEN 1990 AND 1995 AND a.kind_id IN (1, 3)
