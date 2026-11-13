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
        self.assertEqual(cs.classify(cmd), {"kind": "dm", "label": "receiving message from Foreman", "from": "Foreman"})

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

    def test_lookalike_names_are_not_the_known_tools(self):
        for cmd in ("python my_research.py q", "python research.pyc", "python notes/unity/readme_test.py",
                    "python old_local_review.py.bak x", "pytest tests/ --unity-build"):
            self.assertEqual(cs.classify(cmd)["kind"], "other", cmd)
        self.assertEqual(cs.classify("pwsh tools/run_unity_tests.ps1")["kind"], "unity")
        self.assertEqual(cs.classify('"C:/Program Files/Unity/Editor/Unity.exe" -batchmode -runTests')["kind"], "unity")

    def test_config_rules_win_and_can_use_capture_groups(self):
        cfg = cs.parse_config({"rules": [{"match": r"blender(?:\.exe)?\s+-b\s+(\S+)", "kind": "render", "label": r"Blender render (\1)"},
                                         {"match": "(", "label": "broken regex is skipped"}, {"label": "no match key"}],
                               "colors": {"scout": "#00ff88", "bad": "red;}", "n": 5}, "stall_minutes": 2})
        self.assertEqual(cs.classify("Blender.exe -b shot.blend -a", cfg.rules), {"kind": "render", "label": "Blender render (shot.blend)"})
        self.assertEqual(cs.classify("python research.py q", cfg.rules)["kind"], "research")
        self.assertEqual((cfg.colors, cfg.stall_s), ({"scout": "#00ff88"}, 120))
        self.assertEqual(cs.parse_config(["nonsense"]), cs.Config())

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

    def test_linux_kernel_ticks_must_match_exactly(self):
        e = {"pid": 7, "pid_scope": "host", "host_start_time": 123456, "started_at": 1000.0}
        self.assertTrue(cs.job_is_live(e, lambda pid: cs.Proc(True, 1000.0, 123456)))
        self.assertFalse(cs.job_is_live(e, lambda pid: cs.Proc(True, 1000.0, 123457)))  # recycled pid

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

    def test_hover_fields_cwd_and_capped_one_line_command(self):
        job = {"pid": 1, "pid_scope": "host", "cwd": "F:/p", "command": "run\n  it " + "x" * 500, "started_at": NOW - 1}
        home = self.make_home({"scout": [job]})
        (j,) = cs.collect_jobs(home, ["scout"], NOW, lambda pid: cs.Proc(True))["scout"]
        self.assertEqual(j["cwd"], "F:/p")
        self.assertTrue(j["command"].startswith("run it x"))
        self.assertEqual(len(j["command"]), cs.COMMAND_MAX)

    def test_default_profile_lives_in_the_home_folder_itself(self):
        job = {"pid": 1, "pid_scope": "host", "command": "python research.py q", "started_at": NOW - 5}
        home = self.make_home({"scout": []})
        self.assertNotIn("default", [b["name"] for b in cs.list_bots(home)])  # unused: no empty row
        (home / "processes.json").write_text(json.dumps([job]))
        (home / "profile.yaml").write_text("display_name: 'Hermes'\n")
        bots = {b["name"]: b for b in cs.snapshot(home, NOW, lambda pid: cs.Proc(True))["bots"]}
        self.assertEqual((bots["default"]["title"], [j["label"] for j in bots["default"]["jobs"]]), ("Hermes", ["deep research"]))

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
        self.assertEqual(jobs["reviewer"][0]["label"], "Foreman → reviewer")
        titled = cs.collect_jobs(home, ["foreman", "reviewer"], NOW, lambda pid: cs.Proc(True), {"reviewer": "Reviewer"})
        self.assertEqual(titled["reviewer"][0]["label"], "Foreman → Reviewer")

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


