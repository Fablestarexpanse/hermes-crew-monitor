import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import crew_status as cs

NOW = 1_793_381_473.0 + 600


def proc(alive=True, birth=None):
    return lambda pid: cs.Proc(alive, birth)


class Classify(unittest.TestCase):
    def test_dm_delivery_names_author(self):
        cmd = ('python "F:/x/tools/bot_mode_dm.py" --run-delivery --author '
               '{"id":"bot:foreman","name":"Foreman","is_bot":true} query-file dm.txt')
        self.assertEqual(cs.classify(cmd), {"kind": "dm", "label": "receiving message from Foreman"})

    def test_dm_delivery_shell_escaped_author(self):
        cmd = r'python bot_mode_dm.py --run-delivery --author "{\"id\":\"u1\",\"name\":\"Brian\"}" file x'
        self.assertEqual(cs.classify(cmd)["label"], "receiving message from Brian")

    def test_dm_without_author(self):
        self.assertEqual(cs.classify("python bot_mode_dm.py --run-delivery query-file a")["label"],
                         "receiving message from a teammate")

    def test_dm_script_without_run_delivery_is_not_dm(self):
        self.assertEqual(cs.classify("python bot_mode_dm.py --wait-reply x")["kind"], "other")

    def test_known_tools(self):
        self.assertEqual(cs.classify(r'"F:\a\local_review.py" run --repo .')["label"], "desloppify review")
        self.assertEqual(cs.classify("python tools/research.py q")["label"], "deep research")
        self.assertEqual(cs.classify("Unity.exe -runTests -testPlatform PlayMode unity test")["label"], "Unity tests")

    def test_gpu_job_app(self):
        self.assertEqual(cs.classify("python gpu_job.py --app comfyui -- x")["label"], "GPU job (comfyui)")
        self.assertEqual(cs.classify("python gpu_job.py -- x")["label"], "GPU job")

    def test_fallback_truncates_to_60(self):
        out = cs.classify("echo " + "x" * 200)
        self.assertEqual(out["kind"], "other")
        self.assertEqual(len(out["label"]), 61)
        self.assertTrue(out["label"].endswith("…"))

    def test_fallback_short_untouched(self):
        self.assertEqual(cs.classify("sleep   5")["label"], "sleep 5")


class StalePid(unittest.TestCase):
    base = {"pid": 4242, "pid_scope": "host", "host_start_time": 179338147372, "started_at": 1793381473.648}

    def test_dead_pid_hidden(self):
        self.assertFalse(cs.job_is_live(self.base, proc(alive=False)))

    def test_alive_matching_start_shown(self):
        self.assertTrue(cs.job_is_live(self.base, proc(birth=1793381473.72)))

    def test_alive_within_rounding_shown(self):
        self.assertTrue(cs.job_is_live(self.base, proc(birth=1793381473.74)))

    def test_reused_pid_hidden(self):
        self.assertFalse(cs.job_is_live(self.base, proc(birth=1793381473.72 + 3600)))

    def test_alive_birth_unreadable_shown(self):
        self.assertTrue(cs.job_is_live(self.base, proc(birth=None)))

    def test_no_host_start_time_uses_started_at(self):
        e = {"pid": 7, "pid_scope": "host", "started_at": 1000.0}
        self.assertTrue(cs.job_is_live(e, proc(birth=1000.2)))
        self.assertFalse(cs.job_is_live(e, proc(birth=5000.0)))

    def test_non_host_scope_hidden(self):
        self.assertFalse(cs.job_is_live({**self.base, "pid_scope": "wsl"}, proc(birth=1793381473.72)))

    def test_bad_pid_hidden(self):
        for pid in (None, 0, -1, "12", True and "x"):
            self.assertFalse(cs.job_is_live({"pid": pid}, proc()))

    def test_real_current_process_is_live_and_bogus_is_not(self):
        import os
        self.assertTrue(cs.probe_pid(os.getpid()).alive)
        self.assertFalse(cs.probe_pid(0x7FFFFFF0).alive)


