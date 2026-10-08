// Stand-in for screens built in later tasks (Live P3.4, Alerts Phase 6, Stats/Admin Phase 7, Privacy Phase 8).
export default function PlaceholderScreen({ title, note }: { title: string; note: string }) {
  return (
    <section className="flex flex-col gap-2 py-8">
      <h1 className="text-2xl font-bold">{title}</h1>
      <p className="text-muted">{note}</p>
    </section>
  )
}