class Security(unittest.TestCase):
    def test_host_must_be_loopback(self):
        for ok in (None, "127.0.0.1:8094", "localhost:8094", "LOCALHOST", "[::1]:8094"):
            self.assertTrue(cs.host_allowed(ok), ok)
        for bad in ("evil.example:8094", "127.0.0.1.evil.example", "192.168.1.5:8094"):
            self.assertFalse(cs.host_allowed(bad), bad)

    def test_web_sites_cannot_read_app_shells_can(self):
        for ok in (None, "null", "app://-", "tauri://localhost", "file://", "http://localhost:5173",
                   "https://tauri.localhost", "http://127.0.0.1:3000"):
            self.assertTrue(cs.origin_allowed(ok), ok)
        for bad in ("https://evil.example", "http://localhost.evil.example", "https://127.0.0.1.evil.example"):
            self.assertFalse(cs.origin_allowed(bad), bad)

    def test_pinned_origins_are_exact(self):
        allow = frozenset({"app://hermes"})
        self.assertTrue(cs.origin_allowed("app://hermes", allow))
        self.assertTrue(cs.origin_allowed(None, allow))
        for bad in ("null", "app://other", "http://localhost:5173"):
            self.assertFalse(cs.origin_allowed(bad, allow), bad)


class GpuExtras(unittest.TestCase):
    def test_nvidia_smi_parsed_largest_card_and_absent_is_none(self):
        class R:
            stdout = "3, 40, 900, 8192\n62, 71, 22016, 24564\n"
        self.assertEqual(cs.read_nvidia(lambda *a, **k: R),
                         {"util_pct": 62, "temp_c": 71, "vram_used_mib": 22016, "vram_total_mib": 24564})

        def missing(*a, **k):
            raise FileNotFoundError
        self.assertIsNone(cs.read_nvidia(missing))
        R.stdout = "[N/A], x\n"
        self.assertIsNone(cs.read_nvidia(lambda *a, **k: R))

    def test_summary_prefers_nvidia_numbers_over_the_ladder(self):
        ladder = {"mode": "local", "ctx": 262144, "vram_used_mib": 100, "vram_total_mib": 24564}
        g = cs.gpu_summary(ladder, None, {"util_pct": 9, "temp_c": 50, "vram_used_mib": 200, "vram_total_mib": 24564})
        self.assertEqual((g["vram_used_mib"], g["util_pct"], g["temp_c"], g["state"]), (200, 9, 50, "local"))
        self.assertEqual(cs.gpu_summary(ladder, None)["vram_used_mib"], 100)
        self.assertIsNone(cs.gpu_summary(None, None)["vram_total_mib"])

    def test_without_a_ladder_installed_the_row_is_plain_gpu_stats_or_hidden(self):
        nv = {"util_pct": 9, "temp_c": 50, "vram_used_mib": 200, "vram_total_mib": 24564}
        self.assertEqual(cs.gpu_summary(None, None, None, False)["state"], "none")
        g = cs.gpu_summary(None, None, nv, False)
        self.assertEqual((g["state"], g["label"], g["vram_used_mib"]), ("local", "GPU", 200))
        self.assertEqual(cs.gpu_summary(None, None, nv, True)["label"], "Ladder unreachable")  # it used to answer
        self.assertEqual(cs.gpu_summary(None, {"app": "comfyui"}, nv, False)["state"], "held")

    def test_default_home_matches_hermes_per_platform(self):
        home = lambda: Path("/u/me")
        self.assertEqual(cs.default_home({"HERMES_HOME": " /data/h "}, "win32", home), Path("/data/h"))
        self.assertEqual(cs.default_home({"LOCALAPPDATA": "C:/Users/me/AppData/Local"}, "win32", home),
                         Path("C:/Users/me/AppData/Local/hermes"))
        self.assertEqual(cs.default_home({}, "win32", home), Path("/u/me/AppData/Local/hermes"))
        self.assertEqual(cs.default_home({}, "linux", home), Path("/u/me/.hermes"))
        self.assertEqual(cs.default_home({}, "darwin", home), Path("/u/me/.hermes"))

    def test_queue_is_every_gpu_job_but_the_lock_holder(self):
        def bots():
            job = lambda pid, app, t: {"kind": "gpu", "pid": pid, "app": app, "label": f"GPU job ({app})", "started_at": t}
            return [{"name": "asset-bot", "title": "Asset Bot", "jobs": [job(10, "comfyui", 1), job(11, "blender", 2)]},
                    {"name": "trailer-bot", "title": "Trailer Bot", "jobs": [job(12, "comfyui", 3), {"kind": "other", "pid": 13, "started_at": 4}]}]
        b = bots()
        q = cs.gpu_queue(b, {"lock": {"pid": 10, "app": "comfyui"}})
        self.assertEqual([(x["title"], x["label"]) for x in q], [("Asset Bot", "GPU job (blender)"), ("Trailer Bot", "GPU job (comfyui)")])
        self.assertNotIn("waiting_gpu", b[0]["jobs"][0])
        self.assertTrue(b[1]["jobs"][0]["waiting_gpu"])
        # lock pid is not a registry pid (wrapper shell): the oldest job for that app is the holder
        self.assertEqual([x["label"] for x in cs.gpu_queue(bots(), {"lock": {"pid": 999, "app": "comfyui"}})],
                         ["GPU job (blender)", "GPU job (comfyui)"])
        self.assertEqual(cs.gpu_queue(bots(), {"lock": None}), [])
        self.assertEqual(cs.gpu_queue(bots(), {"lock": {"pid": 999, "app": "krita"}}), [])  # holder unknown: no guess


class MonitorTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        d = self.home / "profiles" / "reviewer"
        d.mkdir(parents=True)
        (d / "profile.yaml").write_text("ui_meta:\n  hermes-bots:\n    title: Reviewer\n")
        self.run_dir = self.home / "run"
        (self.run_dir / "prompts").mkdir(parents=True)
        (self.run_dir / "results").mkdir()
        for n in range(10):
            (self.run_dir / "prompts" / f"batch-{n}.md").write_text("x")
        self.now, self.alive = NOW, {5}
        self.mon = cs.Monitor(self.home, probe=lambda pid: cs.Proc(pid in self.alive), clock=lambda: self.now)
        self.write_jobs([{"pid": 5, "pid_scope": "host", "started_at": NOW - 100,
                          "command": f'python local_review.py "{self.run_dir.as_posix()}"'}])

    def write_jobs(self, jobs):
        (self.home / "profiles" / "reviewer" / "processes.json").write_text(json.dumps(jobs))

    def tick(self, seconds, batches=None):
        self.now += seconds
        if batches is not None:
            for n in range(batches):
                (self.run_dir / "results" / f"batch-{n}.raw.txt").write_text("x")
        return self.mon.refresh()

    def job(self, snap):
        return snap["bots"][0]["jobs"][0]

    def test_eta_from_the_observed_rate_and_fixed_between_changes(self):
        p = self.job(self.tick(0, batches=2))["progress"]
        self.assertEqual((p["done"], p["eta_at"], p["stalled"]), (2, None, False))  # resumed run: no rate yet
        p = self.job(self.tick(60, batches=4))["progress"]  # 2 batches a minute, 6 left
        self.assertEqual(p["eta_at"], round(self.now + 180))
        later = self.job(self.tick(30))["progress"]
        self.assertEqual((later["eta_at"], later["changed_at"]), (p["eta_at"], p["changed_at"]))

    def test_stalled_after_no_progress_for_the_configured_time_then_recovers(self):
        self.tick(0, batches=1)
        self.assertFalse(self.job(self.tick(cs.DEFAULT_STALL_S - 1))["progress"]["stalled"])
        self.assertTrue(self.job(self.tick(1))["progress"]["stalled"])
        self.assertFalse(self.job(self.tick(5, batches=2))["progress"]["stalled"])

    def test_finished_job_is_recorded_once_with_its_duration(self):
        self.tick(0)
        self.alive.clear()
        snap = self.tick(20)
        self.assertEqual(snap["bots"][0]["jobs"], [])
        (f,) = snap["finished"]
        self.assertEqual((f["title"], f["label"], f["duration_s"], f["ended_at"]), ("Reviewer", "desloppify review", 120, round(self.now)))
        self.assertEqual(len(self.tick(5)["finished"]), 1)

    def test_entry_missing_from_the_file_but_pid_alive_is_not_finished(self):
        self.tick(0)
        self.write_jobs([])  # e.g. read mid-write, or pruned by Hermes while the process runs on
        self.assertEqual(self.tick(2)["finished"], [])
        self.alive.clear()
        self.assertEqual(len(self.tick(2)["finished"]), 1)

    def test_cache_and_change_sequence(self):
        a = self.mon.get()
        self.assertIs(self.mon.get(), a)  # inside max_age: no re-read
        seq, _ = self.mon.wait(-1, 0)
        self.tick(5)  # only elapsed time moved: same payload version
        self.assertEqual(self.mon.wait(seq, 0)[0], seq)
        self.tick(5, batches=1)
        self.assertEqual(self.mon.wait(seq, 0)[0], seq + 1)
        self.assertNotIn("entry", self.job(self.mon.get(0)))

    def test_config_file_colours_reach_the_payload_and_reload(self):
        cfg = self.home / "crew-monitor.json"
        cfg.write_text(json.dumps({"colors": {"reviewer": "#123456"}}))
        mon = cs.Monitor(self.home, probe=lambda pid: cs.Proc(False), config=cs.ConfigFile(cfg), clock=lambda: self.now)
        self.assertEqual(mon.refresh()["bots"][0]["color"], "#123456")
        cfg.unlink()
        self.assertIsNone(mon.refresh()["bots"][0]["color"])

    def test_kanban_blocked_first_and_counted(self):
        con = sqlite3.connect(self.home / "kanban.db")
        con.execute("CREATE TABLE tasks (id TEXT, assignee TEXT, status TEXT, completed_at INTEGER, title TEXT, created_at INTEGER)")
        con.executemany("INSERT INTO tasks VALUES (?,?,?,?,?,?)", [("1", "reviewer", "todo", None, "a", 1), ("2", "reviewer", "blocked", None, "b", 2)])
        con.commit()
        con.close()
        t = self.tick(0)["bots"][0]["tasks"]
        self.assertEqual((t["blocked"], [i["title"] for i in t["items"]]), (1, ["b", "a"]))


