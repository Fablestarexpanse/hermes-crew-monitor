// Crew Monitor: a live panel beside the chat showing which Hermes bots are working.
//
// Sources (see docs/FINDINGS.md):
//  - "thinking": the same profiles.list roster rows Desktop's own "Active now" reads
//    (focused live turn, last_session written in the last 90 s, worker heartbeat in 150 s).
//  - background jobs + GPU line: the read-only status service on 127.0.0.1:8094 (a plugin cannot
//    read files or probe pids itself).
import { PALETTE_AREA, STATUSBAR_AREAS, Tip, atom, host, useValue } from '@hermes/plugin-sdk'
import { jsx, jsxs } from 'react/jsx-runtime'
import { useEffect, useState } from 'react'

const ID = 'crew-monitor'
const SERVICE = 'http://127.0.0.1:8094/crew'
const STYLE_ID = 'crew-monitor-style'
const ACTIVE_WINDOW_S = 90 // row-helpers.ts ACTIVE_WINDOW_S
const WORKER_WINDOW_S = 150 // row-helpers.ts WORKER_ACTIVE_WINDOW_S
const SERVICE_POLL_MS = 3000
const ROSTER_POLL_MS = 5000

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

/**
 * Merge service data (jobs, titles), roster rows (liveness) and the focused turn into display rows.
 * state: 'thinking' | 'job' | 'idle' | 'offline'. A thinking bot keeps its jobs listed.
 */
export function ago(seconds) {
  return seconds == null ? '' : `${fmtElapsed(seconds)} ago`
}

export function deriveRows({ crew, roster, focusedProfile, busy, now }) {
  const nowS = now / 1000
  const byName = new Map()
  for (const b of (crew && crew.bots) || []) byName.set(b.name, { name: b.name, title: b.title, jobs: b.jobs || [], tasks: b.tasks })
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
      if (thinking) state = 'thinking'
      else if (hasJob) state = 'job'
      else if (!rosterKnown) state = 'offline' // cannot tell if it is thinking
      const jobs = b.jobs.map(j => ({
        label: j.label,
        progress: j.progress,
        elapsed: j.started_at ? nowS - j.started_at : j.elapsed_s,
        startedAt: j.started_at,
        pid: j.pid,
        cwd: j.cwd,
        command: j.command,
      }))
      // A worker heartbeat with no process-registry entry (kanban/tool worker) still deserves a line.
      if (!jobs.length && state === 'job') jobs.push({ label: 'working in background', elapsed: null })
      return { name: b.name, title: b.title, state, jobs, progress: progressOf(jobs, b.tasks), lastWrite: lastWrite || null, canonical: row && row.canonical_session }
    })
    .sort((a, c) => rank(a.state) - rank(c.state) || a.title.localeCompare(c.title))
}

// "3/5": a running job's own progress (desloppify batches) wins over open kanban tasks.
export function progressOf(jobs, tasks) {
  const j = jobs.find(x => x.progress && x.progress.total)
  if (j) return { text: `${j.progress.done}/${j.progress.total}`, what: j.progress.unit || 'steps' }
  if (tasks && tasks.total) return { text: `${tasks.done}/${tasks.total}`, what: 'kanban tasks' }
  return null
}

const rank = s => ({ thinking: 0, job: 1, idle: 2, offline: 3 })[s]

// ---- state -----------------------------------------------------------------------------------

const $crew = atom(null) // service payload, null = unreachable
const $roster = atom(null) // profiles.list rows, null = not loaded
let open = true
let paneDispose = null

async function pollService() {
  const ctl = new AbortController()
  const t = setTimeout(() => ctl.abort(), 2500)
  try {
    const res = await fetch(SERVICE, { signal: ctl.signal, cache: 'no-store' })
    $crew.set(res.ok ? await res.json() : null)
  } catch {
    $crew.set(null)
  } finally {
    clearTimeout(t)
  }
}

async function pollRoster() {
  try {
    const res = await host.request('profiles.list', {})
    $roster.set((res && res.profiles) || [])
  } catch {
    $roster.set(null)
  }
}

function useDerived() {
  const crew = useValue($crew)
  const roster = useValue($roster)
  const focusedProfile = useValue(host.state.focusedSessionProfile || host.state.profile)
  const busy = useValue(host.state.busy)
  const [now, setNow] = useState(Date.now())
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(id)
  }, [])
  return { crew, roster, now, rows: deriveRows({ crew, roster, focusedProfile, busy, now }) }
}

// ---- UI --------------------------------------------------------------------------------------

