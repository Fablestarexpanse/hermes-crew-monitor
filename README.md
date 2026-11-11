# Hermes Crew Monitor

A Hermes Desktop panel beside the chat showing which bots are **thinking**, running a **background
job** (deep research, desloppify review, Unity tests, renders...) or **idle**, with elapsed time, plus
a GPU line (Fit Ladder context / VRAM, or who holds the GPU).

Why: bots start long jobs as background processes and end their turn, so the built-in "Active now"
strip shows them idle while the job still runs.

## What you see
- Top block: `Ladder 256K local · 21.5/24.0 GB`, `held by gpu_job comfyui`, or `cloud fallback`; a VRAM
  bar; GPU utilisation and temperature (when `nvidia-smi` is present); and who is waiting for the GPU.
- One row per bot with a coloured dot: pulsing = thinking, solid = background job (label + elapsed
  such as `desloppify review  1h 02m`), dim = idle, hollow = unknown (roster unreachable), amber =
  **waiting for you** (see Limits).
- Progress next to the name: review batches with an estimate (`7/20 35%`, `~42m left`), kanban tasks
  (`tasks 3/6 50%`, `1 blocked`). A job whose progress has not moved for 15 minutes reads `stalled`.
- **Recently finished**: the last jobs that ended, with how long they ran.
- Notifications: a job finished, a job stalled, a bot started waiting for you.
- `hide idle` in the panel. Click a row to open that bot's Bot Chat. The status-bar chip `crew N`
  (N = busy bots; hover it for who is doing what) toggles the panel.
- Command palette: **Crew Monitor: toggle panel**, **show or hide idle bots**, **toggle job
  notifications**, **log roster fields**.
- If the status service misses a few reads the panel says so and still shows thinking state.

## Screenshots
Rendered from the real `plugin/plugin.js` with invented data (no real sessions). The left list is a mock of
the core Bots rows; the plugin only styles them, it does not draw them. The page that makes them is
`docs/demo/index.html` (serve the repo root and open it).

![Dark: busy bots pulse in their colour; badges show 7/20 batches and tasks 3/6; GPU bar, queue and recently finished jobs](docs/img/overview-dark.png)

![Light theme](docs/img/overview-light.png)

Hover a row for details: job start, pid, folder and command line.

![Hover card on a running review](docs/img/hover-reviewer-dark.png)

Bots with open kanban tasks list them.

![Hover card with kanban tasks](docs/img/hover-scout-light.png)

## Install
```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File install.ps1
```
- Copies `plugin/plugin.js` to `<HermesHome>\desktop-plugins\crew-monitor\`.
- Copies the service to `%LOCALAPPDATA%\HermesCrewMonitor\` (so this checkout can move or be deleted)
  and registers the per-user logon task `HermesCrewMonitor` (limited rights, stdlib Python,
  `127.0.0.1` only), starts it and checks `/health` and `/crew`.
- Then restart Hermes Desktop.

Options: `-HermesHome F:\Hermes\data`, `-Port 8094` (the installed plugin copy is pointed at the same
port), `-Python <pythonw.exe>` (default: the newest Python under `<HermesHome>\tools`, then PATH),
`-AllowOrigin <origin>` (see Safety).

## Settings
`%LOCALAPPDATA%\HermesCrewMonitor\crew-monitor.json`, re-read when it changes (no restart):
```json
{
  "stall_minutes": 15,
  "colors": { "scout": "#00ff88" },
  "rules": [
    { "match": "blender(\\.exe)?\\s+-b\\s+(\\S+)", "kind": "render", "label": "Blender render (\\2)" }
  ]
}
```
`rules` are regular expressions tried against a job's command line before the built-in labels;
`colors` override the built-in bot colours.

## Troubleshooting
- Log: `%LOCALAPPDATA%\HermesCrewMonitor\crew-monitor.log`.
- `http://127.0.0.1:8094/health` shows the version, uptime and which page origins have connected.
- "Status service is version ..." in the panel: plugin and service are from different installs;
  re-run `install.ps1`.

## Uninstall
```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File install.ps1 -Uninstall
```
Your `crew-monitor.json` is kept.

## Tests
`npm test` (JS logic with a stubbed SDK, then `python -m unittest discover -s service`). CI runs both
on Windows and Linux.

## Safety
- Read-only: writes no Hermes file; listens on 127.0.0.1 only; the only outbound calls are to the
  Ladder on 127.0.0.1 and `nvidia-smi`.
- Web pages cannot read it: requests whose `Origin` is an ordinary web site, or whose `Host` is not
  loopback (DNS rebinding), get 403. App shells (`app://`, `file://`, `null`) and loopback pages pass.
  `null` is also what a sandboxed iframe on a web page sends, so to close that too, look up Desktop's
  origin in `/health` (`origins_seen`) and re-run `install.ps1 -AllowOrigin <that origin>`.

## Limits
- **Waiting for you** depends on a field in Desktop's `profiles.list` rows that the SDK does not
  document. The plugin recognises the likely spellings (`pending_approval`, `needs_input`, a status
  like `awaiting_input`, ...). If it never lights up, run **Crew Monitor: log roster fields** and
  look in the developer console for the real field name.
- "Waiting for the GPU" is inferred: a live `gpu_job` that is not the lock holder.
- Estimates and "stalled" exist only for jobs with countable progress (desloppify batches).
- Finished-job history is in memory: it restarts with the service, and exit codes are not known.
- There is no click-through to the kanban board (no host API for it was found).

Design notes: `docs/FINDINGS.md`.
