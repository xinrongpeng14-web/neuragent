SELECT min(a.title), count(*) FROM sm_title AS a, sm_cast AS b WHERE a.id = b.movie_id AND a.production_year > 2010 AND b.role_id = 3
