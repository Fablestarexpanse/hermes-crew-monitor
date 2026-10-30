"""End to end: a fake Hermes home (profiles, kanban.db, a desloppify run dir, a live job) served over HTTP."""
import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import crew_status as cs


class CrewHttp(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        home = Path(tmp.name)
        for name, title in (("reviewer", "Reviewer"), ("scout", "Scout")):
            d = home / "profiles" / name
            d.mkdir(parents=True)
            (d / "profile.yaml").write_text(f"ui_meta:\n  hermes-bots:\n    title: {title}\n")
        run = home / "run 1"
        (run / "prompts").mkdir(parents=True)
        (run / "results").mkdir()
        for n in range(1, 11):
            (run / "prompts" / f"batch-{n}.md").write_text("x")
        for n in range(1, 4):
            (run / "results" / f"batch-{n}.raw.txt").write_text("x")
        # a job whose pid is this very process: alive, and its start time is read from the OS
        birth = cs.probe_pid(os.getpid()).birth
        job = {"pid": os.getpid(), "pid_scope": "host", "host_start_time": round(birth * 100),
               "started_at": time.time() - 90, "session_key": "agent:reviewer:local:dm:x",
               "command": f'python "F:/t/local_review.py" "{run.as_posix()}" --repo .'}
        (home / "profiles" / "reviewer" / "processes.json").write_text(json.dumps([job]))
        con = sqlite3.connect(home / "kanban.db")
        con.execute("CREATE TABLE tasks (id TEXT, assignee TEXT, status TEXT, completed_at INTEGER, title TEXT, created_at INTEGER)")
        now = int(time.time())
        con.executemany("INSERT INTO tasks VALUES (?,?,?,?,?,?)", [
            ("1", "scout", "done", now - 30, "find refs", 1), ("2", "scout", "done", now - 20, "rank refs", 2),
            ("3", "scout", "done", now - 10, "summarise", 3), ("4", "scout", "running", None, "write brief", 4),
            ("5", "scout", "todo", None, "send brief", 5)])
        con.commit()
        con.close()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), cs.make_handler(home))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.shutdown)

    def get(self, path):
        return urllib.request.urlopen(f"http://127.0.0.1:{self.server.server_port}{path}", timeout=5)

    def test_crew_endpoint(self):
        res = self.get("/crew")
        self.assertEqual(res.headers["Access-Control-Allow-Origin"], "*")
        bots = {b["name"]: b for b in json.load(res)["bots"]}
        job = bots["reviewer"]["jobs"][0]
        self.assertEqual(job["label"], "desloppify review")
        self.assertEqual(job["progress"], {"done": 3, "total": 10, "unit": "batches"})
        self.assertEqual((bots["scout"]["tasks"]["done"], bots["scout"]["tasks"]["total"]), (3, 5))
        self.assertEqual([i["status"] for i in bots["scout"]["tasks"]["items"]], ["running", "todo"])
        self.assertIsNone(bots["reviewer"]["tasks"])

    def test_unknown_path_404(self):
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self.get("/nope")
        self.assertEqual(cm.exception.code, 404)


if __name__ == "__main__":
    unittest.main()
