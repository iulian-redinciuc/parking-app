import { act, renderHook } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { createMockFeed } from '../api/mock'
import { useLiveStatus } from './useLiveStatus'
import { useNow } from './useNow'

describe('useLiveStatus', () => {
  afterEach(() => vi.useRealTimers())

  it('starts the feed and re-renders on every update', () => {
    vi.useFakeTimers()
    const feed = createMockFeed({ seed: 1, minDelayMs: 1000, maxDelayMs: 1000 })
    const { result } = renderHook(() => useLiveStatus(feed))
    expect(result.current.connection).toBe('live')
    const first = result.current
    act(() => vi.advanceTimersByTime(1000))
    expect(result.current).not.toBe(first)
    expect(result.current).toBe(feed.getSnapshot())
    feed.stop()
  })

  it('shares one feed between components', () => {
    const feed = createMockFeed({ seed: 2 })
    const start = vi.spyOn(feed, 'start')
    const a = renderHook(() => useLiveStatus(feed))
    const b = renderHook(() => useLiveStatus(feed))
    expect(a.result.current).toBe(b.result.current)
    expect(start).toHaveBeenCalled()
    feed.stop()
  })
})

describe('useNow', () => {
  it('ticks every interval', () => {
    vi.useFakeTimers()
    vi.setSystemTime(1_000_000)
    const { result, unmount } = renderHook(() => useNow(5000))
    expect(result.current).toBe(1_000_000)
    act(() => vi.advanceTimersByTime(5000))
    expect(result.current).toBe(1_005_000)
    unmount()
    expect(vi.getTimerCount()).toBe(0)
    vi.useRealTimers()
  })
})
