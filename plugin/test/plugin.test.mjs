import test from 'node:test'
import assert from 'node:assert/strict'
import plugin, { progressOf, rowCss, ago, deriveRows, fmtElapsed } from '../plugin.js'

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
