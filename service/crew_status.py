"""Crew Monitor status service.

Read-only, stdlib-only, bound to 127.0.0.1. GET /crew returns, per Hermes profile (bot), the
background jobs that are really still running, recently finished jobs, kanban progress and a GPU
summary. GET /events pushes the same payload as server-sent events whenever it changes; GET /health
reports version and uptime.

It only exists because a Hermes Desktop plugin cannot read the filesystem or probe pids.
"Thinking" state is NOT computed here: the plugin reads it from the same profiles.list roster
data Desktop's own "Active now" uses (see docs/FINDINGS.md).

    pythonw.exe crew_status.py [--home F:\\Hermes\\data] [--port 8094] [--log FILE] [--config FILE]
                               [--gpu-lock FILE] [--ladder-url URL] [--allow-origin ORIGIN ...]
"""
from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import os
import re
import shlex
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from collections import deque
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Optional

DEFAULT_HOME = Path(os.environ.get("HERMES_HOME") or r"F:\Hermes\data")
DEFAULT_PORT = 8094
LADDER_URL = "http://127.0.0.1:8093/"
VERSION = "0.2.0"
API = 2  # bumped when the /crew payload changes shape; the plugin warns on a mismatch

log = logging.getLogger("crew-monitor")
log.addHandler(logging.NullHandler())


def default_gpu_lock(home: Path) -> Path:
    """<Hermes root>/tools/studio-control/logs/gpu_job_active.json, where home is <Hermes root>/data."""
    return home.parent / "tools" / "studio-control" / "logs" / "gpu_job_active.json"


GPU_LOCK = default_gpu_lock(DEFAULT_HOME)

# psutil create_time * 100, rounded (gateway.status._get_process_start_time). Same FILETIME source
# as our ctypes read, so equality holds to rounding; 2 centiseconds of slack.
START_TOLERANCE_CS = 2
# With no recorded host_start_time, a live pid born well after the job started is a reused pid.
BIRTH_SLACK_S = 30
COMMAND_MAX = 300
FINISHED_KEEP = 20
DEFAULT_STALL_S = 15 * 60


# --- process identity -------------------------------------------------------------------------

@dataclass(frozen=True)
class Proc:
    alive: bool
    birth: Optional[float] = None  # epoch seconds; None when unreadable


Probe = Callable[[int], Proc]
_K32 = None


def probe_pid(pid: int) -> Proc:
    """Windows: alive = exit code STILL_ACTIVE; birth = GetProcessTimes creation time."""
    if os.name != "nt":  # dev/test hosts only; Hermes here is Windows
        try:
            os.kill(pid, 0)
            return Proc(True, None)
        except OSError:
            return Proc(False)
    import ctypes
    from ctypes import wintypes

    global _K32
    if _K32 is None:
        _K32 = ctypes.WinDLL("kernel32", use_last_error=True)
        _K32.OpenProcess.restype = wintypes.HANDLE
        _K32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        _K32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        _K32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        _K32.CloseHandle.argtypes = [wintypes.HANDLE]
    k32 = _K32
    h = k32.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        # Gone, or access denied. Denied means a protected/system process: a job Hermes started as
        # this user can always be opened, so a denied pid is a recycled one, not our job.
        return Proc(False)
    try:
        code = wintypes.DWORD()
        if not k32.GetExitCodeProcess(h, ctypes.byref(code)) or code.value != 259:  # STILL_ACTIVE
            return Proc(False)
        c, e, k, u = (wintypes.FILETIME() for _ in range(4))
        if not k32.GetProcessTimes(h, ctypes.byref(c), ctypes.byref(e), ctypes.byref(k), ctypes.byref(u)):
            return Proc(True)
        ticks = (c.dwHighDateTime << 32) | c.dwLowDateTime
        return Proc(True, ticks / 1e7 - 11644473600)
    finally:
        k32.CloseHandle(h)


