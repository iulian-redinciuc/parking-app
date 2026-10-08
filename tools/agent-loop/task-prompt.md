Your task: **{{TASK}}**: {{TITLE}}
Phase guide: `{{GUIDE}}` (section "## {{TASK}}:").

Follow these steps in order:

1. **Get context.** Read `PLAN.md`, `PROGRESS.md` (decision log, open questions, "Waiting on Iulian"), `docs/README.md` (conventions and definition of done), the `{{TASK}}` section of the phase guide, and every design spec it links. Run `git status` and `git log --oneline -8`. If there are uncommitted changes, they're probably from an interrupted earlier attempt at this task: continue from them rather than starting over. If CI exists and the latest run on `main` failed (`gh run list --branch main --limit 3`), fix that first, in its own commit (`fix CI: …`).

2. **Check whether you can do it.** The project is building an **MVP with what exists** (PLAN.md §6). **Decisions are yours:** technical and design choices (models, algorithms, workarounds for the sample photos, values the open questions leave undecided) are never a reason to block. Pick the best option for the MVP, keep it easy to swap later, implement it, and record it in the PROGRESS.md decision log. Use the working assumptions recorded for open questions. Only block when the task physically needs something only Iulian can provide: hardware or its installation, recordings from a camera that doesn't exist yet, accounts, a domain, or payments. Also block if it depends on an earlier task that isn't done or is blocked. **Don't invent** those things.
   - Do any useful part that doesn't need the missing input.
   - Then change the task's line in `PROGRESS.md` from `- [ ] **{{TASK}}** …` to `- [ ] ⏸️ **{{TASK}}** … (needs: <exactly what is missing and why>)`.
   - Add a bullet under "## Waiting on Iulian" in `PROGRESS.md`.
   - Commit (`{{TASK}}: blocked, needs …`), push, give a short summary, and stop.

3. **Implement it** exactly as the guide and specs say. If the guide is wrong or incomplete, make the smallest sensible choice, and record it in the PROGRESS.md decision log (and fix the spec in the same commit, per the definition of done).

4. **Verify.** Run the task's "Done when" check, plus the relevant lint and tests. Fix things until they pass. Report the actual results; never claim a check passed if you didn't run it.

5. **Record progress** in `PROGRESS.md`:
   - tick the task (`- [x]`)
   - update that phase's `n / total` counter and status in the overview table
   - add a one-line entry under today's date in the session log
   - add any metrics or decisions

6. **Commit and push:** one commit `{{TASK}}: <short summary>`, pushed to `origin main` (retry with backoff on network errors).

7. **Finish with a short summary** (3–6 lines): what you did, how you verified it, and anything Iulian needs to know or do.
