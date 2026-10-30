export const PALETTE_AREA = 'palette'
export const STATUSBAR_AREAS = { right: 'statusBar.right', left: 'statusBar.left' }
export const atom = v => ({ v, get: () => v, set(n) { v = n } })
export const host = { state: {}, request: async () => ({}), notify() {} }
export const useValue = a => a.get()