def job_is_live(entry: dict, probe: Probe = probe_pid) -> bool:
    """True only if the pid is alive AND is the process the registry recorded.

    Mirrors ProcessRegistry._host_pid_is_ours: a recycled pid has a different start time.
    Entries we cannot verify (non-host scope, no pid) are not shown.
    """
    pid = entry.get("pid")
    if not isinstance(pid, int) or pid <= 0 or entry.get("pid_scope", "host") != "host":
        return False
    proc = probe(pid)
    if not proc.alive:
        return False
    if proc.birth is None:
        return True  # alive, identity unreadable: same degrade as Hermes
    expected = entry.get("host_start_time")
    if isinstance(expected, int):
        return abs(round(proc.birth * 100) - expected) <= START_TOLERANCE_CS
    started = entry.get("started_at")
    if isinstance(started, (int, float)):
        return proc.birth <= started + BIRTH_SLACK_S
    return True


# --- command classification -------------------------------------------------------------------

_AUTHOR_NAME = re.compile(r'\\*"name\\*"\s*:\s*\\*"([^"\\]+)')
_APP = re.compile(r'--app[ =]+["\']?([^\s"\']+)')


def _script(name: str) -> re.Pattern:
    """Matches a script by its file name only: research.py, not my_research.py or research.pyc."""
    return re.compile(r"(?:^|[\s\"'/=])" + re.escape(name) + r"(?=$|[\s\"'])")


_DM, _REVIEW, _RESEARCH, _GPU_JOB = (_script(n) for n in ("bot_mode_dm.py", "local_review.py", "research.py", "gpu_job.py"))
# Unity's own test flags, or a runner whose file name mentions both (run_unity_tests.ps1).
_UNITY = re.compile(r"unity\S*\s.*-(?:runtests|testplatform)\b|[^\s/]*unity[^\s/]*test|[^\s/]*test[^\s/]*unity")


def classify(command: str, rules: tuple = ()) -> dict:
    """Friendly {kind, label} for a background command line. Config rules are tried first."""
    cmd = command or ""
    low = cmd.lower().replace("\\", "/")
    for pattern, kind, label in rules:
        m = pattern.search(cmd)
        if m:
            try:
                label = m.expand(label)  # \1 in a label is the first capture group
            except (re.error, IndexError):
                pass
            return {"kind": kind, "label": label}
    if _DM.search(low) and "--run-delivery" in low:
        m = _AUTHOR_NAME.search(cmd)
        who = m.group(1) if m else "a teammate"
        return {"kind": "dm", "label": f"receiving message from {who}", "from": who}
    if _REVIEW.search(low):
        return {"kind": "review", "label": "desloppify review"}
    if _RESEARCH.search(low):
        return {"kind": "research", "label": "deep research"}
    if _UNITY.search(low):
        return {"kind": "unity", "label": "Unity tests"}
    if _GPU_JOB.search(low):
        m = _APP.search(cmd)
        return {"kind": "gpu", "label": f"GPU job ({m.group(1)})" if m else "GPU job", "app": m.group(1) if m else None}
    one = " ".join(cmd.split())
    return {"kind": "other", "label": one[:60] + ("…" if len(one) > 60 else "")}


# --- sources ----------------------------------------------------------------------------------

_TITLE = re.compile(r"^\s+title:\s*['\"]?(.+?)['\"]?\s*$", re.M)


def parse_title(profile_yaml: str) -> Optional[str]:
    """ui_meta.hermes-bots.title without a YAML parser (stdlib only)."""
    i = profile_yaml.find("hermes-bots:")
    if i < 0:
        return None
    m = _TITLE.search(profile_yaml, i)
    return m.group(1) if m else None


