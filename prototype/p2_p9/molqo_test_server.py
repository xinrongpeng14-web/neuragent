#!/usr/bin/env python3
"""nr_molqo 的测试服务端: 按路径返回不同格式的"优化结果", 并记录每个请求。"""
import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOG = sys.argv[2] if len(sys.argv) > 2 else "/tmp/molqo_requests.log"
SETS = "SET enable_hashjoin TO off;\nSET enable_mergejoin TO off;\n"


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"])).decode("utf-8")
        req = json.loads(body)
        sql = req.get("sql", "")
        with open(LOG, "a") as f:
            f.write(json.dumps({"path": self.path, "expert_filter": req.get("expert_filter"),
                                "sql_len": len(sql)}) + "\n")
        name = "HintPlanSel"
        if self.path == "/set":
            opt = SETS + sql
        elif self.path == "/hint":
            opt, name = "/*+ MergeJoin(a b) */ " + sql, "JoinOrder"
        elif self.path == "/slow":
            time.sleep(3)
            opt = SETS + sql
        elif self.path == "/badset":
            opt = "SET no_such_guc TO 1;\n" + SETS + sql
        else:
            opt, name = sql, "none"
        resp = {"original_sql": sql, "optimized_sql": opt,
                "optimization_applied": opt != sql, "expert_name": name, "endpoint": self.path}
        data = json.dumps(resp).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):
        pass


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8080
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
