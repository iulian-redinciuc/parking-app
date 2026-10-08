// `value`, but changing at most once every `ms`; the latest value always arrives in the end.
// Used for the aria-live count so screen readers announce it at most every 30 s (frontend.md §4).
import { useEffect, useRef, useState } from 'react'

export function useThrottled<T>(value: T, ms: number): T {
  const [shown, setShown] = useState(value)
  const lastChange = useRef(0)
  useEffect(() => {
    if (Object.is(value, shown)) return
    const wait = Math.max(0, lastChange.current + ms - Date.now())
    const timer = setTimeout(() => {
      lastChange.current = Date.now()
      setShown(value)
    }, wait)
    return () => clearTimeout(timer)
  }, [value, shown, ms])
  return shown
}