def list_bots(home: Path) -> list[dict]:
    out = []
    root = home / "profiles"
    if not root.is_dir():
        return out
    for d in sorted(root.iterdir()):
        py = d / "profile.yaml"
        if not d.is_dir() or not py.is_file():
            continue
        try:
            title = parse_title(py.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            title = None
        out.append({"name": d.name, "title": title or d.name.replace("-", " ").title()})
    return out


def review_progress(command: str, cwd: Optional[str]) -> Optional[dict]:
    """Desloppify batches finished / total, read from the run dir named in the local_review.py command.

    A batch counts as done when results/batch-N.raw.txt exists (local_review.py renames invalid ones
    to .rejected.txt and skips only valid ones on resume). Total = files in prompts/.
    """
    try:
        argv = shlex.split(command.replace("\\", "/"), posix=True)
        i = next(n for n, a in enumerate(argv) if a.endswith("local_review.py"))
        run = Path(argv[i + 1])
        if argv[i + 1].startswith("-"):
            return None
        if not run.is_absolute() and cwd:
            run = Path(cwd) / run
        total = sum(1 for _ in (run / "prompts").iterdir())
        done = sum(1 for _ in (run / "results").glob("batch-*.raw.txt"))
    except (StopIteration, IndexError, OSError, ValueError):
        return None
    return {"done": done, "total": total, "unit": "batches"} if total else None


def kanban_progress(db: Path, now: float, window_s: int = 86400) -> dict:
    """{assignee: {done, total, items[, blocked]}} for tasks that are open, or finished within the last day.

    Only bots with something still open are reported (a fully finished board is not progress to watch).
    Read-only, WAL-aware connection; any error means no data.
    """
    try:
        con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=1)
        try:
            rows = con.execute(
                "SELECT assignee, status, completed_at, title FROM tasks "
                "WHERE assignee IS NOT NULL AND status != 'archived' ORDER BY created_at"
            ).fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return {}
    out: dict = {}
    for who, status, completed, title in rows:
        if status == "done" and not (completed and now - completed <= window_s):
            continue
        t = out.setdefault(who, {"done": 0, "total": 0, "items": []})
        t["total"] += 1
        t["done"] += status == "done"
        if status != "done":  # open tasks, for the hover card
            t["items"].append({"status": status, "title": " ".join(str(title or "").split())[:80]})
    for t in out.values():
        blocked = sum(1 for i in t["items"] if i["status"] == "blocked")
        if blocked:
            t["blocked"] = blocked
            t["items"].sort(key=lambda i: i["status"] != "blocked")  # stable: blocked first
        del t["items"][6:]
    return {k: v for k, v in out.items() if v["done"] < v["total"]}


_SESSION_OWNER = re.compile(r"agent:([^:]+):")


def job_owner(entry: dict, file_profile: str, kind: str, known: set) -> str:
    """Which bot a job belongs to.

    processes.json files are not a reliable owner: the same job can sit in several profiles' files
    (seen: a reviewer job in foreman's file). A DM delivery is shown under the receiver (the file
    it was written to); anything else belongs to the profile that started it (session_key).
    """
    if kind != "dm":
        m = _SESSION_OWNER.search(str(entry.get("session_key") or ""))
        if m and m.group(1) in known:
            return m.group(1)
    return file_profile


