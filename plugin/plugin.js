// Crew Monitor: a live panel beside the chat showing which Hermes bots are working.
//
// Sources (see docs/FINDINGS.md):
//  - "thinking": the same profiles.list roster rows Desktop's own "Active now" reads
//    (focused live turn, last_session written in the last 90 s, worker heartbeat in 150 s).
//  - background jobs, finished jobs, kanban progress, GPU line: the read-only status service on
//    127.0.0.1 (a plugin cannot read files or probe pids itself). Pushed over /events (server-sent
//    events); polled from /crew when that is unavailable.
import { PALETTE_AREA, STATUSBAR_AREAS, Tip, atom, host, useValue } from '@hermes/plugin-sdk'
import { jsx, jsxs } from 'react/jsx-runtime'

const ID = 'crew-monitor'
const SERVICE_PORT = 8094 // install.ps1 -Port rewrites this line in the installed copy
const BASE = `http://127.0.0.1:${SERVICE_PORT}`
const API = 2 // crew_status.py API: the /crew payload shape this file expects
const STYLE_ID = 'crew-monitor-style'
const ACTIVE_WINDOW_S = 90 // row-helpers.ts ACTIVE_WINDOW_S
const WORKER_WINDOW_S = 150 // row-helpers.ts WORKER_ACTIVE_WINDOW_S
const SERVICE_POLL_MS = 3000
const ROSTER_POLL_MS = 5000
const HIDDEN_POLL_MS = 15000 // window hidden: poll slowly, only so notifications still arrive
const MISS_LIMIT = 3 // failed reads in a row before the panel says the service is offline
const SSE_RETRY_MS = 30000
const SSE_SILENCE_MS = 40000 // the service pings every 15 s
const FINISHED_SHOWN = 5
const AMBER = '#E8A317' // needs attention: waiting for you, stalled, blocked

// Console-skin colours, keyed by profile name.
const COLORS = {
  foreman: '#FFD700',
  researcher: '#2BB3A3',
  scout: '#2BB3A3',
  'design-partner': '#4169E1',
  'senior-dev': '#4169E1',
  'editor-integrator': '#4169E1',
  reviewer: '#DC143C',
  librarian: '#9AA0A6',
  'trailer-bot': '#E8661E',
  'asset-bot': '#E8661E',
  gatekeeper: '#C9D1D9',
}
const FALLBACK_COLOR = 'var(--ui-accent)'

// ---- pure logic (exported for tests) ---------------------------------------------------------

export function fmtElapsed(seconds) {
  if (seconds == null || !Number.isFinite(seconds)) return ''
  const s = Math.max(0, Math.floor(seconds))
  if (s < 60) return `${s}s`
  const m = Math.floor(s / 60)
  if (s < 3600) return `${m}m ${String(s % 60).padStart(2, '0')}s`
  return `${Math.floor(m / 60)}h ${String(m % 60).padStart(2, '0')}m`
}

export function ago(seconds) {
  return seconds == null ? '' : `${fmtElapsed(seconds)} ago`
}

/** "~42m left" from an absolute finish estimate (epoch seconds). */
export function fmtEta(etaAt, nowS) {
  if (!etaAt) return ''
  const left = etaAt - nowS
  if (left <= 0) return 'overdue'
  if (left < 60) return '<1m left'
  const m = Math.round(left / 60)
  return m < 60 ? `~${m}m left` : `~${Math.floor(m / 60)}h ${String(m % 60).padStart(2, '0')}m left`
}

const HEX = /^#(?:[0-9a-f]{3,4}|[0-9a-f]{6}|[0-9a-f]{8})$/i
const colorOf = b => (typeof b.color === 'string' && HEX.test(b.color) ? b.color : COLORS[b.name] || FALLBACK_COLOR)

