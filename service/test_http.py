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
        job = {"pid": os.getpid(), "pid_scope": "host",
               **({"host_start_time": round(birth * 100)} if birth is not None else {}),  # None off Windows
               "started_at": time.time() - 90, "session_key": "agent:reviewer:local:dm:x", "cwd": "F:/t",
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
        (home / "c.json").write_text(json.dumps({"instances": [{"name": "Demo", "port": 9, "start": "x"}]}))
        self.calls = []

        class Fake(cs.Instances):
            def act(fake, iid, action):
                self.calls.append((iid, action))
                return 200, {"ok": True}
        cfg = cs.ConfigFile(home / "c.json")
        self.mon = cs.Monitor(home, config=cfg, token="s3cret-token-of-enough-length",
                              instances=Fake(cfg, listeners=dict, processes=dict, vram=dict))
        self.mon.instances.poll()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), cs.make_handler(self.mon))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def get(self, path, **headers):
        req = urllib.request.Request(f"http://127.0.0.1:{self.server.server_port}{path}", headers=headers)
        return urllib.request.urlopen(req, timeout=5)

    def test_crew_endpoint(self):
        res = self.get("/crew", Origin="app://hermes")
        self.assertEqual(res.headers["Access-Control-Allow-Origin"], "app://hermes")  # echoed, never "*"
        data = json.load(res)
        self.assertEqual((data["api"], data["finished"], data["gpu"]["state"]), (cs.API, [], "unknown"))
        bots = {b["name"]: b for b in data["bots"]}
        job = bots["reviewer"]["jobs"][0]
        self.assertEqual(job["label"], "desloppify review")
        self.assertEqual(job["cwd"], "F:/t")
        self.assertIn("local_review.py", job["command"])
        self.assertEqual({k: job["progress"][k] for k in ("done", "total", "unit", "stalled")},
                         {"done": 3, "total": 10, "unit": "batches", "stalled": False})
        self.assertEqual((bots["scout"]["tasks"]["done"], bots["scout"]["tasks"]["total"]), (3, 5))
        self.assertEqual([i["status"] for i in bots["scout"]["tasks"]["items"]], ["running", "todo"])
        self.assertIsNone(bots["reviewer"]["tasks"])

    def test_web_page_origin_and_foreign_host_are_refused(self):
        for headers in ({"Origin": "https://evil.example"}, {"Host": "evil.example"}):
            for path in ("/crew", "/events", "/health"):
                with self.assertRaises(urllib.error.HTTPError) as cm:
                    self.get(path, **headers)
                self.assertEqual(cm.exception.code, 403)
        self.assertIsNone(self.get("/crew").headers["Access-Control-Allow-Origin"])

    def test_health(self):
        h = json.load(self.get("/health", Origin="null"))
        self.assertEqual((h["ok"], h["api"], h["version"], h["origins_seen"]), (True, cs.API, cs.VERSION, ["null"]))

    def test_events_stream_pushes_the_snapshot_first(self):
        res = self.get("/events")
        self.assertEqual(res.headers["Content-Type"], "text/event-stream")
        line = res.readline().decode()
        self.assertTrue(line.startswith("data: "))
        self.assertEqual(json.loads(line[6:])["bots"][0]["name"], "reviewer")
        res.close()

    def post(self, path, **headers):
        req = urllib.request.Request(f"http://127.0.0.1:{self.server.server_port}{path}", data=b"", headers=headers, method="POST")
        try:
            return urllib.request.urlopen(req, timeout=5).status
        except urllib.error.HTTPError as e:
            return e.code

    def test_instances_are_listed_and_control_needs_the_token(self):
        data = json.load(self.get("/crew"))
        self.assertEqual((data["control"], [(i["id"], i["state"]) for i in data["instances"]]), (True, [("demo", "stopped")]))
        self.assertEqual(self.post("/instances/demo/start"), 403)
        self.assertEqual(self.post("/instances/demo/start", **{"X-Crew-Token": "wrong"}), 403)
        self.assertEqual(self.post("/instances/demo/start", Origin="https://evil.example", **{"X-Crew-Token": self.mon.token}), 403)
        self.assertEqual(self.post("/instances/demo/restart", **{"X-Crew-Token": self.mon.token}), 404)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.post("/instances/demo/start", Origin="app://hermes", **{"X-Crew-Token": self.mon.token}), 200)
        self.assertEqual(self.calls, [("demo", "start")])

    def test_control_is_off_without_a_token(self):
        self.mon.token = ""
        self.assertEqual(self.post("/instances/demo/stop", **{"X-Crew-Token": ""}), 403)
        self.assertFalse(json.load(self.get("/crew"))["control"])
        self.assertEqual(self.calls, [])

    def test_preflight_allows_the_token_header_for_app_origins_only(self):
        url = f"http://127.0.0.1:{self.server.server_port}/instances/demo/stop"
        res = urllib.request.urlopen(urllib.request.Request(url, method="OPTIONS", headers={"Origin": "app://hermes"}), timeout=5)
        self.assertEqual((res.status, res.headers["Access-Control-Allow-Origin"]), (204, "app://hermes"))
        self.assertIn("X-Crew-Token", res.headers["Access-Control-Allow-Headers"])
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(urllib.request.Request(url, method="OPTIONS", headers={"Origin": "https://evil.example"}), timeout=5)
        self.assertEqual(cm.exception.code, 403)

    def test_unknown_path_404(self):
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self.get("/nope")
        self.assertEqual(cm.exception.code, 404)


if __name__ == "__main__":
    unittest.main()
