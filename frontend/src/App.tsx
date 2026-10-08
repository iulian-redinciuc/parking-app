import { HashRouter, Route, Routes } from 'react-router'

function ComingSoon() {
  return (
    <main className="flex min-h-dvh flex-col items-center justify-center gap-2 bg-bg p-6 text-text">
      <h1 className="text-3xl font-bold">Parking: coming soon</h1>
      <p className="text-muted">Live free-space counts will appear here.</p>
    </main>
  )
}

export default function App() {
  return (
    <HashRouter>
      <Routes>
        <Route path="*" element={<ComingSoon />} />
      </Routes>
    </HashRouter>
  )
}