// "Waiting for you": the SDK does not document such a field on profiles.list rows, so this looks for
// the plausible spellings on the row and its sessions. With none present it is simply never true.
// The palette command "Crew Monitor: log roster fields" shows what the rows really carry.
const WAITING_FLAGS = ['awaiting_user', 'awaiting_input', 'awaiting_approval', 'pending_approval', 'pending_approvals', 'pending_question', 'needs_input', 'needs_approval', 'waiting_for_user']
const WAITING_STATUS = /^(awaiting|waiting|needs|pending)[_ -]?(for[_ -])?(user|input|approval|reply|answer|confirmation)$/i
const truthy = v => (Array.isArray(v) ? v.length > 0 : Boolean(v))
export function waitingOf(row) {
  if (!row) return false
  for (const o of [row, row.last_session, row.canonical_session, row.worker_session]) {
    if (!o || typeof o !== 'object') continue
    if (WAITING_FLAGS.some(k => truthy(o[k]))) return true
    if ([o.status, o.state].some(v => typeof v === 'string' && WAITING_STATUS.test(v))) return true
  }
  return false
}

/**
 * Merge service data (jobs, titles), roster rows (liveness) and the focused turn into display rows.
 * state: 'waiting' | 'thinking' | 'job' | 'idle' | 'offline'. A thinking bot keeps its jobs listed.
 */
export function deriveRows({ crew, roster, focusedProfile, busy, now }) {
  const nowS = now / 1000
  const byName = new Map()
  for (const b of (crew && crew.bots) || []) byName.set(b.name, { name: b.name, title: b.title, color: b.color, jobs: b.jobs || [], tasks: b.tasks })
  for (const r of roster || []) {
    if (!r || !r.name) continue
    const hit = byName.get(r.name) || { name: r.name, title: r.name, jobs: [] }
    const metaTitle = r.ui_meta && r.ui_meta['hermes-bots'] && r.ui_meta['hermes-bots'].title
    byName.set(r.name, { ...hit, title: metaTitle || hit.title, row: r })
  }
  const rosterKnown = Array.isArray(roster)
  return [...byName.values()]
    .map(b => {
      const row = b.row
      const lastWrite = (row && row.last_session && row.last_session.last_active) || 0
      const worker = (row && row.worker_session && row.worker_session.last_active) || 0
      const focusedBusy = Boolean(busy && focusedProfile && focusedProfile === b.name)
      const thinking = focusedBusy || Boolean(lastWrite && nowS - lastWrite < ACTIVE_WINDOW_S)
      const hasJob = b.jobs.length > 0 || Boolean(worker && nowS - worker < WORKER_WINDOW_S)
      let state = 'idle'
      if (waitingOf(row)) state = 'waiting'
      else if (thinking) state = 'thinking'
      else if (hasJob) state = 'job'
      else if (!rosterKnown) state = 'offline' // cannot tell if it is thinking
      const jobs = b.jobs.map(j => ({
        id: j.id,
        label: j.label,
        progress: j.progress,
        elapsed: j.started_at ? nowS - j.started_at : j.elapsed_s,
        startedAt: j.started_at,
        pid: j.pid,
        cwd: j.cwd,
        command: j.command,
        waitingGpu: Boolean(j.waiting_gpu),
        stalledFor: j.progress && j.progress.stalled ? nowS - j.progress.changed_at : null,
      }))
      // A worker heartbeat with no process-registry entry (kanban/tool worker) still deserves a line.
      if (!jobs.length && state === 'job') jobs.push({ label: 'working in background', elapsed: null })
      return {
        name: b.name,
        title: b.title,
        color: colorOf(b),
        state,
        stalled: jobs.some(j => j.stalledFor != null),
        jobs,
        progress: progressOf(jobs, b.tasks, nowS),
        lastWrite: lastWrite || null,
        canonical: row && row.canonical_session,
      }
    })
    .sort((a, c) => rank(a.state) - rank(c.state) || a.title.localeCompare(c.title))
}

const pct = (done, total) => `${Math.round((100 * done) / total)}%`

// Two independent readouts: the running job's own progress (desloppify batches, "2/20 10%") and the
// bot's kanban tasks ("tasks 3/5 60%"). `badge` is what goes next to the name.
export function progressOf(jobs, tasks, nowS) {
  const j = jobs.find(x => x.progress && x.progress.total)
  const batches = j
    ? {
        text: `${j.progress.done}/${j.progress.total} ${pct(j.progress.done, j.progress.total)}`,
        what: j.progress.unit || 'steps',
        eta: nowS == null ? '' : fmtEta(j.progress.eta_at, nowS),
      }
    : null
  const kanban =
    tasks && tasks.total
      ? { text: `tasks ${tasks.done}/${tasks.total} ${pct(tasks.done, tasks.total)}`, items: tasks.items || [], blocked: tasks.blocked || 0 }
      : null
  if (!batches && !kanban) return null
  return { batches, tasks: kanban, badge: [batches && batches.text, kanban && kanban.text].filter(Boolean).join(' · ') }
}

