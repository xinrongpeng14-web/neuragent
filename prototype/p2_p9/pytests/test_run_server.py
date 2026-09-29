"""run.py: 多进程服务、expert_filter 透传、/stats 跨进程汇总、冻结开关、关停。"""
import json
import os
import signal
import socket
import subprocess
import sys
import time
import unittest
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

REPO = os.environ.get("NEURQO", "/home/zhanhao/neuragent/NeuralDB/aiengine/neurqo_frame")
HERE = os.path.dirname(os.path.abspath(__file__))


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def post(port, sql, expert_filter=None):
    body = {"sql": sql}
    if expert_filter is not None:
        body["expert_filter"] = expert_filter
    req = urllib.request.Request(f"http://127.0.0.1:{port}/optimize", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def get(port, path):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=10) as r:
        return json.loads(r.read())


class Server:
    def __init__(self, *extra, delay="0"):
        self.port = free_port()
        env = dict(os.environ, MOQOE_CONTROLLER_FACTORY="fake_controller:make",
                   PYTHONPATH=HERE, FAKE_DELAY=delay)
        self.proc = subprocess.Popen(
            [sys.executable, os.path.join(REPO, "run.py"), "--host", "127.0.0.1", "--port", str(self.port),
             "--torch-threads", "0", "--quiet", *extra],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    def wait_ready(self, workers):
        deadline = time.time() + 20
        pids = set()
        while time.time() < deadline and len(pids) < workers:
            try:
                pids.add(get(self.port, "/health")["worker_pid"])
            except Exception:
                time.sleep(0.1)
        return pids

    def stop(self):
        self.proc.send_signal(signal.SIGTERM)
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            raise
        return self.proc.returncode


class MultiWorkerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = Server("--workers", "3", "--freeze", delay="0.3")
        cls.pids = cls.srv.wait_ready(3)

    @classmethod
    def tearDownClass(cls):
        cls.srv.stop()

    def test_1_three_workers_answer(self):
        self.assertEqual(len(self.pids), 3)

    def test_2_requests_run_in_parallel_and_filter_is_passed(self):
        jobs = [("select 1", "hint"), ("select 2", "join"), ("select 3", "all"),
                ("select 4", None), ("select plain", "all"), ("select boom", "hint")]
        t0 = time.time()
        with ThreadPoolExecutor(6) as ex:
            results = list(ex.map(lambda j: post(self.srv.port, *j), jobs))
        elapsed = time.time() - t0
        self.assertLess(elapsed, 1.5, "6 个各 0.3 秒的请求由 3 个进程处理应约 0.6 秒")
        self.assertEqual([r[0] for r in results], [200, 200, 200, 200, 200, 500])
        self.assertEqual([r[1]["expert_name"] for r in results],
                         ["HintPlanSel", "JoinOrder", "JoinOrder", "JoinOrder",
                          "cost-based optimizer", "cost-based optimizer"])
        self.assertEqual(results[0][1]["optimized_sql"], "/*+ Leading(a b) */ select 1")
        self.assertFalse(results[4][1]["optimization_applied"])
        self.assertEqual(results[5][1]["optimized_sql"], "select boom")
        self.assertGreaterEqual(len({r[1]["worker_pid"] for r in results[:5]}), 2)

    def test_3_stats_are_summed_over_workers(self):
        seen = [get(self.srv.port, "/stats") for _ in range(6)]
        for s in seen:
            self.assertEqual(s, dict(seen[0], uptime_s=s["uptime_s"]))
        s = seen[0]
        self.assertEqual(s["workers"], 3)
        self.assertEqual(s["requests"], 6)
        self.assertEqual(s["optimized"], 4)
        self.assertEqual(s["fallbacks"], 2)
        self.assertEqual(s["errors"], 1)
        self.assertEqual((s["expert_HintPlanSel"], s["expert_JoinOrder"]), (1, 3))
        self.assertEqual((s["filter_all"], s["filter_hint"], s["filter_join"]), (3, 1, 1))
        self.assertGreater(s["opt_time_ms"], 5 * 300 * 0.9)
        self.assertLess(s["opt_time_ms"], 6 * 300 * 1.5)

    def test_4_unknown_path(self):
        with self.assertRaises(urllib.error.HTTPError) as cm:
            get(self.srv.port, "/nope")
        self.assertEqual(cm.exception.code, 404)


class LifecycleTest(unittest.TestCase):
    def test_sigterm_stops_parent_and_workers(self):
        srv = Server("--workers", "2", "--freeze")
        pids = srv.wait_ready(2)
        self.assertEqual(len(pids), 2)
        self.assertEqual(srv.stop(), 0)
        time.sleep(0.5)
        for pid in pids:
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)

    def test_killed_worker_is_replaced(self):
        srv = Server("--workers", "2", "--freeze")
        try:
            pids = srv.wait_ready(2)
            victim = sorted(pids)[0]
            os.kill(victim, signal.SIGKILL)
            deadline = time.time() + 15
            now = set()
            while time.time() < deadline and not (len(now) == 2 and victim not in now):
                now = {get(srv.port, "/health")["worker_pid"] for _ in range(12)}
                time.sleep(0.2)
            self.assertEqual(len(now), 2)
            self.assertNotIn(victim, now)
        finally:
            srv.stop()

    def test_several_workers_require_freeze(self):
        p = subprocess.run([sys.executable, os.path.join(REPO, "run.py"), "--workers", "2"],
                           capture_output=True, text=True)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("--freeze", p.stderr)

    def test_freeze_sets_readonly_cache(self):
        sys.path.insert(0, REPO)
        import importlib
        run = importlib.import_module("run")
        os.environ.pop("MOQOE_READONLY", None)
        self.assertFalse(run.parse_args([]).freeze)
        self.assertTrue(run.parse_args(["--freeze", "--workers", "4"]).freeze)


if __name__ == "__main__":
    unittest.main(verbosity=2)
