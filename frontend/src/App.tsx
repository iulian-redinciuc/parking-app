import { HashRouter, Navigate, Route, Routes } from 'react-router'
import BottomNav from './components/BottomNav'
import Header from './components/Header'
import { useLiveStatus } from './hooks/useLiveStatus'
import LiveScreen from './screens/LiveScreen'
import PlaceholderScreen from './screens/PlaceholderScreen'

export default function App() {
  const { connection } = useLiveStatus()
  return (
    <HashRouter>
      <div className="flex min-h-dvh flex-col">
        <Header connection={connection} />
        <main className="mx-auto w-full max-w-xl flex-1 pr-[max(1rem,env(safe-area-inset-right))] pb-[calc(5rem+env(safe-area-inset-bottom))] pl-[max(1rem,env(safe-area-inset-left))]">
          <Routes>
            <Route index element={<LiveScreen />} />
            <Route
              path="alerts"
              element={<PlaceholderScreen title="Alerts" note="Notifications are coming soon." />}
            />
            <Route
              path="stats"
              element={
                <PlaceholderScreen
                  title="Stats"
                  note="Charts of typical free spaces are coming soon."
                />
              }
            />
            <Route
              path="privacy"
              element={
                <PlaceholderScreen
                  title="Privacy"
                  note="What we process and store will be described here."
                />
              }
            />
            <Route
              path="admin/*"
              element={
                <PlaceholderScreen
                  title="Admin"
                  note="Camera and zone administration is coming soon."
                />
              }
            />
            <Route path="*" element={<Navigate to="/" replace />} />
          </Routes>
        </main>
        <BottomNav />
      </div>
    </HashRouter>
  )
}