const rank = s => ({ waiting: 0, thinking: 1, job: 2, idle: 3, offline: 4 })[s]
const isBusy = r => r.state === 'waiting' || r.state === 'thinking' || r.state === 'job'

/**
 * What to tell the user about a new service payload: jobs that finished, jobs that stopped making
 * progress. `seen` (a Set) remembers what was already said; `quiet` records without returning
 * anything (first payload after load: old news).
 */
export function crewEvents(crew, seen, quiet) {
  const out = []
  const say = (key, text) => {
    if (seen.has(key)) return
    seen.add(key)
    if (!quiet) out.push(text)
  }
  for (const f of crew.finished || []) {
    if (f.kind !== 'dm') say(`f:${f.id}:${f.ended_at}`, `${f.title} finished ${f.label} after ${fmtElapsed(f.duration_s)}`)
  }
  const stalled = new Set()
  for (const b of crew.bots || []) {
    for (const j of b.jobs || []) {
      if (!j.progress || !j.progress.stalled) continue
      stalled.add(`s:${j.id}`)
      say(`s:${j.id}`, `${b.title}: ${j.label} has made no progress for ${fmtElapsed(crew.ts - j.progress.changed_at)}`)
    }
  }
  for (const k of [...seen]) if (k.startsWith('s:') && !stalled.has(k)) seen.delete(k) // may stall again later
  return out
}

/** Recently finished jobs for the panel, newest first; DM deliveries are too frequent to list. */
export function finishedView(crew, nowS, max = FINISHED_SHOWN) {
  return ((crew && crew.finished) || [])
    .filter(f => f.kind !== 'dm')
    .slice(0, max)
    .map(f => ({ key: `${f.id}:${f.ended_at}`, bot: f.bot, text: `${f.title} · ${f.label}`, when: `${fmtElapsed(f.duration_s)} · ${ago(Math.max(0, nowS - f.ended_at))}` }))
}

const gb = mib => (mib / 1024).toFixed(1)
/** GPU row extras: VRAM fill fraction, a "62% util · 71°C" line, who is waiting for the GPU. */
export function gpuView(gpu) {
  const used = gpu.vram_used_mib
  const total = gpu.vram_total_mib
  const has = used != null && total > 0
  const meta = [
    has && !/GB/.test(gpu.label || '') ? `VRAM ${gb(used)}/${gb(total)} GB` : null,
    gpu.util_pct != null ? `${gpu.util_pct}% util` : null,
    gpu.temp_c != null ? `${gpu.temp_c}°C` : null,
  ].filter(Boolean)
  const queue = (gpu.queue || []).map(q => q.title)
  return { frac: has ? Math.min(1, Math.max(0, used / total)) : null, meta: meta.join(' · '), queue: queue.length ? `waiting for GPU: ${queue.join(', ')}` : '' }
}

/** Tooltip for the status-bar chip: one line per busy bot. */
export function chipSummary(rows) {
  const lines = rows.filter(isBusy).map(r => {
    const what = r.state === 'waiting' ? 'waiting for you' : r.jobs.length ? r.jobs.map(j => j.label).join(', ') : 'thinking'
    return `${r.title}: ${what}${r.stalled ? ' (stalled)' : ''}`
  })
  return lines.length ? lines.join('\n') : 'Crew Monitor: everyone is idle'
}

// ---- state -----------------------------------------------------------------------------------

const $crew = atom(null) // service payload, null = unreachable
const $roster = atom(null) // profiles.list rows, null = not loaded
const $view = atom({ rows: [], now: Date.now() }) // derived once per tick, shared by pane, chip and row CSS
const $hideIdle = atom(false)
let open = true
let paneDispose = null
let rowsStyle = null
let notifyOn = true
const seenEvents = new Set()
let eventsPrimed = false
let waitingNames = null // bots already announced as waiting; null until the first roster

const hidden = () => typeof document !== 'undefined' && document.hidden
const focusedProfile = () => {
  const a = host.state.focusedSessionProfile || host.state.profile
  return a ? a.get() : null
}

function say(message) {
  if (notifyOn) host.notify({ kind: 'info', message })
}