function Dot({ color, state }) {
  const hollow = state === 'offline'
  return jsx('span', {
    'aria-hidden': 'true',
    className: state === 'thinking' ? 'crew-dot crew-pulse' : 'crew-dot',
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

function Hover({ bot, now }) {
  const stateText = { thinking: 'thinking now', job: 'running a background job', idle: 'idle', offline: 'status unknown (roster unreachable)' }[bot.state]
  const dim = { color: 'var(--ui-text-tertiary)' }
  return jsxs('div', {
    className: 'crew-tip',
    children: [
      jsxs('div', { children: [jsx('b', { children: bot.title }), jsx('span', { style: dim, children: ` · ${bot.name}` })] }),
      jsx('div', { style: dim, children: stateText }),
      bot.progress ? jsx('div', { style: dim, children: `${bot.progress.text} ${bot.progress.what}` }) : null,
      bot.lastWrite ? jsx('div', { style: dim, children: `last wrote ${ago(now / 1000 - bot.lastWrite)}` }) : null,
      ...bot.jobs.map((j, i) =>
        jsxs('div', { className: 'crew-tip-job', children: [
          jsx('div', { children: j.label + (j.elapsed != null ? ` · ${fmtElapsed(j.elapsed)}` : '') }),
          j.startedAt ? jsx('div', { style: dim, children: `started ${new Date(j.startedAt * 1000).toLocaleTimeString()}${j.pid ? ` · pid ${j.pid}` : ''}` }) : null,
          j.cwd ? jsx('div', { style: dim, className: 'crew-tip-mono', children: j.cwd }) : null,
          j.command ? jsx('div', { style: dim, className: 'crew-tip-mono', children: j.command }) : null,
        ] }, i)
      ),
    ],
  })
}

function BotRow({ bot, now }) {
  const color = COLORS[bot.name] || FALLBACK_COLOR
  const dim = bot.state === 'idle' || bot.state === 'offline'
  const status = bot.state === 'thinking' ? 'thinking' : bot.state === 'job' ? '' : bot.state
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
              bot.progress
                ? jsx('span', { className: 'crew-progress', title: bot.progress.what, children: bot.progress.text })
                : null,
              status
                ? jsx('span', { className: 'crew-state', style: { color: 'var(--ui-text-tertiary)' }, children: status })
                : null,
            ],
          }),
          ...bot.jobs.map((j, i) =>
            jsxs(
              'span',
              {
                className: 'crew-job',
                style: { color: 'var(--ui-text-tertiary)' },
                children: [
                  jsx('span', { className: 'crew-job-label', children: j.label }),
                  j.elapsed != null ? jsx('span', { className: 'crew-job-time', children: fmtElapsed(j.elapsed) }) : null,
                ],
              },
              i
            )
          ),
        ],
      }),
    ],
  }) })
}

function GpuRow({ gpu }) {
  if (!gpu) return null
  const color = gpu.state === 'unknown' ? 'var(--ui-text-quaternary)' : 'var(--ui-accent)'
  return jsxs('div', {
    className: 'crew-gpu',
    style: { color: 'var(--ui-text-secondary)', borderBottom: '1px solid var(--ui-stroke-secondary)' },
    title: 'Local GPU context',
    children: [jsx(Dot, { color, state: gpu.state === 'held' ? 'job' : 'local' }), jsx('span', { children: gpu.label })],
  })
}

function CrewPane() {
  const { crew, rows, now } = useDerived()
  return jsxs('div', {
    className: 'crew-pane',
    children: [
      jsx(GpuRow, { gpu: crew && crew.gpu }),
      crew
        ? null
        : jsx('div', {
            className: 'crew-note',
            style: { color: 'var(--ui-text-tertiary)' },
            children: 'Status service offline: background jobs are not shown.',
          }),
      jsx('div', { className: 'crew-list', children: rows.map(bot => jsx(BotRow, { bot, now }, bot.name)) }),
    ],
  })
}

function StatusChip({ onToggle }) {
  const { rows } = useDerived()
  const busy = rows.filter(r => r.state === 'thinking' || r.state === 'job').length
  return jsxs('button', {
    type: 'button',
    className: 'crew-chip',
    style: { color: 'var(--ui-text-tertiary)' },
    title: 'Toggle Crew Monitor',
    onClick: onToggle,
    children: [jsx('span', { className: busy ? 'crew-dot crew-pulse' : 'crew-dot', style: { background: busy ? 'var(--ui-accent)' : 'var(--ui-text-quaternary)' } }), `crew ${busy}`],
  })
}

