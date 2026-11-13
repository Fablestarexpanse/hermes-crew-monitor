import test from 'node:test'
import assert from 'node:assert/strict'
import plugin, { progressOf, rowCss, ago, deriveRows, fmtElapsed, fmtEta, waitingOf, crewEvents, finishedView, gpuView, chipSummary, instanceView } from '../plugin.js'

const NOW = 1_800_000_000_000
const sec = n => NOW / 1000 - n
const crew = {
  bots: [
    { name: 'foreman', title: 'Foreman', jobs: [] },
    { name: 'reviewer', title: 'Reviewer', jobs: [{ label: 'desloppify review', started_at: sec(3725) }] },
    { name: 'scout', title: 'Scout', jobs: [] },
  ],
}
const row = (name, extra = {}) => ({ name, ...extra })
const derive = o => Object.fromEntries(deriveRows({ crew, roster: [], focusedProfile: null, busy: false, now: NOW, ...o }).map(r => [r.name, r]))

test('plugin contract', () => {
  assert.equal(plugin.id, 'crew-monitor')
  assert.equal(typeof plugin.register, 'function')
})

test('fmtElapsed', () => {
  assert.equal(fmtElapsed(5), '5s')
  assert.equal(fmtElapsed(125), '2m 05s')
  assert.equal(fmtElapsed(3725), '1h 02m')
  assert.equal(fmtElapsed(null), '')
})

test('background job with no live turn: state job, elapsed recomputed from started_at', () => {
  const r = derive({})
  assert.equal(r.reviewer.state, 'job')
  assert.equal(r.reviewer.jobs[0].elapsed, 3725)
  assert.equal(r.foreman.state, 'idle')
})

test('focused busy turn is thinking and keeps its jobs', () => {
  const r = derive({ focusedProfile: 'reviewer', busy: true })
  assert.equal(r.reviewer.state, 'thinking')
  assert.equal(r.reviewer.jobs.length, 1)
  assert.equal(r.scout.state, 'idle')
})

test('busy flag for a different profile does not light this bot', () => {
  assert.equal(derive({ focusedProfile: 'foreman', busy: true }).scout.state, 'idle')
})

test('write inside 90s is thinking, outside is not', () => {
  const roster = [row('scout', { last_session: { last_active: sec(30) } }), row('foreman', { last_session: { last_active: sec(91) } })]
  const r = derive({ roster })
  assert.equal(r.scout.state, 'thinking')
  assert.equal(r.foreman.state, 'idle')
})

test('worker heartbeat inside 150s is a job even with no registry entry', () => {
  const r = derive({ roster: [row('scout', { worker_session: { last_active: sec(100) } })] })
  assert.equal(r.scout.state, 'job')
  assert.equal(r.scout.jobs[0].label, 'working in background')
  assert.equal(derive({ roster: [row('scout', { worker_session: { last_active: sec(151) } })] }).scout.state, 'idle')
})

test('roster unreachable: idle bots read offline/unknown, bots with a job still show it', () => {
  const r = deriveRows({ crew: null, roster: null, focusedProfile: null, busy: false, now: NOW })
  assert.deepEqual(r, [])
  const only = deriveRows({ crew: { bots: [{ name: 'x', title: 'X', jobs: [] }] }, roster: null, focusedProfile: null, busy: false, now: NOW })
  assert.equal(only[0].state, 'offline')
  const withJob = deriveRows({ crew, roster: null, focusedProfile: null, busy: false, now: NOW })
  assert.equal(withJob.find(x => x.name === 'reviewer').state, 'job')
})

test('roster title and canonical session are used; sort puts busy first', () => {
  const roster = [row('scout', { ui_meta: { 'hermes-bots': { title: 'Scout Prime' } }, canonical_session: { id: 'a', resolved_id: 'b' } })]
  const rows = deriveRows({ crew, roster, focusedProfile: null, busy: false, now: NOW })
  assert.equal(rows[0].name, 'reviewer')
  const scout = rows.find(x => x.name === 'scout')
  assert.equal(scout.title, 'Scout Prime')
  assert.equal(scout.canonical.resolved_id, 'b')
})

test('hover details carry through: command, pid, cwd, startedAt, lastWrite', () => {
  const c = { bots: [{ name: 'reviewer', title: 'Reviewer', jobs: [{ label: 'x', started_at: sec(10), pid: 7, cwd: 'F:/p', command: 'run it' }] }] }
  const [r] = deriveRows({ crew: c, roster: [row('reviewer', { last_session: { last_active: sec(200) } })], focusedProfile: null, busy: false, now: NOW })
  assert.deepEqual([r.jobs[0].pid, r.jobs[0].cwd, r.jobs[0].command, r.lastWrite], [7, 'F:/p', 'run it', sec(200)])
  assert.equal(ago(200), '3m 20s ago')
})

test('rowCss targets only busy bots by roster-key suffix, with bot colour', () => {
  const css = rowCss([{ name: 'reviewer', state: 'job' }, { name: 'foreman', state: 'thinking' }, { name: 'scout', state: 'idle' }, { name: 'bad"]x', state: 'job' }])
  assert.ok(css.includes('[data-roster-key$="::reviewer"]{--crew-c:#DC143C;animation:crew-glow-core 2.6s'))
  assert.ok(css.includes('::foreman"]{--crew-c:#FFD700;animation:crew-glow-core 1.2s'))
  assert.ok(!css.includes('scout') && !css.includes('bad'))
})