function recompute() {
  const now = Date.now()
  const rows = deriveRows({ crew: $crew.get(), roster: $roster.get(), focusedProfile: focusedProfile(), busy: host.state.busy.get(), now })
  $view.set({ rows, now })
  if (rowsStyle) {
    const css = rowCss(rows)
    if (rowsStyle.textContent !== css) rowsStyle.textContent = css
  }
  if ($roster.get()) {
    const waiting = new Set(rows.filter(r => r.state === 'waiting').map(r => r.name))
    if (waitingNames) for (const r of rows) if (waiting.has(r.name) && !waitingNames.has(r.name)) say(`${r.title} is waiting for you`)
    waitingNames = waiting
  }
}

function acceptCrew(data) {
  const news = crewEvents(data, seenEvents, !eventsPrimed)
  eventsPrimed = true
  $crew.set(data)
  recompute()
  news.forEach(say)
}

async function fetchCrew() {
  const ctl = new AbortController()
  const t = setTimeout(() => ctl.abort(), 2500)
  try {
    const res = await fetch(`${BASE}/crew`, { signal: ctl.signal, cache: 'no-store' })
    return res.ok ? await res.json() : null
  } catch {
    return null
  } finally {
    clearTimeout(t)
  }
}

// Push when possible (EventSource on /events), poll otherwise. One bad read is not "offline":
// the last good payload stays until MISS_LIMIT reads in a row have failed.
function startFeed(ctx) {
  let es = null
  let misses = 0
  let retryAt = 0
  let lastEvent = 0
  let lastPoll = 0
  const ok = data => {
    misses = 0
    lastEvent = Date.now()
    acceptCrew(data)
  }
  const miss = () => {
    if (++misses >= MISS_LIMIT && $crew.get() !== null) {
      $crew.set(null)
      recompute()
    }
  }
  const drop = () => {
    if (es) es.close()
    es = null
    retryAt = Date.now() + SSE_RETRY_MS
  }
  const connect = () => {
    if (es || typeof EventSource !== 'function' || Date.now() < retryAt) return
    es = new EventSource(`${BASE}/events`)
    lastEvent = Date.now()
    es.onmessage = e => {
      try {
        ok(JSON.parse(e.data))
      } catch {
        // a malformed frame is not worth dropping the stream for
      }
    }
    es.addEventListener('ping', () => {
      lastEvent = Date.now()
    })
    es.onerror = () => {
      drop()
      miss()
    }
  }
  const tick = () => {
    const now = Date.now()
    if (es) {
      if (now - lastEvent > SSE_SILENCE_MS) {
        drop()
        miss()
      }
      return
    }
    if (hidden() && now - lastPoll < HIDDEN_POLL_MS) return
    lastPoll = now
    fetchCrew().then(data => {
      if (data) {
        ok(data)
        connect()
      } else {
        miss()
      }
    })
  }
  tick()
  ctx.setInterval(tick, SERVICE_POLL_MS)
  ctx.onDispose(() => es && es.close())
}

let lastRoster = 0
async function pollRoster(force) {
  const now = Date.now()
  if (!force && hidden() && now - lastRoster < HIDDEN_POLL_MS) return
  lastRoster = now
  try {
    const res = await host.request('profiles.list', {})
    $roster.set((res && res.profiles) || [])
  } catch {
    $roster.set(null)
  }
  recompute()
}

// ---- UI --------------------------------------------------------------------------------------

function Dot({ color, state }) {
  const hollow = state === 'offline'
  return jsx('span', {
    'aria-hidden': 'true',
    className: state === 'thinking' || state === 'waiting' ? 'crew-dot crew-pulse' : 'crew-dot',
    style: {
      background: hollow ? 'transparent' : color,
      border: `1.5px solid ${color}`,
      opacity: state === 'idle' ? 0.35 : hollow ? 0.6 : 1,
    },
  })
}

function openBotChat(bot) {
  const c = bot.canonical
  const sid = c && (c.resolved_id || c.id)
  if (!sid || typeof host.openSession !== 'function') {
    host.notify({ kind: 'info', message: `${bot.title} has no Bot Chat yet.` })
    return
  }
  Promise.resolve(host.openSession(sid, { profile: bot.name })).catch(err =>
    host.notify({ kind: 'error', message: (err && err.message) || 'Could not open Bot Chat.' })
  )
}

