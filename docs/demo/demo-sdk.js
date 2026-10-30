// Stand-in for '@hermes/plugin-sdk' so the REAL plugin/plugin.js can render in a plain browser page
// (used only to make the README screenshots). Tip is a minimal hover card; atoms use React state.
import { createElement as h, cloneElement, useState, useSyncExternalStore } from 'react'

export const PALETTE_AREA = 'palette'
export const STATUSBAR_AREAS = { right: 'statusBar.right', left: 'statusBar.left' }

export function atom(value) {
  const subs = new Set()
  return {
    get: () => value,
    set(next) {
      value = next
      subs.forEach(fn => fn())
    },
    subscribe(fn) {
      subs.add(fn)
      return () => subs.delete(fn)
    },
  }
}

export const useValue = a => useSyncExternalStore(a.subscribe, a.get)

export const host = {
  state: { busy: atom(false), focusedSessionProfile: atom(''), profile: atom('') },
  request: async () => ({ profiles: [] }),
  notify: () => {},
  openSession: async () => {},
}

export function Tip({ label, children, side }) {
  const [pos, setPos] = useState(null)
  return h(
    'span',
    {
      className: 'tip-wrap',
      onMouseEnter: e => setPos(e.currentTarget.getBoundingClientRect()),
      onMouseLeave: () => setPos(null),
    },
    children,
    pos
      ? h('div', { className: 'tip-card', style: { top: pos.top, right: window.innerWidth - pos.left + 8 } }, label)
      : null
  )
}
