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

## Not done / limits
- Plugin UI was exercised only through pure-logic tests with a stubbed SDK; real rendering needs
  Hermes Desktop restarted (see README).
- Colour dots use the fixed per-bot colours from the brief; everything else uses `--ui-*` variables.