const DIM = { color: 'var(--ui-text-tertiary)' }
const WARN = { color: AMBER }
const STATE_TEXT = {
  waiting: 'waiting for you',
  thinking: 'thinking now',
  job: 'running a background job',
  idle: 'idle',
  offline: 'status unknown (roster unreachable)',
}

function Hover({ bot, now }) {
  const p = bot.progress
  return jsxs('div', {
    className: 'crew-tip',
    children: [
      jsxs('div', { children: [jsx('b', { children: bot.title }), jsx('span', { style: DIM, children: ` · ${bot.name}` })] }),
      jsx('div', { style: bot.state === 'waiting' ? WARN : DIM, children: STATE_TEXT[bot.state] }),
      p && p.batches ? jsx('div', { style: DIM, children: [`${p.batches.text} ${p.batches.what}`, p.batches.eta].filter(Boolean).join(' · ') }) : null,
      p && p.tasks
        ? jsxs('div', { className: 'crew-tip-job', children: [
            jsx('div', { children: p.tasks.text + (p.tasks.blocked ? ` · ${p.tasks.blocked} blocked` : '') }),
            ...p.tasks.items.map((t, i) => jsx('div', { style: t.status === 'blocked' ? WARN : DIM, children: `${t.status} · ${t.title}` }, i)),
          ] })
        : null,
      bot.lastWrite ? jsx('div', { style: DIM, children: `last wrote ${ago(now / 1000 - bot.lastWrite)}` }) : null,
      ...bot.jobs.map((j, i) =>
        jsxs('div', { className: 'crew-tip-job', children: [
          jsx('div', { children: j.label + (j.elapsed != null ? ` · ${fmtElapsed(j.elapsed)}` : '') }),
          j.waitingGpu ? jsx('div', { style: WARN, children: 'waiting for the GPU' }) : null,
          j.stalledFor != null ? jsx('div', { style: WARN, children: `no progress for ${fmtElapsed(j.stalledFor)}` }) : null,
          j.startedAt ? jsx('div', { style: DIM, children: `started ${new Date(j.startedAt * 1000).toLocaleTimeString()}${j.pid ? ` · pid ${j.pid}` : ''}` }) : null,
          j.cwd ? jsx('div', { style: DIM, className: 'crew-tip-mono', children: j.cwd }) : null,
          j.command ? jsx('div', { style: DIM, className: 'crew-tip-mono', children: j.command }) : null,
        ] }, j.id || i)
      ),
    ],
  })
}

function BotRow({ bot, now }) {
  const color = bot.state === 'waiting' ? AMBER : bot.color
  const dim = bot.state === 'idle' || bot.state === 'offline'
  const status = bot.state === 'waiting' ? 'waiting for you' : bot.stalled ? 'stalled' : bot.state === 'job' ? '' : bot.state
  const p = bot.progress
  return jsx(Tip, { label: jsx(Hover, { bot, now }), side: 'left', className: 'max-w-96', children: jsxs('button', {
    type: 'button',
    className: `crew-row crew-${bot.state}`,
    style: { '--crew-c': color },
    'aria-label': `Open ${bot.title}`,
    onClick: () => openBotChat(bot),
    children: [
      jsx(Dot, { color, state: bot.state }),
      jsxs('span', {
        className: 'crew-main',
        children: [
          jsxs('span', {
            className: 'crew-line',
            children: [
              jsx('span', {
                className: 'crew-title',
                style: { color: dim ? 'var(--ui-text-tertiary)' : 'var(--ui-text-secondary)' },
                children: bot.title,
              }),
              p ? jsx('span', { className: 'crew-progress', title: 'progress', children: p.badge }) : null,
              status
                ? jsx('span', { className: 'crew-state', style: bot.state === 'waiting' || bot.stalled ? WARN : DIM, children: status })
                : null,
            ],
          }),
          p && ((p.batches && p.batches.eta) || (p.tasks && p.tasks.blocked))
            ? jsxs('span', { className: 'crew-job', children: [
                jsx('span', { style: DIM, children: (p.batches && p.batches.eta) || '' }),
                p.tasks && p.tasks.blocked ? jsx('span', { style: WARN, children: `${p.tasks.blocked} blocked` }) : null,
              ] })
            : null,
          ...bot.jobs.map((j, i) =>
            jsxs(
              'span',
              {
                className: 'crew-job',
                style: DIM,
                children: [
                  jsx('span', { className: 'crew-job-label', children: j.waitingGpu ? `${j.label} · waiting for GPU` : j.label }),
                  j.elapsed != null ? jsx('span', { className: 'crew-job-time', children: fmtElapsed(j.elapsed) }) : null,
                ],
              },
              j.id || i
            )
          ),
        ],
      }),
    ],
  }) })
}

