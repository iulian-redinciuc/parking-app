// The live lot status for components (docs/design/frontend.md §3): every caller shares the
// one connection from `liveFeed()`, which starts on first use and stays open for the app's life.
import { useEffect, useSyncExternalStore } from 'react'
import { liveFeed } from '../api/live'
import type { LiveFeed, LiveState } from '../api/types'

export function useLiveStatus(feed: LiveFeed = liveFeed()): LiveState {
  useEffect(() => feed.start(), [feed])
  return useSyncExternalStore(feed.subscribe, feed.getSnapshot)
}