// Core Bots list rows carry data-roster-key="<connection>::<profile>". Pure CSS keyed on that
// attribute: no DOM observers, nothing to undo but removing the <style>.
export function rowCss(rows) {
  const out = []
  for (const r of rows) {
    if (!/^[\w.-]+$/.test(r.name)) continue
    const row = `[data-roster-key$="::${r.name}"]`
    const color = COLORS[r.name] || FALLBACK_COLOR
    if (r.state === 'thinking' || r.state === 'job') {
      out.push(`${row}{--crew-c:${color};animation:crew-glow-core ${r.state === 'thinking' ? '1.2s' : '2.6s'} ease-in-out infinite}`)
    }
    // text is digits and a slash by construction (progressOf), so it is safe inside a CSS string
    if (r.progress && /^\d+\/\d+$/.test(r.progress.text)) {
      out.push(`${row} span.truncate.font-medium::after{content:"  ${r.progress.text}";font-weight:400;font-size:.6875rem;color:${color}}`)
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
.crew-gpu{display:flex;gap:8px;align-items:center;padding:8px 12px}
.crew-gpu .crew-dot{margin-top:0}
.crew-tip{display:flex;flex-direction:column;gap:2px;font-size:.6875rem}
.crew-tip-job{margin-top:6px;padding-top:4px;border-top:1px solid var(--ui-stroke-secondary)}
.crew-tip-mono{font-family:ui-monospace,monospace;word-break:break-all}
.crew-note{padding:6px 12px;font-size:.6875rem}
.crew-chip{display:inline-flex;gap:5px;align-items:center;padding:0 6px;border:0;background:transparent;font-size:.6875rem;cursor:pointer}
.crew-chip .crew-dot{margin-top:0;width:7px;height:7px}
@keyframes crew-glow{0%,100%{background:color-mix(in srgb,var(--crew-c) 8%,transparent)}50%{background:color-mix(in srgb,var(--crew-c) 30%,transparent)}}
.crew-row.crew-thinking{animation:crew-glow 1.2s ease-in-out infinite}
.crew-row.crew-job{animation:crew-glow 2.6s ease-in-out infinite}
@keyframes crew-pulse{0%,100%{opacity:1;transform:scale(1)}50%{opacity:.35;transform:scale(.8)}}
.crew-pulse{animation:crew-pulse 1.2s ease-in-out infinite}
@media (prefers-reduced-motion:reduce){.crew-pulse{animation:none}.crew-row.crew-thinking,.crew-row.crew-job{animation:none;background:color-mix(in srgb,var(--crew-c) 18%,transparent)}}
`

export default {
  id: ID,
  name: 'Crew Monitor',
  description: 'Live panel showing which bots are thinking, running background jobs, or idle.',
  defaultEnabled: true,
  register(ctx) {
    open = ctx.storage.get('open', true)
    const style = document.createElement('style')
    style.id = STYLE_ID
    style.textContent = CSS
    document.head.appendChild(style)
    const rowsStyle = document.createElement('style')
    rowsStyle.id = STYLE_ID + '-rows'
    document.head.appendChild(rowsStyle)
    ctx.onDispose(() => {
      document.getElementById(STYLE_ID)?.remove()
      rowsStyle.remove()
    })
    const syncRows = () => {
      const css = rowCss(
        deriveRows({
          crew: $crew.get(),
          roster: $roster.get(),
          focusedProfile: (host.state.focusedSessionProfile || host.state.profile).get(),
          busy: host.state.busy.get(),
          now: Date.now(),
        })
      )
      if (rowsStyle.textContent !== css) rowsStyle.textContent = css
    }
    ctx.setInterval(syncRows, 1000)

    const mountPane = () => {
      if (paneDispose) paneDispose()
      paneDispose = ctx.register({
        id: 'pane',
        area: 'panes',
        title: 'Crew',
        data: { placement: 'right', width: '280px' },
        render: () => jsx(CrewPane, {}),
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
    ctx.onDispose(() => {
      paneDispose = null
    })

    if (open) mountPane()
    ctx.registerMany([
      {
        id: 'toggle',
        area: PALETTE_AREA,
        data: { id: 'crew-monitor.toggle', label: 'Crew Monitor: toggle panel', keywords: ['crew', 'bots', 'monitor', 'jobs'], run: toggle },
      },
      { id: 'chip', area: STATUSBAR_AREAS.right, order: 125, render: () => jsx(StatusChip, { onToggle: toggle }) },
    ])

    pollService()
    pollRoster()
    ctx.setInterval(pollService, SERVICE_POLL_MS)
    ctx.setInterval(pollRoster, ROSTER_POLL_MS)
  },
}