function GpuRow({ gpu }) {
  if (!gpu || gpu.state === 'none') return null // 'none': no ladder, no gpu_job lock, no nvidia-smi
  const color = gpu.state === 'unknown' ? 'var(--ui-text-quaternary)' : 'var(--ui-accent)'
  const v = gpuView(gpu)
  return jsxs('div', {
    className: 'crew-gpu',
    style: { color: 'var(--ui-text-secondary)', borderBottom: '1px solid var(--ui-stroke-secondary)' },
    title: 'Local GPU',
    children: [
      jsxs('div', { className: 'crew-gpu-line', children: [jsx(Dot, { color, state: gpu.state === 'held' ? 'job' : 'local' }), jsx('span', { children: gpu.label })] }),
      v.frac != null
        ? jsx('div', { className: 'crew-bar', role: 'img', 'aria-label': `VRAM ${Math.round(v.frac * 100)}% used`, children: jsx('div', { className: 'crew-bar-fill', style: { width: `${(v.frac * 100).toFixed(1)}%`, background: v.frac > 0.92 ? AMBER : 'var(--ui-accent)' } }) })
        : null,
      v.meta ? jsx('div', { className: 'crew-gpu-meta', style: DIM, children: v.meta }) : null,
      v.queue ? jsx('div', { className: 'crew-gpu-meta', style: WARN, children: v.queue }) : null,
    ],
  })
}

function Finished({ crew, now }) {
  const items = finishedView(crew, now / 1000)
  if (!items.length) return null
  return jsxs('div', {
    className: 'crew-finished',
    style: { borderTop: '1px solid var(--ui-stroke-secondary)' },
    children: [
      jsx('div', { className: 'crew-head', style: { color: 'var(--ui-text-quaternary)' }, children: 'RECENTLY FINISHED' }),
      ...items.map(f =>
        jsxs('div', { className: 'crew-job', style: DIM, children: [jsx('span', { className: 'crew-job-label', children: f.text }), jsx('span', { className: 'crew-job-time', children: f.when })] }, f.key)
      ),
    ],
  })
}

function CrewPane({ onToggleIdle }) {
  const crew = useValue($crew)
  const { rows, now } = useValue($view)
  const hideIdle = useValue($hideIdle)
  const shown = hideIdle ? rows.filter(isBusy) : rows
  const idle = rows.length - rows.filter(isBusy).length
  const note = text => jsx('div', { className: 'crew-note', style: DIM, children: text })
  return jsxs('div', {
    className: 'crew-pane',
    children: [
      jsx(GpuRow, { gpu: crew && crew.gpu }),
      crew ? null : note('Status service offline: background jobs are not shown.'),
      crew && crew.api !== API ? note(`Status service is version ${crew.version || 'unknown'} and this panel expects another: re-run install.ps1.`) : null,
      jsxs('div', { className: 'crew-bar-row', style: DIM, children: [
        jsx('span', { children: `${rows.length - idle} busy · ${idle} idle` }),
        jsx('button', { type: 'button', className: 'crew-link', style: DIM, 'aria-pressed': hideIdle, onClick: onToggleIdle, children: hideIdle ? 'show idle' : 'hide idle' }),
      ] }),
      jsx('div', { className: 'crew-list', children: shown.map(bot => jsx(BotRow, { bot, now }, bot.name)) }),
      jsx(Finished, { crew, now }),
    ],
  })
}

function StatusChip({ onToggle }) {
  const { rows } = useValue($view)
  const busy = rows.filter(isBusy).length
  const alert = rows.some(r => r.state === 'waiting' || r.stalled)
  return jsxs('button', {
    type: 'button',
    className: 'crew-chip',
    style: { color: 'var(--ui-text-tertiary)' },
    title: `${chipSummary(rows)}\n(click to toggle the Crew panel)`,
    onClick: onToggle,
    children: [jsx('span', { className: busy ? 'crew-dot crew-pulse' : 'crew-dot', style: { background: alert ? AMBER : busy ? 'var(--ui-accent)' : 'var(--ui-text-quaternary)' } }), `crew ${busy}`],
  })
}