test('progress: batches and kanban tasks are separate readouts with percentages', () => {
  const both = progressOf([{ progress: { done: 2, total: 20, unit: 'batches' } }], { done: 3, total: 5, items: [{ status: 'running', title: 't' }] })
  assert.equal(both.batches.text, '2/20 10%')
  assert.equal(both.tasks.text, 'tasks 3/5 60%')
  assert.equal(both.badge, '2/20 10% · tasks 3/5 60%')
  assert.equal(progressOf([{}], { done: 1, total: 3 }).badge, 'tasks 1/3 33%')
  assert.equal(progressOf([], null), null)
})

test('progress flows through deriveRows and rowCss adds a name badge, even for an idle bot', () => {
  const c = { bots: [{ name: 'scout', title: 'Scout', jobs: [], tasks: { done: 3, total: 5 } }, { name: 'reviewer', title: 'Reviewer', jobs: [{ label: 'r', started_at: sec(5), progress: { done: 2, total: 20, unit: 'batches' } }] }] }
  const rows = deriveRows({ crew: c, roster: [], focusedProfile: null, busy: false, now: NOW })
  const scout = rows.find(x => x.name === 'scout')
  assert.equal(scout.state, 'idle')
  const css = rowCss(rows)
  assert.ok(css.includes('::scout"] span.truncate.font-medium::after{content:"  tasks 3/5 60%";'))
  assert.ok(css.includes('content:"  2/20 10%";'))
  assert.ok(!css.includes('::scout"]{--crew-c'), 'idle bot gets a badge but no pulse')
})

test('fmtEta', () => {
  assert.equal(fmtEta(null, 0), '')
  assert.equal(fmtEta(1000 + 30, 1000), '<1m left')
  assert.equal(fmtEta(1000 + 42 * 60, 1000), '~42m left')
  assert.equal(fmtEta(1000 + 3725, 1000), '~1h 02m left')
  assert.equal(fmtEta(900, 1000), 'overdue')
})

test('waiting for you: recognised spellings only, and it outranks thinking', () => {
  assert.equal(waitingOf(null), false)
  assert.equal(waitingOf({ name: 'x', last_session: { last_active: 1, status: 'running' } }), false)
  assert.equal(waitingOf({ pending_approvals: [] }), false)
  assert.equal(waitingOf({ pending_approvals: [{}] }), true)
  assert.equal(waitingOf({ last_session: { status: 'awaiting_approval' } }), true)
  assert.equal(waitingOf({ canonical_session: { state: 'waiting for user' } }), true)
  const rows = deriveRows({ crew, roster: [row('scout', { needs_input: true, last_session: { last_active: sec(5) } })], focusedProfile: null, busy: false, now: NOW })
  assert.deepEqual([rows[0].name, rows[0].state], ['scout', 'waiting'])
  assert.ok(rowCss(rows).includes('::scout"]{--crew-c:#E8A317;animation:crew-glow-core 1.2s'))
})

test('service colour overrides the built-in one; a bad value does not reach CSS', () => {
  const c = { bots: [{ name: 'scout', title: 'Scout', color: '#00ff88', jobs: [{ label: 'x', started_at: sec(1) }] }, { name: 'foreman', title: 'F', color: 'red;}body{', jobs: [{ label: 'x', started_at: sec(1) }] }] }
  const rows = deriveRows({ crew: c, roster: [], focusedProfile: null, busy: false, now: NOW })
  assert.equal(rows.find(r => r.name === 'scout').color, '#00ff88')
  assert.equal(rows.find(r => r.name === 'foreman').color, '#FFD700')
  assert.ok(!rowCss([{ name: 'scout', state: 'job', color: 'red;}body{' }]).includes('body'))
})

test('eta, stall, GPU wait and blocked tasks flow through deriveRows', () => {
  const job = { id: '5:1', label: 'desloppify review', started_at: sec(600), waiting_gpu: true, progress: { done: 4, total: 10, unit: 'batches', eta_at: sec(-1800), changed_at: sec(1200), stalled: true } }
  const c = { bots: [{ name: 'reviewer', title: 'Reviewer', jobs: [job], tasks: { done: 0, total: 2, blocked: 1, items: [{ status: 'blocked', title: 'b' }] } }] }
  const [r] = deriveRows({ crew: c, roster: [], focusedProfile: null, busy: false, now: NOW })
  assert.equal(r.stalled, true)
  assert.deepEqual([r.jobs[0].stalledFor, r.jobs[0].waitingGpu, r.jobs[0].id], [1200, true, '5:1'])
  assert.equal(r.progress.batches.eta, '~30m left')
  assert.equal(r.progress.tasks.blocked, 1)
  assert.equal(r.progress.badge, '4/10 40% · tasks 0/2 0%') // the badge that goes into CSS stays plain
  assert.equal(chipSummary([r]), 'Reviewer: desloppify review (stalled)')
  assert.equal(chipSummary([]), 'Crew Monitor: everyone is idle')
})