def collect_jobs(home: Path, names: list, now: float, probe: Probe = probe_pid,
                 titles: Optional[dict] = None, rules: tuple = ()) -> dict:
    """{bot name: [live job, ...]} across every profile's processes.json, de-duplicated."""
    known, out, seen = set(names), {n: [] for n in names}, set()
    titles = titles or {}
    for file_profile in names:
        try:
            raw = json.loads((home / "profiles" / file_profile / "processes.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for e in raw if isinstance(raw, list) else []:
            if not isinstance(e, dict) or not job_is_live(e, probe):
                continue
            ident = (e.get("pid"), e.get("host_start_time") or e.get("started_at"))
            if ident in seen:
                continue
            seen.add(ident)
            started = e.get("started_at")
            started = float(started) if isinstance(started, (int, float)) else None
            command, cwd = str(e.get("command") or ""), e.get("cwd")
            cls = classify(command, rules)
            progress = review_progress(command, cwd) if cls["kind"] == "review" else None
            owner = job_owner(e, file_profile, cls["kind"], known)
            if cls["kind"] == "dm":
                cls["label"] = f"{cls['from']} → {titles.get(owner, owner)}"
            out[owner].append({
                **cls,
                "id": f"{ident[0]}:{ident[1]}",
                # just enough to re-probe the pid after the entry leaves the file (Monitor)
                "entry": {k: e.get(k) for k in ("pid", "pid_scope", "host_start_time", "started_at")},
                "progress": progress,
                "pid": e.get("pid"),
                # for the hover card; one line, capped (DM deliveries carry the whole message)
                "cwd": cwd if isinstance(cwd, str) else None,
                "command": " ".join(command.split())[:COMMAND_MAX] or None,
                "session_id": e.get("session_id"),
                "started_at": started,
                "elapsed_s": max(0, int(now - started)) if started else None,
            })
    for jobs in out.values():
        jobs.sort(key=lambda j: j["started_at"] or 0)
    return out


def _get_json(url: str, timeout: float = 1.0) -> Optional[dict]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:  # 127.0.0.1 only
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None


def gpu_label(ladder: Optional[dict], lock: Optional[dict]) -> dict:
    """{state, label} for the top row. A live gpu_job lock beats the ladder's own status."""
    if lock:
        return {"state": "held", "label": f"held by gpu_job {lock.get('app') or ''}".strip()}
    if not ladder:
        return {"state": "unknown", "label": "Ladder unreachable"}
    mode = ladder.get("mode")
    if mode == "held":
        return {"state": "held", "label": f"held by {ladder.get('held_by') or 'another job'}"}
    if mode == "cloud":
        return {"state": "cloud", "label": "cloud fallback"}
    if mode == "local":
        ctx = ladder.get("ctx") or 0
        used, total = ladder.get("vram_used_mib"), ladder.get("vram_total_mib")
        vram = f" · {used / 1024:.1f}/{total / 1024:.1f} GB" if used is not None and total else ""
        return {"state": "local", "label": f"Ladder {round(ctx / 1024)}K local{vram}"}
    return {"state": "unknown", "label": f"Ladder {mode or 'unknown'}"}


def read_lock(lock_path: Path, probe: Probe = probe_pid) -> Optional[dict]:
    try:
        data = json.loads(lock_path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and job_is_live({"pid": data.get("pid")}, probe):
            return data
    except (OSError, ValueError):
        pass
    return None


def read_nvidia(run=subprocess.run) -> Optional[dict]:
    """{util_pct, temp_c, vram_used_mib, vram_total_mib} of the largest GPU, or None without nvidia-smi."""
    try:
        res = run(
            ["nvidia-smi", "--query-gpu=utilization.gpu,temperature.gpu,memory.used,memory.total",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=4,
            creationflags=0x08000000 if os.name == "nt" else 0,  # CREATE_NO_WINDOW
        )
        cards = [[int(float(x)) for x in line.split(",")] for line in res.stdout.splitlines() if line.strip()]
        util, temp, used, total = max(cards, key=lambda c: c[3])
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return None
    return {"util_pct": util, "temp_c": temp, "vram_used_mib": used, "vram_total_mib": total}


def gpu_summary(ladder: Optional[dict], lock: Optional[dict], nvidia: Optional[dict] = None) -> dict:
    """The /crew "gpu" object. VRAM comes from nvidia-smi when present, else from the ladder."""
    out = {**gpu_label(ladder, lock), "ladder": ladder, "lock": lock,
           "vram_used_mib": None, "vram_total_mib": None, "util_pct": None, "temp_c": None, "queue": []}
    for src in (ladder, nvidia):
        for k in ("vram_used_mib", "vram_total_mib", "util_pct", "temp_c"):
            if src and isinstance(src.get(k), (int, float)):
                out[k] = src[k]
    return out


def read_gpu(probe: Probe = probe_pid, lock_path: Path = GPU_LOCK, ladder_url: str = LADDER_URL) -> dict:
    """One blocking read (up to 1 s when the ladder is down). The service uses GpuPoller instead."""
    return gpu_summary(_get_json(ladder_url), read_lock(lock_path, probe), None)


class GpuPoller:
    """Keeps the GPU summary fresh on its own thread so /crew never waits on the ladder or nvidia-smi."""

    def __init__(self, lock_path: Path, ladder_url: str = LADDER_URL, probe: Probe = probe_pid):
        self.lock_path, self.ladder_url, self.probe = lock_path, ladder_url, probe
        self._value = gpu_summary(None, None)
        self._nvidia, self._nvidia_at, self._nvidia_every = None, 0.0, 5.0

    def get(self) -> dict:
        return self._value

    def poll(self) -> None:
        now = time.monotonic()
        if now - self._nvidia_at >= self._nvidia_every:
            self._nvidia, self._nvidia_at = read_nvidia(), now
            self._nvidia_every = 5.0 if self._nvidia else 60.0  # no nvidia-smi: look again each minute
        self._value = gpu_summary(_get_json(self.ladder_url), read_lock(self.lock_path, self.probe), self._nvidia)

    def run(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                self.poll()
            except Exception:
                log.exception("gpu poll failed")
            stop.wait(2.0)


def gpu_queue(bots: list, gpu: dict) -> list:
    """GPU jobs that are alive but do not hold the lock: they are waiting for it. Marks them too.

    The holder is the job with the lock's pid; failing that (the registry pid can be a wrapper shell),
    the oldest job for the lock's app. With no live lock nobody is known to be waiting.
    """
    lock = gpu.get("lock")
    if not lock:
        return []
    jobs = sorted(((j, b) for b in bots for j in b["jobs"] if j["kind"] == "gpu"), key=lambda x: x[0]["started_at"] or 0)
    holder = next((j for j, _ in jobs if j["pid"] == lock.get("pid")), None) or next(
        (j for j, _ in jobs if j.get("app") and j.get("app") == lock.get("app")), None)
    if holder is None:
        return []
    out = []
    for j, b in jobs:
        if j is not holder:
            j["waiting_gpu"] = True
            out.append({"bot": b["name"], "title": b["title"], "label": j["label"]})
    return out


# --- config -----------------------------------------------------------------------------------

_COLOR = re.compile(r"^#(?:[0-9a-fA-F]{3,4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")


@dataclass(frozen=True)
class Config:
    rules: tuple = ()  # (compiled pattern, kind, label), tried before the built-in classifier
    colors: dict = field(default_factory=dict)  # bot name -> "#rrggbb"
    stall_s: int = DEFAULT_STALL_S


def parse_config(data) -> Config:
    """Tolerant: anything malformed is skipped (and logged), never fatal."""
    if not isinstance(data, dict):
        return Config()
    rules = []
    for r in data.get("rules") or []:
        try:
            rules.append((re.compile(r["match"], re.I), str(r.get("kind") or "other"), str(r["label"])))
        except (re.error, KeyError, TypeError) as exc:
            log.warning("config: skipped rule %r: %s", r, exc)
    colors = {str(k): v for k, v in (data.get("colors") or {}).items() if isinstance(v, str) and _COLOR.match(v)}
    stall = data.get("stall_minutes")
    stall_s = int(stall * 60) if isinstance(stall, (int, float)) and stall > 0 else DEFAULT_STALL_S
    return Config(tuple(rules), colors, stall_s)


class ConfigFile:
    """Re-read when the file's mtime changes; a missing or broken file is the default config."""

    def __init__(self, path: Optional[Path]):
        self.path, self._mtime, self._cfg = path, None, Config()

    def get(self) -> Config:
        if self.path is None:
            return self._cfg
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            mtime = None
        if mtime != self._mtime:
            self._mtime = mtime
            try:
                self._cfg = parse_config(json.loads(self.path.read_text(encoding="utf-8"))) if mtime else Config()
                log.info("config loaded: %s", self.path if mtime else "defaults")
            except (OSError, ValueError) as exc:
                log.warning("config %s unreadable, using defaults: %s", self.path, exc)
                self._cfg = Config()
        return self._cfg


# --- snapshot ---------------------------------------------------------------------------------

def snapshot(home: Path = DEFAULT_HOME, now: Optional[float] = None, probe: Probe = probe_pid,
             gpu: Optional[dict] = None, config: Optional[Config] = None) -> dict:
    """One stateless reading. gpu defaults to "unknown" (no network); the service passes GpuPoller's."""
    now = time.time() if now is None else now
    config = config or Config()
    bots = list_bots(home)
    titles = {b["name"]: b["title"] for b in bots}
    jobs = collect_jobs(home, list(titles), now, probe, titles, config.rules)
    tasks = kanban_progress(home / "kanban.db", now)
    bots = [{**b, "color": config.colors.get(b["name"]), "jobs": jobs[b["name"]], "tasks": tasks.get(b["name"])}
            for b in bots]
    gpu = dict(gpu) if gpu else gpu_summary(None, None)
    gpu["queue"] = gpu_queue(bots, gpu)
    return {"api": API, "version": VERSION, "ts": now, "gpu": gpu, "bots": bots, "finished": []}


class Monitor:
    """Snapshots over time: caching, finished-job history, progress ETA / stall, change notification.

    Nothing is persisted: history starts empty when the service starts.
    """

    def __init__(self, home: Path, probe: Probe = probe_pid, gpu: Optional[Callable[[], dict]] = None,
                 config: Optional[ConfigFile] = None, clock: Callable[[], float] = time.time):
        self.home, self.probe, self.clock = home, probe, clock
        self._gpu = gpu or (lambda: None)
        self._config = config or ConfigFile(None)
        self._refresh_lock = threading.Lock()
        self._cond = threading.Condition()
        self._snap: Optional[dict] = None
        self._sig, self._seq = None, 0
        self._live: dict = {}  # job id -> summary of a job seen alive
        self._track: dict = {}  # job id -> progress observations
        self.finished: deque = deque(maxlen=FINISHED_KEEP)
        self.started = clock()
        self.last_request = 0.0
        self.clients = 0  # open /events streams
        self._last_error = None

    def refresh(self) -> dict:
        with self._refresh_lock:
            now = self.clock()
            cfg = self._config.get()
            snap = snapshot(self.home, now, self.probe, self._gpu(), cfg)
            self._observe(snap, now, cfg)
            snap["finished"] = list(self.finished)
            sig = _signature(snap)
            with self._cond:
                self._snap = snap
                if sig != self._sig:
                    self._sig, self._seq = sig, self._seq + 1
                    self._cond.notify_all()
            return snap

    def get(self, max_age: float = 1.0) -> dict:
        """The cached snapshot, refreshed first if older than max_age seconds."""
        self.last_request = self.clock()
        snap = self._snap
        if snap is None or self.clock() - snap["ts"] > max_age:
            snap = self.refresh()
        return snap

    def wait(self, seq: int, timeout: float):
        """Block until the payload differs from version `seq` (or timeout). Returns (seq, snapshot)."""
        with self._cond:
            if self._seq == seq:
                self._cond.wait(timeout)
            return self._seq, self._snap

    def run(self, stop: threading.Event) -> None:
        """Keep observing even with nobody connected, so finished jobs are caught; slower when idle."""
        while not stop.is_set():
            try:
                self.refresh()
                self._last_error = None
            except Exception as exc:
                if repr(exc) != self._last_error:  # log a persistent failure once, not every tick
                    self._last_error = repr(exc)
                    log.exception("refresh failed")
            watched = self.clients > 0 or self.clock() - self.last_request < 30
            stop.wait(2.0 if watched else 10.0)

    def _observe(self, snap: dict, now: float, cfg: Config) -> None:
        live = {}
        for b in snap["bots"]:
            for j in b["jobs"]:
                entry = j.pop("entry")
                live[j["id"]] = {"id": j["id"], "bot": b["name"], "title": b["title"], "kind": j["kind"],
                                 "label": j["label"], "started_at": j["started_at"], "entry": entry,
                                 "seen_at": self._live.get(j["id"], {}).get("seen_at", now)}
                p = j["progress"]
                if not p:
                    continue
                t = self._track.get(j["id"])
                if t is None:
                    t = self._track[j["id"]] = {"t0": now, "d0": p["done"], "t": now, "d": p["done"]}
                elif p["done"] != t["d"]:
                    t["t"], t["d"] = now, p["done"]
                # Rate from what this service watched (a resumed run starts with batches already done).
                # Fixed at the last change, so the payload only changes when progress does.
                p["eta_at"] = None
                if t["d"] > t["d0"] and t["t"] > t["t0"] and p["done"] < p["total"]:
                    rate = (t["d"] - t["d0"]) / (t["t"] - t["t0"])
                    p["eta_at"] = round(t["t"] + (p["total"] - p["done"]) / rate)
                p["changed_at"] = round(t["t"])
                p["stalled"] = p["done"] < p["total"] and now - t["t"] >= cfg.stall_s
        for jid, old in self._live.items():
            if jid in live:
                continue
            if job_is_live(old["entry"], self.probe):
                live[jid] = old  # dropped from the file (or the file was mid-write) but still running
                continue
            start = old["started_at"] or old["seen_at"]
            self.finished.appendleft({k: old[k] for k in ("id", "bot", "title", "kind", "label", "started_at")}
                                     | {"ended_at": round(now), "duration_s": max(0, round(now - start))})
        self._live = live
        self._track = {k: v for k, v in self._track.items() if k in live}


def _signature(snap: dict) -> str:
    """The payload minus the fields that tick every second: equal signatures need no push."""
    bots = [{**b, "jobs": [{**j, "elapsed_s": 0} for j in b["jobs"]]} for b in snap["bots"]]
    return json.dumps({**snap, "ts": 0, "bots": bots}, sort_keys=True, default=str)


# --- http -------------------------------------------------------------------------------------

_LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def host_allowed(host: Optional[str]) -> bool:
    """Reject requests addressed to any other name: that is what DNS rebinding looks like."""
    if not host:
        return True  # HTTP/1.0 client
    name = host.strip().lower()
    name = name[1:].split("]")[0] if name.startswith("[") else name.split(":")[0]
    return name in _LOOPBACK


def origin_allowed(origin: Optional[str], allow: frozenset = frozenset()) -> bool:
    """May a page of this origin read the response?

    With --allow-origin, only those exact origins. Otherwise everything except ordinary web sites:
    app shells (app://, tauri://, file://, "null") and loopback pages pass, http(s)://anything-else
    does not. "null" is also what a sandboxed iframe sends, so pin the origin for a strict setup.
    """
    if not origin:
        return True  # not a browser cross-origin read
    if allow:
        return origin in allow
    if origin == "null":
        return True
    u = urllib.parse.urlsplit(origin)
    if u.scheme not in ("http", "https"):
        return True
    name = (u.hostname or "").lower()
    return name in _LOOPBACK or name.endswith(".localhost")


def make_handler(source, allow_origins: frozenset = frozenset()):
    """source: a Monitor, or a Hermes home Path (a Monitor is made for it)."""
    mon = source if isinstance(source, Monitor) else Monitor(Path(source))
    origins_seen: set = set()

    class Handler(BaseHTTPRequestHandler):
        timeout = 10  # a client that connects and says nothing does not hold a thread for ever

        def _gate(self) -> bool:
            origin = self.headers.get("Origin")
            if origin and origin not in origins_seen and len(origins_seen) < 50:
                origins_seen.add(origin)
                log.info("first request from origin %s", origin)
            if host_allowed(self.headers.get("Host")) and origin_allowed(origin, allow_origins):
                return True
            log.warning("refused: host=%r origin=%r", self.headers.get("Host"), origin)
            self.send_error(403)
            return False

        def _head(self, status: int, ctype: str, length: Optional[int] = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            if length is not None:
                self.send_header("Content-Length", str(length))
            self.send_header("Cache-Control", "no-store")
            origin = self.headers.get("Origin")
            if origin:  # already checked by _gate; never "*"
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Vary", "Origin")
            self.end_headers()

        def _json(self, make) -> None:
            try:
                status, body = 200, json.dumps(make()).encode("utf-8")
            except Exception as exc:  # never crash the poll loop
                log.exception("request failed: %s", self.path)
                status, body = 500, json.dumps({"error": str(exc)}).encode("utf-8")
            self._head(status, "application/json", len(body))
            self.wfile.write(body)

        def _events(self) -> None:
            self._head(200, "text/event-stream")
            mon.clients += 1
            try:
                mon.get()
                seq = -1
                while True:
                    new, snap = mon.wait(seq, 15)
                    mon.last_request = mon.clock()
                    if new != seq and snap is not None:
                        seq = new
                        self.wfile.write(b"data: " + json.dumps(snap).encode("utf-8") + b"\n\n")
                    else:
                        self.wfile.write(b"event: ping\ndata: {}\n\n")
                    self.wfile.flush()
            except OSError:
                pass  # client went away
            finally:
                mon.clients -= 1

        def do_GET(self):  # noqa: N802
            path = self.path.split("?")[0]
            if path not in ("/crew", "/events", "/health"):
                self.send_error(404)
            elif not self._gate():
                pass
            elif path == "/crew":
                self._json(mon.get)
            elif path == "/events":
                self._events()
            else:
                self._json(lambda: {
                    "ok": True, "api": API, "version": VERSION, "home": str(mon.home),
                    "uptime_s": round(mon.clock() - mon.started), "clients": mon.clients,
                    "origins_seen": sorted(origins_seen),
                })

        def log_message(self, *_):
            pass

    return Handler


class Server(ThreadingHTTPServer):
    # On Windows SO_REUSEADDR lets a second process bind a port that is already served, silently.
    allow_reuse_address = os.name != "nt"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--home", type=Path, default=DEFAULT_HOME)
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--log", type=Path, help="rotating log file (pythonw has no console)")
    ap.add_argument("--config", type=Path, default=Path(__file__).with_name("crew-monitor.json"),
                    help="optional JSON: classifier rules, bot colours, stall_minutes")
    ap.add_argument("--gpu-lock", type=Path, help="gpu_job lock file (default: derived from --home)")
    ap.add_argument("--ladder-url", default=LADDER_URL)
    ap.add_argument("--allow-origin", action="append", default=[],
                    help="exact Origin allowed to read the service; repeatable. Default: any non-web origin")
    a = ap.parse_args(argv)
    if a.log:
        h = logging.handlers.RotatingFileHandler(a.log, maxBytes=1_000_000, backupCount=2, encoding="utf-8")
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        log.addHandler(h)
        log.setLevel(logging.INFO)
    try:
        poller = GpuPoller(a.gpu_lock or default_gpu_lock(a.home), a.ladder_url)
        mon = Monitor(a.home, gpu=poller.get, config=ConfigFile(a.config))
        server = Server(("127.0.0.1", a.port), make_handler(mon, frozenset(a.allow_origin)))
    except OSError as exc:  # port taken: say so instead of dying silently
        log.error("cannot start on 127.0.0.1:%s: %s", a.port, exc)
        print(f"cannot start on 127.0.0.1:{a.port}: {exc}", file=sys.stderr)
        return 1
    stop = threading.Event()
    for target in (poller.run, mon.run):
        threading.Thread(target=target, args=(stop,), daemon=True).start()
    log.info("crew monitor %s serving %s on 127.0.0.1:%s", VERSION, a.home, a.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        log.info("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
