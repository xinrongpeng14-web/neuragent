"""pg_conn.py: 连接时以启动参数关闭 molqo, 服务端没有该参数时退回普通连接。"""
import os
import sys
import types
import unittest

REPO = os.environ.get("NEURQO", "/home/zhanhao/neuragent/NeuralDB/aiengine/neurqo_frame")


class OperationalError(Exception):
    pass


calls = []
behaviour = {"reject_option": False, "other_error": False}


class FakeCursor:
    def __init__(self):
        self.sql = []

    def execute(self, q):
        self.sql.append(q)

    def close(self):
        pass


class FakeConn:
    autocommit = False

    def __init__(self):
        self.cur = FakeCursor()

    def cursor(self):
        return self.cur

    def rollback(self):
        pass

    def close(self):
        pass


def fake_connect(dsn, **kwargs):
    calls.append(kwargs)
    if behaviour["other_error"]:
        raise OperationalError("could not connect to server: Connection refused")
    if behaviour["reject_option"] and "options" in kwargs:
        raise OperationalError('FATAL:  unrecognized configuration parameter "enable_molqo"')
    return FakeConn()


psy = types.ModuleType("psycopg2")
psy.connect = fake_connect
psy.OperationalError = OperationalError
psy.Error = Exception
sys.modules["psycopg2"] = psy
sys.path.insert(0, os.path.join(REPO, "src"))
for m in [k for k in sys.modules if k == "db" or k.startswith("db.")]:
    del sys.modules[m]
from db.pg_conn import PostgresConnector  # noqa: E402

_out = open(os.devnull, "w")


class ConnectTest(unittest.TestCase):
    def setUp(self):
        calls.clear()
        behaviour.update(reject_option=False, other_error=False)
        self._stdout, sys.stdout = sys.stdout, _out

    def tearDown(self):
        sys.stdout = self._stdout

    def test_option_is_sent(self):
        c = PostgresConnector("imdb_ori")
        c.connect()
        self.assertEqual(calls, [{"options": "-c enable_molqo=off"}])
        self.assertTrue(c.connection.autocommit)

    def test_fallback_when_setting_unknown(self):
        behaviour["reject_option"] = True
        c = PostgresConnector("imdb_ori")
        c.connect()
        self.assertEqual(calls, [{"options": "-c enable_molqo=off"}, {}])
        self.assertIsNotNone(c.cursor)

    def test_other_errors_are_not_swallowed(self):
        behaviour["other_error"] = True
        c = PostgresConnector("imdb_ori")
        with self.assertRaises(OperationalError):
            c.connect()
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
