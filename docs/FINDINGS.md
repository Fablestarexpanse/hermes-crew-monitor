# Findings and decisions

Durable record of what was measured and why things are built the way they are.

## SDK capabilities (desktop-plugin-sdk.md)
- A disk plugin is one uncompiled ESM file (`desktop-plugins/<id>/plugin.js`); imports limited to
  `@hermes/plugin-sdk`, `react`, `react/jsx-runtime`. No JSX, so no build step: "packaging" is a file copy.
- It has no documented filesystem, process-probe or spawn API. `ctx.rest` needs a Python `plugin_api.py`
  inside the gateway plus an entry in `plugins.enabled` in config.yaml: heavier and touches Hermes
  config. **Decision: companion service on 127.0.0.1:8094**, as the brief said.
- No CSP found in apps/desktop (only embed-host and local-preview), so `fetch` to 127.0.0.1 is allowed;
  the service sends `Access-Control-Allow-Origin: *` (read-only local data).
- Surfaces used: `panes` (placement right, 280px), `STATUSBAR_AREAS.right`, `PALETTE_AREA`,
  `ctx.setInterval`, `ctx.storage`, `ctx.onDispose`, `host.request('profiles.list')`, `host.state.busy`,
  `host.state.focusedSessionProfile`, `host.openSession`, `host.notify`.
- Closing the only pane a plugin contributes disables the plugin. So the palette/status-bar toggle
  unregisters/re-registers the pane itself (state in `ctx.storage` key `open`) rather than hiding it.

## How Desktop decides "Active now" (apps/desktop/src/plugins/hermes-bots/row-helpers.ts)
- focused live turn: `host.state.busy` AND `focusedSessionProfile` is that bot;
- wrote in the last 90 s: `profiles.list` row `last_session.last_active` (ACTIVE_WINDOW_S);
- worker heartbeat: row `worker_session.last_active` within 150 s (WORKER_ACTIVE_WINDOW_S).
`profiles.list` is exposed to plugins via `host.request`, so the plugin reuses exactly that source.
**Decision: no state.db reading anywhere.** The fallback in the brief was not needed.
Bot Chat id for a row click: `canonical_session.resolved_id ?? .id` -> `host.openSession(id, {profile})`.

## processes.json
- `host_start_time` = `round(psutil.create_time() * 100)` (gateway/status.py). Hermes' Python has no
  psutil, so the service reads the same FILETIME with ctypes `GetProcessTimes` and compares within
  2 centiseconds. Same degrade as Hermes `_host_pid_is_ours`: alive but birth unreadable -> still shown.
- Measured 2026-10-30: reviewer job pid 48500 `host_start_time` 179338147372 vs started_at
  1793381473.648 (+72 ms), and the live pid verified through the service.
- **Files are not a reliable owner.** foreman's processes.json held the *reviewer's* desloppify job
  (session_key `agent:reviewer:...`), and reviewer's held a foreman DM delivery. Rule: DM deliveries
  stay with the file they were written to (the receiver); every other job belongs to the profile named
  in `session_key`; jobs are de-duplicated by (pid, host_start_time).
- Non-`host` pid_scope entries are hidden (cannot be verified).

## GPU row
Ladder status JSON at :8093 (`mode` local/cloud/held, `ctx`, `vram_*_mib`, `held_by`); gpu_job lock
`studio-control/logs/gpu_job_active.json` (`pid`, `app`) counts only while its pid is alive.
A live lock beats the ladder's own mode.

## Service hardening (v0.2)
- `Access-Control-Allow-Origin: *` let any web page read bot names, task titles and command lines.
  Now: `Origin` is echoed only when allowed (ordinary http(s) sites are refused with 403), and `Host`
  must be loopback. Desktop's renderer origin is not known from here, so non-web origins are allowed
  by default and `--allow-origin` pins it; `/health` lists the origins seen.
- OpenProcess "access denied" used to count as alive. A job Hermes starts as this user can always be
  opened with PROCESS_QUERY_LIMITED_INFORMATION, so denied means a recycled pid (a protected process):
  now hidden. This differs from Hermes' `_host_pid_is_ours` on purpose.
- The ladder call (1 s timeout when it is down) and `nvidia-smi` run on their own thread; `/crew`
  serves a snapshot cached for 1 s. A worker refreshes every 2 s while someone is watching and every
  10 s otherwise, which is what lets it notice jobs finishing with Desktop closed.
- `/events` pushes the payload when it changes. Fields that tick (ts, elapsed) are left out of the
  comparison, and the estimate is an absolute time fixed at the last progress change, for that reason.
- A job that vanishes from processes.json is re-probed before it is called finished: the file can be
  read mid-write, and Hermes can prune an entry while the process runs on.
- The plugin keeps the last payload until 3 reads in a row fail, so one slow read no longer blanks it.
- Windows: `allow_reuse_address` is off, otherwise a second service binds the same port silently.

## Local instances and control (v0.3)
- "Up" is decided by a listening TCP port (`netstat -ano`, which also gives the pid) or, without a
  port, by image name (`tasklist`). Listening rows are recognised by their empty remote end, because
  the state word is translated on non-English Windows.
- Start/stop breaks the read-only rule, so it is opt-in (`--control` + a token file) and limited to
  instances in the settings file. The origin gate alone is not enough for a request that changes
  something: a sandboxed iframe sends `Origin: null`, which the gate lets through. Hence the secret
  in an `X-Crew-Token` header, which also forces a CORS preflight.
- Started programs are asked to break away from the scheduled task's job object so re-installing the
  monitor does not end them; if Windows refuses, they start inside it and do end with the service.
- Stop is `taskkill /T /F` on the port owner and on the process this service launched: no graceful
  shutdown. A `stop` command in the settings runs first for programs that have one.
- Verified: unit tests, a real start/stop of a throwaway server on Windows and Linux in CI, and the
  buttons in the demo page. Not verified: a real ComfyUI/Ollama, and the buttons inside Desktop.

## Not done / limits
- Plugin UI was exercised only through pure-logic tests with a stubbed SDK; real rendering needs
  Hermes Desktop restarted (see README).
- Colour dots use the per-bot colours from the brief unless `crew-monitor.json` overrides them; amber
  (#E8A317) marks things needing attention; everything else uses `--ui-*` variables.
- v0.2 was tested on Linux only (unit tests, the service process over HTTP, the demo page in a
  browser). Not yet run: `install.ps1`, the ctypes probe change, `nvidia-smi` on Windows, and the
  plugin inside Hermes Desktop (EventSource from its renderer, `host.notify` toasts).