test('crewEvents: first payload is quiet, then finished and stalled are each said once', () => {
  const seen = new Set()
  const fin = { id: '5:1', kind: 'review', title: 'Reviewer', label: 'desloppify review', duration_s: 3725, ended_at: 100 }
  const dm = { id: '6:1', kind: 'dm', title: 'Scout', label: 'Foreman → Scout', duration_s: 4, ended_at: 101 }
  assert.deepEqual(crewEvents({ finished: [fin] }, seen, true), [])
  assert.deepEqual(crewEvents({ finished: [fin] }, seen, false), [], 'already known from the first payload')
  const next = { ...fin, id: '7:1', ended_at: 200 }
  assert.deepEqual(crewEvents({ finished: [next, dm, fin] }, seen, false), [{ text: 'Reviewer finished desloppify review after 1h 02m', kind: 'success' }])
  assert.deepEqual(crewEvents({ finished: [next, dm, fin] }, seen, false), [])
  const stalled = { ts: 2000, bots: [{ title: 'Reviewer', jobs: [{ id: '8:1', label: 'desloppify review', progress: { stalled: true, changed_at: 1100 } }] }] }
  assert.deepEqual(crewEvents(stalled, seen, false), [{ text: 'Reviewer: desloppify review has made no progress for 15m 00s', kind: 'warning' }])
  assert.deepEqual(crewEvents(stalled, seen, false), [])
  crewEvents({ ts: 2100, bots: [{ title: 'Reviewer', jobs: [{ id: '8:1', label: 'x', progress: { stalled: false } }] }] }, seen, false)
  assert.equal(crewEvents(stalled, seen, false).length, 1, 'stalling again after recovering is news')
})

test('finishedView: newest first, capped, no DM deliveries', () => {
  const f = (id, kind, ended) => ({ id, kind, bot: 'scout', title: 'Scout', label: `job ${id}`, duration_s: 125, ended_at: ended })
  const v = finishedView({ finished: [f('c', 'other', 990), f('b', 'dm', 980), f('a', 'review', 400)] }, 1000, 5)
  assert.deepEqual(v.map(x => [x.text, x.when]), [['Scout · job c', '2m 05s · 10s ago'], ['Scout · job a', '2m 05s · 10m 00s ago']])
  assert.equal(finishedView({ finished: [f('c', 'other', 990), f('a', 'review', 400)] }, 1000, 1).length, 1)
  assert.deepEqual(finishedView(null, 1000), [])
})

test('gpuView: bar fraction, stats line without repeating the label, queue', () => {
  const g = gpuView({ label: 'held by gpu_job comfyui', vram_used_mib: 22016, vram_total_mib: 24564, util_pct: 62, temp_c: 71, queue: [{ title: 'Asset Bot' }, { title: 'Trailer Bot' }] })
  assert.equal(g.frac.toFixed(3), '0.896')
  assert.equal(g.meta, 'VRAM 21.5/24.0 GB · 62% util · 71°C')
  assert.equal(g.queue, 'waiting for GPU: Asset Bot, Trailer Bot')
  assert.equal(gpuView({ label: 'Ladder 256K local · 21.5/24.0 GB', vram_used_mib: 22016, vram_total_mib: 24564 }).meta, '')
  assert.deepEqual(gpuView({ label: 'Ladder unreachable' }), { frac: null, meta: '', queue: '' })
})

test('instanceView: what is shown, and a button only for listed instances with control on', () => {
  const up = { id: 'comfyui', name: 'ComfyUI', state: 'running', port: 8188, vram_mib: 6144, started_at: 1000 - 7500, listed: true, can_start: true, can_stop: true }
  assert.deepEqual(instanceView(up, 1000, true), { id: 'comfyui', name: 'ComfyUI', state: 'running', meta: 'port 8188 · 6.0 GB · up 2h 05m', action: 'stop' })
  assert.equal(instanceView(up, 1000, false).action, null)
  const down = { ...up, state: 'stopped', vram_mib: null, started_at: null }
  assert.deepEqual([instanceView(down, 1000, true).meta, instanceView(down, 1000, true).action], ['port 8188 · stopped', 'start'])
  assert.equal(instanceView({ ...down, can_start: false }, 1000, true).action, null)
  assert.equal(instanceView({ ...up, state: 'starting' }, 1000, true).action, 'stop')
  const found = { id: 'ollama', name: 'Ollama', state: 'running', port: 11434, listed: false, can_start: false, can_stop: false }
  assert.deepEqual([instanceView(found, 1000, true).meta, instanceView(found, 1000, true).action], ['port 11434 · detected', null])
})

test('a bot only the roster knows is titled by its display name', () => {
  const rows = deriveRows({ crew, roster: [row('default', { display_name: 'Hermes' }), row('scout', { display_name: 'ignored: the service titled it' })], focusedProfile: null, busy: false, now: NOW })
  assert.equal(rows.find(r => r.name === 'default').title, 'Hermes')
  assert.equal(rows.find(r => r.name === 'scout').title, 'Scout')
})