class InstancesTest(unittest.TestCase):
    CFG = {"instances": [
        {"name": "ComfyUI", "port": 8188, "start": "run.bat", "cwd": "F:/ComfyUI"},
        {"name": "Ollama", "id": "ollama", "process": "ollama.exe"},
        {"name": "no way to tell if it is up"}, {"name": "ComfyUI", "port": 1}, {"name": "x", "id": "../evil", "port": 2},
        {"name": "bad port", "port": "8188"}, "nonsense"]}

    def manager(self, listeners=None, procs=None, cfg=None):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        f = Path(tmp.name) / "crew-monitor.json"
        f.write_text(json.dumps(cfg or self.CFG))
        self.listeners, self.procs = listeners or {}, procs or {}
        return cs.Instances(cs.ConfigFile(f), Path(tmp.name) / "logs", probe=lambda pid: cs.Proc(True, 1000.0),
                            listeners=lambda: dict(self.listeners), processes=lambda: dict(self.procs),
                            vram=lambda: {4321: 6144}, clock=lambda: NOW)

    def test_config_keeps_only_well_formed_instances(self):
        inst = cs.parse_config(self.CFG).instances
        self.assertEqual([(i.id, i.port, i.process, i.start) for i in inst],
                         [("comfyui", 8188, None, "run.bat"), ("ollama", None, "ollama.exe", None)])
        self.assertEqual(cs.parse_config({"instances": "x"}).instances, ())

    def test_status_by_port_and_by_process_name(self):
        m = self.manager({8188: 4321}, {"ollama.exe": [77]})
        by = {i["id"]: i for i in m.poll()}
        self.assertEqual((by["comfyui"]["state"], by["comfyui"]["pid"], by["comfyui"]["vram_mib"], by["comfyui"]["started_at"]),
                         ("running", 4321, 6144, 1000))
        self.assertEqual((by["comfyui"]["can_start"], by["comfyui"]["can_stop"]), (True, True))
        self.assertEqual((by["ollama"]["state"], by["ollama"]["can_start"], by["ollama"]["vram_mib"]), ("running", False, None))
        self.listeners, self.procs = {}, {}
        self.assertEqual([i["state"] for i in m.poll()], ["stopped", "stopped"])

    def test_well_known_servers_show_only_when_up_and_are_never_controllable(self):
        m = self.manager({11434: 9, 1234: 10}, cfg={"instances": []})
        got = m.poll()
        self.assertEqual([(i["name"], i["listed"], i["can_start"], i["can_stop"]) for i in got],
                         [("Ollama", False, False, False), ("LM Studio", False, False, False)])
        self.assertEqual(m.act("ollama", "stop")[0], 404)  # found, but not listed: hands off

    def test_act_refuses_what_makes_no_sense(self):
        m = self.manager({8188: 4321}, {})
        self.assertEqual(m.act("nope", "start")[0], 404)
        self.assertEqual(m.act("comfyui", "start")[0], 409)  # already running
        self.assertEqual(m.act("ollama", "stop")[0], 409)  # not running
        self.assertEqual(m.act("ollama", "start")[0], 400)  # no start command

    def test_kill_tree_never_touches_system_pids_or_itself(self):
        import os
        calls = []
        for pid in (0, 4, -1, None, os.getpid()):
            self.assertFalse(cs.kill_tree(pid, run=lambda *a, **k: calls.append(a)))
        self.assertEqual(calls, [])

    def test_netstat_and_tasklist_output_is_parsed(self):
        import os
        class R:
            stdout = ""
        if os.name == "nt":
            R.stdout = ("  Proto  Local Address  Foreign Address  State  PID\n"
                        "  TCP    127.0.0.1:8188    0.0.0.0:0    LISTENING    4321\n"
                        "  TCP    [::]:11434    [::]:0    ABHÖREN    77\n"
                        "  TCP    127.0.0.1:50000    127.0.0.1:8188    ESTABLISHED    5\n")
            self.assertEqual(cs.read_listeners(lambda *a, **k: R), {8188: 4321, 11434: 77})
            R.stdout = '"ollama.exe","77","Console","1","50,000 K"\n"Ollama.exe","78","Console","1","1 K"\n'
            self.assertEqual(cs.read_processes(lambda *a, **k: R), {"ollama.exe": [77, 78]})
        else:
            R.stdout = 'LISTEN 0 5 127.0.0.1:8188 0.0.0.0:* users:(("python",pid=4321,fd=3))\nLISTEN 0 5 [::]:11434 [::]:*\n'
            self.assertEqual(cs.read_listeners(lambda *a, **k: R), {8188: 4321, 11434: None})
            R.stdout = "   77 ollama\n   78 ollama\n"
            self.assertEqual(cs.read_processes(lambda *a, **k: R), {"ollama": [77, 78]})
        R.stdout = "4321, 6144\n99, [N/A]\n"
        self.assertEqual(cs.read_vram_by_pid(lambda *a, **k: R), {4321: 6144})

    def test_really_starts_and_stops_a_listed_server(self):
        import socket, sys, time
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        f = Path(tmp.name) / "c.json"
        f.write_text(json.dumps({"instances": [{"name": "Demo", "port": port,
                                                "start": f'"{sys.executable}" -m http.server {port} --bind 127.0.0.1'}]}))
        m = cs.Instances(cs.ConfigFile(f), Path(tmp.name) / "logs")
        self.addCleanup(lambda: m.act("demo", "stop"))

        def wait_for(state):
            end = time.time() + 20
            while time.time() < end:
                if m.poll()[0]["state"] == state:
                    return True
                time.sleep(0.2)
            return False
        self.assertEqual(m.poll()[0]["state"], "stopped")
        self.assertEqual(m.act("demo", "start"), (200, {"ok": True, "state": "starting"}))
        self.assertTrue(wait_for("running"), m.poll())
        self.assertEqual(m.act("demo", "start")[0], 409)
        self.assertEqual(m.act("demo", "stop")[0], 200)
        self.assertTrue(wait_for("stopped"), m.poll())
        self.assertTrue((Path(tmp.name) / "logs" / "demo.log").exists())


if __name__ == "__main__":
    unittest.main()
