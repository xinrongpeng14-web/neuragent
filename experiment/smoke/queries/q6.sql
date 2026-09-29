SELECT min(a.title) FROM sm_title AS a, sm_cast AS b WHERE a.id = b.movie_id AND b.person_id = 4242 AND a.kind_id > 0