class Snapshot(unittest.TestCase):
    def make_home(self, files):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name) / "profiles"
        for name, title in (("foreman", "Foreman"), ("scout", None), ("reviewer", "Reviewer")):
            d = root / name
            d.mkdir(parents=True)
            body = f"ui_meta:\n  hermes-bots:\n    shape: blobatar\n    title: {title}\n" if title else "x: 1\n"
            (d / "profile.yaml").write_text(body)
            if name in files:
                (d / "processes.json").write_text(json.dumps(files[name]))
        (root / "junk").mkdir()  # no profile.yaml: skipped
        return Path(tmp.name)

    def test_titles_and_only_live_jobs(self):
        gpu = {"pid": 1, "pid_scope": "host", "command": "python gpu_job.py --app comfyui",
               "started_at": NOW - 125, "session_id": "a"}
        stale = {"pid": 2, "pid_scope": "host", "command": "stale", "started_at": NOW - 9999}
        home = self.make_home({"foreman": [gpu, stale], "scout": []})
        snap = cs.snapshot(home, NOW, lambda pid: cs.Proc(pid == 1))
        bots = {b["name"]: b for b in snap["bots"]}
        self.assertEqual(sorted(bots), ["foreman", "reviewer", "scout"])
        self.assertEqual((bots["foreman"]["title"], bots["scout"]["title"]), ("Foreman", "Scout"))
        self.assertEqual([(j["label"], j["elapsed_s"]) for j in bots["foreman"]["jobs"]], [("GPU job (comfyui)", 125)])
        self.assertEqual(bots["scout"]["jobs"], [])

    def test_job_in_another_profiles_file_goes_to_its_session_owner_once(self):
        review = {"pid": 5, "pid_scope": "host", "command": "python local_review.py x", "started_at": NOW - 60,
                  "session_key": "agent:reviewer:local:dm:abc"}
        home = self.make_home({"foreman": [review], "reviewer": [review]})
        jobs = cs.collect_jobs(home, ["foreman", "reviewer", "scout"], NOW, lambda pid: cs.Proc(True))
        self.assertEqual(jobs["foreman"], [])
        self.assertEqual([j["label"] for j in jobs["reviewer"]], ["desloppify review"])

    def test_dm_delivery_stays_with_the_receiver_file(self):
        dm = {"pid": 6, "pid_scope": "host", "started_at": NOW - 5, "session_key": "agent:foreman:local:dm:abc",
              "command": 'python bot_mode_dm.py --run-delivery --author {"name":"Foreman"} q d'}
        home = self.make_home({"reviewer": [dm]})
        jobs = cs.collect_jobs(home, ["foreman", "reviewer"], NOW, lambda pid: cs.Proc(True))
        self.assertEqual(jobs["foreman"], [])
        self.assertEqual(jobs["reviewer"][0]["label"], "receiving message from Foreman")

    def test_malformed_processes_json_is_empty(self):
        home = self.make_home({})
        (home / "profiles" / "scout" / "processes.json").write_text("{not json")
        self.assertEqual(cs.collect_jobs(home, ["scout"], NOW, lambda pid: cs.Proc(True)), {"scout": []})


class Progress(unittest.TestCase):
    def make_run(self, prompts, done, rejected=0):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        run = Path(tmp.name) / "run dir 1"  # a space, like the real "Game Projects" paths
        (run / "prompts").mkdir(parents=True)
        (run / "results").mkdir()
        for n in range(1, prompts + 1):
            (run / "prompts" / f"batch-{n}.md").write_text("x")
        for n in range(1, done + 1):
            (run / "results" / f"batch-{n}.raw.txt").write_text("x")
        for n in range(done + 1, done + 1 + rejected):
            (run / "results" / f"batch-{n}.rejected.txt").write_text("x")
        return run

    def test_review_batches_done_of_total_ignoring_rejected(self):
        run = self.make_run(20, 3, rejected=1)
        cmd = f'cd "{run.parent}" && "F:/py/python.exe" "F:/t/local_review.py" "{run}" --repo .'
        self.assertEqual(cs.review_progress(cmd, str(run.parent)), {"done": 3, "total": 20, "unit": "batches"})

    def test_review_progress_none_when_run_dir_missing_or_unparsable(self):
        self.assertIsNone(cs.review_progress('python local_review.py "Z:/nope/run" --repo .', None))
        self.assertIsNone(cs.review_progress("python other.py", None))
        self.assertIsNone(cs.review_progress("python local_review.py --repo .", None))

    def test_kanban_counts_open_and_recently_done_only(self):
        with tempfile.TemporaryDirectory() as t:
            db = Path(t) / "kanban.db"
            con = sqlite3.connect(db)
            con.execute("CREATE TABLE tasks (id TEXT, assignee TEXT, status TEXT, completed_at INTEGER, title TEXT, created_at INTEGER)")
            rows = [("1", "scout", "done", NOW - 60), ("2", "scout", "done", NOW - 120), ("3", "scout", "running", None),
                    ("4", "scout", "todo", None), ("5", "scout", "done", NOW - 10 * 86400),  # too old: ignored
                    ("6", "scout", "archived", None), ("7", "foreman", "done", NOW - 5),  # all done: not reported
                    ("8", None, "todo", None)]
            con.executemany("INSERT INTO tasks VALUES (?,?,?,?,?,?)", [r + (f"task {r[0]}", int(r[0])) for r in rows])
            con.commit()
            con.close()
            self.assertEqual(cs.kanban_progress(db, NOW), {"scout": {"done": 2, "total": 4, "items": [{"status": "running", "title": "task 3"}, {"status": "todo", "title": "task 4"}]}})

    def test_kanban_missing_db_is_empty(self):
        self.assertEqual(cs.kanban_progress(Path("Z:/nope/kanban.db"), NOW), {})


class Gpu(unittest.TestCase):
    def test_labels(self):
        local = {"mode": "local", "ctx": 262144, "vram_used_mib": 19763, "vram_total_mib": 24564}
        self.assertEqual(cs.gpu_label(local, None)["label"], "Ladder 256K local · 19.3/24.0 GB")
        self.assertEqual(cs.gpu_label({"mode": "cloud"}, None)["label"], "cloud fallback")
        self.assertEqual(cs.gpu_label({"mode": "held", "held_by": "gpu_job comfyui"}, None)["label"],
                         "held by gpu_job comfyui")
        self.assertEqual(cs.gpu_label(local, {"app": "comfyui"})["label"], "held by gpu_job comfyui")
        self.assertEqual(cs.gpu_label(None, None)["state"], "unknown")


if __name__ == "__main__":
    unittest.main()
