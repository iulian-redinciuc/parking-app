// The API origin, on its own so the service worker can use it without the client and mock code.

/** `mock` (dev default), or the API origin, e.g. `http://localhost:8000`. */
export const API_BASE: string = (import.meta.env.VITE_API_BASE ?? 'mock').replace(/\/+$/, '')
export const IS_MOCK = API_BASE === 'mock'
