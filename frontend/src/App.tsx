import { useTranslation } from 'react-i18next'
import { HashRouter, Navigate, Route, Routes } from 'react-router'
import BottomNav from './components/BottomNav'
import Header from './components/Header'
import { useLiveStatus } from './hooks/useLiveStatus'
import LiveScreen from './screens/LiveScreen'
import PlaceholderScreen from './screens/PlaceholderScreen'

export default function App() {
  const { connection } = useLiveStatus()
  const { t } = useTranslation()
  return (
    <HashRouter>
      <div className="flex min-h-dvh flex-col">
        <Header connection={connection} />
        <main className="mx-auto w-full max-w-xl flex-1 pr-[max(1rem,env(safe-area-inset-right))] pb-[calc(5rem+env(safe-area-inset-bottom))] pl-[max(1rem,env(safe-area-inset-left))]">
          <Routes>
            <Route index element={<LiveScreen />} />
            <Route
              path="alerts"
              element={
                <PlaceholderScreen title={t('screen.alerts')} note={t('screen.alerts_note')} />
              }
            />
            <Route
              path="stats"
              element={
                <PlaceholderScreen title={t('screen.stats')} note={t('screen.stats_note')} />
              }
            />
            <Route
              path="privacy"
              element={
                <PlaceholderScreen title={t('screen.privacy')} note={t('screen.privacy_note')} />
              }
            />
            <Route
              path="admin/*"
              element={
                <PlaceholderScreen title={t('screen.admin')} note={t('screen.admin_note')} />
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