// Core Bots list rows carry data-roster-key="<connection>::<profile>". Pure CSS keyed on that
// attribute: no DOM observers, nothing to undo but removing the <style>.
export function rowCss(rows) {
  const out = []
  for (const r of rows) {
    if (!/^[\w.-]+$/.test(r.name)) continue
    const row = `[data-roster-key$="::${r.name}"]`
    const own = typeof r.color === 'string' && (HEX.test(r.color) || r.color === FALLBACK_COLOR) ? r.color : COLORS[r.name] || FALLBACK_COLOR
    const color = r.state === 'waiting' ? AMBER : own
    if (isBusy(r)) {
      out.push(`${row}{--crew-c:${color};animation:crew-glow-core ${r.state === 'job' ? '2.6s' : '1.2s'} ease-in-out infinite}`)
    }
    // badge is digits, letters, spaces, / % and a dot by construction (progressOf); re-checked before it goes in a CSS string
    if (r.progress && /^[\w %·/]+$/.test(r.progress.badge)) {
      out.push(`${row} span.truncate.font-medium::after{content:"  ${r.progress.badge}";font-weight:400;font-size:.6875rem;color:${color}}`)
    }
  }
  return out.join('\n')
}

const CSS = `
@keyframes crew-glow-core{0%,100%{background:color-mix(in srgb,var(--crew-c) 8%,transparent)}50%{background:color-mix(in srgb,var(--crew-c) 30%,transparent)}}
@media (prefers-reduced-motion:reduce){[data-roster-key]{animation:none!important}}
`+`
.crew-pane{display:flex;flex-direction:column;height:100%;overflow:auto;font-size:.75rem}
.crew-list{display:flex;flex-direction:column;padding:4px}
.crew-row{display:flex;gap:8px;align-items:flex-start;width:100%;padding:6px 8px;border:0;border-radius:4px;background:transparent;text-align:left;cursor:pointer}
.crew-row:hover{background:var(--chrome-action-hover)}
.crew-dot{display:inline-block;flex:none;width:9px;height:9px;margin-top:3px;border-radius:50%;box-sizing:border-box}
.crew-main{display:flex;flex-direction:column;min-width:0;flex:1}
.crew-line{display:flex;gap:6px;align-items:baseline}
.crew-title{font-weight:500;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.crew-progress{font-size:.6875rem;font-variant-numeric:tabular-nums;color:var(--crew-c)}
.crew-state{font-size:.6875rem}
.crew-job{display:flex;justify-content:space-between;gap:8px;font-size:.6875rem;line-height:1.35}
.crew-job-label{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.crew-job-time{flex:none;font-variant-numeric:tabular-nums}
.crew-gpu{display:flex;flex-direction:column;gap:5px;padding:8px 12px}
.crew-gpu-line{display:flex;gap:8px;align-items:center}
.crew-gpu .crew-dot{margin-top:0}
.crew-gpu-meta{font-size:.6875rem;font-variant-numeric:tabular-nums}
.crew-bar{height:4px;border-radius:2px;background:var(--ui-stroke-secondary);overflow:hidden}
.crew-bar-fill{height:100%;border-radius:2px;transition:width .4s ease}
.crew-bar-row{display:flex;justify-content:space-between;align-items:center;padding:6px 12px 0;font-size:.6875rem}
.crew-link{padding:0;border:0;background:transparent;font-size:.6875rem;cursor:pointer;text-decoration:underline}
.crew-finished{display:flex;flex-direction:column;gap:2px;margin-top:4px;padding:8px 12px}
.crew-head{font-size:.625rem;letter-spacing:.06em;margin-bottom:2px}
.crew-tip{display:flex;flex-direction:column;gap:2px;font-size:.6875rem}
.crew-tip-job{margin-top:6px;padding-top:4px;border-top:1px solid var(--ui-stroke-secondary)}
.crew-tip-mono{font-family:ui-monospace,monospace;word-break:break-all}
.crew-note{padding:6px 12px;font-size:.6875rem}
.crew-chip{display:inline-flex;gap:5px;align-items:center;padding:0 6px;border:0;background:transparent;font-size:.6875rem;cursor:pointer}
.crew-chip .crew-dot{margin-top:0;width:7px;height:7px}
@keyframes crew-glow{0%,100%{background:color-mix(in srgb,var(--crew-c) 8%,transparent)}50%{background:color-mix(in srgb,var(--crew-c) 30%,transparent)}}
.crew-row.crew-thinking,.crew-row.crew-waiting{animation:crew-glow 1.2s ease-in-out infinite}
.crew-row.crew-job{animation:crew-glow 2.6s ease-in-out infinite}
@keyframes crew-pulse{0%,100%{opacity:1;transform:scale(1)}50%{opacity:.35;transform:scale(.8)}}
.crew-pulse{animation:crew-pulse 1.2s ease-in-out infinite}
@media (prefers-reduced-motion:reduce){.crew-pulse{animation:none}.crew-bar-fill{transition:none}.crew-row.crew-thinking,.crew-row.crew-waiting,.crew-row.crew-job{animation:none;background:color-mix(in srgb,var(--crew-c) 18%,transparent)}}
`

