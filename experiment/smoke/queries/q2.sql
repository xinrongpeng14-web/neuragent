SELECT min(c.name), count(*) FROM sm_cast AS b, sm_person AS c WHERE b.person_id = c.id AND c.gender = 'f' AND b.role_id < 4
