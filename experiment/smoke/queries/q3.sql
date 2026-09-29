SELECT min(a.title), min(c.name) FROM sm_title AS a, sm_cast AS b, sm_person AS c WHERE a.id = b.movie_id AND b.person_id = c.id AND a.kind_id = 2 AND c.gender = 'm' AND b.role_id = 1
