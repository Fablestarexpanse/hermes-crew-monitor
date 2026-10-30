"""Crew Monitor status service.

Read-only, stdlib-only, bound to 127.0.0.1. GET /crew returns, per Hermes profile (bot), the
background jobs that are really still running, plus an optional GPU summary.

It only exists because a Hermes Desktop plugin cannot read the filesystem or probe pids.
"Thinking" state is NOT computed here: the plugin reads it from the same profiles.list roster
data Desktop's own "Active now" uses (see docs/FINDINGS.md).

    pythonw.exe crew_status.py [--home F:\\Hermes\\data] [--port 8094]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import sqlite3
import sys
import time
import urllib.request
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Optional

DEFAULT_HOME = Path(os.environ.get("HERMES_HOME") or r"F:\Hermes\data")
DEFAULT_PORT = 8094
LADDER_URL = "http://127.0.0.1:8093/"
GPU_LOCK = Path(r"F:\Hermes\tools\studio-control\logs\gpu_job_active.json")

# psutil create_time * 100, rounded (gateway.status._get_process_start_time). Same FILETIME source
# as our ctypes read, so equality holds to rounding; 2 centiseconds of slack.
START_TOLERANCE_CS = 2
# With no recorded host_start_time, a live pid born well after the job started is a reused pid.
BIRTH_SLACK_S = 30


# --- process identity -------------------------------------------------------------------------

@dataclass(frozen=True)
class Proc:
    alive: bool
    birth: Optional[float] = None  # epoch seconds; None when unreadable


Probe = Callable[[int], Proc]


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

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = wintypes.HANDLE
    h = k32.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        # Access denied still means it exists; anything else means it does not.
        return Proc(ctypes.get_last_error() == 5)
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


def classify(command: str) -> dict:
    """Friendly {kind, label} for a background command line."""
    cmd = command or ""
    low = cmd.lower().replace("\\", "/")
    if "bot_mode_dm.py" in low and "--run-delivery" in low:
        m = _AUTHOR_NAME.search(cmd)
        who = m.group(1) if m else "a teammate"
        return {"kind": "dm", "label": f"receiving message from {who}"}
    if "local_review.py" in low:
        return {"kind": "review", "label": "desloppify review"}
    if "research.py" in low:
        return {"kind": "research", "label": "deep research"}
    if "unity" in low and "test" in low:
        return {"kind": "unity", "label": "Unity tests"}
    if "gpu_job.py" in low:
        m = _APP.search(cmd)
        return {"kind": "gpu", "label": f"GPU job ({m.group(1)})" if m else "GPU job"}
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
    """{assignee: {done, total}} for tasks that are open, or finished within the last day.

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
        if status != "done" and len(t["items"]) < 6:  # open tasks, for the hover card
            t["items"].append({"status": status, "title": " ".join(str(title or "").split())[:80]})
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


def collect_jobs(home: Path, names: list, now: float, probe: Probe = probe_pid) -> dict:
    """{bot name: [live job, ...]} across every profile's processes.json, de-duplicated."""
    known, out, seen = set(names), {n: [] for n in names}, set()
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
            cls = classify(str(e.get("command", "")))
            progress = review_progress(str(e.get("command", "")), e.get("cwd")) if cls["kind"] == "review" else None
            out[job_owner(e, file_profile, cls["kind"], known)].append({
                **cls,
                "progress": progress,
                "pid": e.get("pid"),
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


def read_gpu(probe: Probe = probe_pid, lock_path: Path = GPU_LOCK, ladder_url: str = LADDER_URL) -> dict:
    lock = None
    try:
        data = json.loads(lock_path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and job_is_live({"pid": data.get("pid")}, probe):
            lock = data
    except (OSError, ValueError):
        pass
    ladder = _get_json(ladder_url)
    return {**gpu_label(ladder, lock), "ladder": ladder, "lock": lock}


def snapshot(home: Path = DEFAULT_HOME, now: Optional[float] = None, probe: Probe = probe_pid) -> dict:
    now = time.time() if now is None else now
    bots = list_bots(home)
    jobs = collect_jobs(home, [b["name"] for b in bots], now, probe)
    tasks = kanban_progress(home / "kanban.db", now)
    bots = [{**b, "jobs": jobs[b["name"]], "tasks": tasks.get(b["name"])} for b in bots]
    return {"ts": now, "gpu": read_gpu(probe), "bots": bots}


# --- http -------------------------------------------------------------------------------------

def make_handler(home: Path):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if self.path.split("?")[0] != "/crew":
                self.send_error(404)
                return
            try:
                body = json.dumps(snapshot(home)).encode("utf-8")
                self.send_response(200)
            except Exception as exc:  # never crash the poll loop
                body = json.dumps({"error": str(exc)}).encode("utf-8")
                self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            # Desktop's renderer origin is not 127.0.0.1; data is read-only local status.
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            pass

    return Handler


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--home", type=Path, default=DEFAULT_HOME)
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    a = ap.parse_args(argv)
    ThreadingHTTPServer(("127.0.0.1", a.port), make_handler(a.home)).serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