export default {
  id: ID,
  name: 'Crew Monitor',
  description: 'Live panel showing which bots are thinking, running background jobs, or idle.',
  defaultEnabled: true,
  register(ctx) {
    open = ctx.storage.get('open', true)
    notifyOn = ctx.storage.get('notify', true)
    $hideIdle.set(ctx.storage.get('hideIdle', false))
    const style = document.createElement('style')
    style.id = STYLE_ID
    style.textContent = CSS
    document.head.appendChild(style)
    rowsStyle = document.createElement('style')
    rowsStyle.id = STYLE_ID + '-rows'
    document.head.appendChild(rowsStyle)
    ctx.onDispose(() => {
      document.getElementById(STYLE_ID)?.remove()
      rowsStyle?.remove()
      rowsStyle = null
      paneDispose = null
    })

    const toggleIdle = () => {
      $hideIdle.set(!$hideIdle.get())
      ctx.storage.set('hideIdle', $hideIdle.get())
    }
    const mountPane = () => {
      if (paneDispose) paneDispose()
      paneDispose = ctx.register({
        id: 'pane',
        area: 'panes',
        title: 'Crew',
        data: { placement: 'right', width: '280px' },
        render: () => jsx(CrewPane, { onToggleIdle: toggleIdle }),
      })
    }
    const toggle = () => {
      open = !open
      ctx.storage.set('open', open)
      if (open) {
        mountPane()
      } else if (paneDispose) {
        paneDispose()
        paneDispose = null
      }
    }
    const toggleNotify = () => {
      notifyOn = !notifyOn
      ctx.storage.set('notify', notifyOn)
      host.notify({ kind: 'info', message: `Crew Monitor notifications ${notifyOn ? 'on' : 'off'}.` })
    }
    const logRoster = () => {
      console.log('[crew-monitor] profiles.list rows', $roster.get())
      host.notify({ kind: 'info', message: 'Roster rows written to the developer console.' })
    }
    const command = (id, label, keywords, run) => ({ id, area: PALETTE_AREA, data: { id: `crew-monitor.${id}`, label, keywords, run } })

    if (open) mountPane()
    ctx.registerMany([
      command('toggle', 'Crew Monitor: toggle panel', ['crew', 'bots', 'monitor', 'jobs'], toggle),
      command('idle', 'Crew Monitor: show or hide idle bots', ['crew', 'idle', 'hide'], toggleIdle),
      command('notify', 'Crew Monitor: toggle job notifications', ['crew', 'notify', 'finished', 'stalled'], toggleNotify),
      command('roster', 'Crew Monitor: log roster fields', ['crew', 'debug', 'roster'], logRoster),
      { id: 'chip', area: STATUSBAR_AREAS.right, order: 125, render: () => jsx(StatusChip, { onToggle: toggle }) },
    ])

    startFeed(ctx)
    pollRoster(true)
    ctx.setInterval(() => pollRoster(false), ROSTER_POLL_MS)
    // The one clock: elapsed times, the 90 s "thinking" window, row CSS. Paused while hidden.
    ctx.setInterval(() => {
      if (!hidden()) recompute()
    }, 1000)
  },
}
