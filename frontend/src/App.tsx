import { lazy, Suspense } from 'react'
import { HashRouter, Navigate, Route, Routes } from 'react-router'
import BottomNav from './components/BottomNav'
import Footer from './components/Footer'
import Header from './components/Header'
import ProximityBanner from './components/ProximityBanner'
import { useLiveStatus } from './hooks/useLiveStatus'
import LiveScreen from './screens/LiveScreen'
import NotificationsScreen from './screens/NotificationsScreen'
import PrivacyScreen from './screens/PrivacyScreen'

const AdminScreen = lazy(() => import('./screens/admin/AdminScreen'))
const StatsScreen = lazy(() => import('./screens/StatsScreen'))

export default function App() {
  const { connection } = useLiveStatus()
  return (
    <HashRouter>
      <div className="flex min-h-dvh flex-col">
        <Header connection={connection} />
        <main className="mx-auto w-full max-w-xl flex-1 pr-[max(1rem,env(safe-area-inset-right))] pl-[max(1rem,env(safe-area-inset-left))]">
          <ProximityBanner />
          {/* One boundary around all routes, already mounted: the router navigates in a
              transition, so a lazy screen keeps the old one up until its chunk is in, with no
              fallback flash and no 300 ms Suspense reveal throttle (P7.7). */}
          <Suspense fallback={null}>
            <Routes>
              <Route index element={<LiveScreen />} />
              <Route path="alerts" element={<NotificationsScreen />} />
              <Route path="stats" element={<StatsScreen />} />
              <Route path="privacy" element={<PrivacyScreen />} />
              <Route path="admin/*" element={<AdminScreen />} />
              <Route path="*" element={<Navigate to="/" replace />} />
            </Routes>
          </Suspense>
        </main>
        <Footer />
        <BottomNav />
      </div>
    </HashRouter>
  )
}
