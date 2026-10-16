Your task: **{{TASK}}**: {{TITLE}}
Phase guide: `{{GUIDE}}` (section "## {{TASK}}:").

Follow these steps in order:

1. **Get context.** Read `PLAN.md`, `PROGRESS.md` (decision log, open questions, "Waiting on Iulian"), `docs/README.md` (conventions and definition of done), the `{{TASK}}` section of the phase guide, and every design spec it links. Run `git status` and `git log --oneline -8`. If there are uncommitted changes, they're probably from an interrupted earlier attempt at this task: continue from them rather than starting over. If CI exists and the latest run on `main` failed (`gh run list --branch main --limit 3`), fix that first, in its own commit (`fix CI: …`).

2. **PoC mode: never block.** The project is building a **fully working PoC on images** in the dev environment; going live is a separate track (PLAN.md §6, `docs/go-live.md`). Images are the cameras for now: the real sample photo, the simulated feed (`parking simulate-feed`), the generated images in `samples/generated/`, the synthetic ramp clip.
   - **Decisions are yours.** Technical and design choices, and values the open questions leave undecided, are never a reason to stop: pick the best option for the PoC, keep it easy to swap later, implement it, and record it in the PROGRESS.md decision log.
   - **When the guide's "Done when" needs the real camera, the lot, the vision host, production machines, real phones or anything else that doesn't exist yet,** do the **PoC check** instead: the same check in the dev environment on the image feeds. For production checks use a **production rehearsal** on the dev Pi (Compose project `parking-rehearsal`: the production Compose files, its own `.env` from `deploy/scripts/prod-env.sh`, its own data folder, loopback ports, image feeds; tear it down afterwards). Checks that run for hours or days are shortened to **at most about 20 minutes**. Phones are emulated (Playwright Chromium + WebKit, phone viewports).
   - Then **tick the task** like any other, add a line `**PoC check (date):** what was run and the result` at the end of the task's section in the phase guide, and add an entry `### {{TASK}} <title>` at the end of `docs/go-live-checks.md`: one line for what the PoC proved, and a `- [ ]` list of what remains to be checked for real (and what it needs from Iulian).
   - **A task that is only physical or needs an account or a payment** (buying, mounting, cabling, renting a server, a domain): finish whatever can be prepared (choices, scripts, docs), tick it with "(moved to go-live)" appended to its line, and put its steps in `docs/go-live-checks.md` the same way.
   - Never write `⏸️` on a task line, and don't add items to "Waiting on Iulian". **Don't invent** results for things that weren't run: say exactly what the PoC check covered.
   - Much of the code for these tasks already exists from an earlier blocked attempt (see the session log and git log): reuse it, don't rebuild it.

3. **Implement it** exactly as the guide and specs say. If the guide is wrong or incomplete, make the smallest sensible choice, and record it in the PROGRESS.md decision log (and fix the spec in the same commit, per the definition of done).

4. **Verify.** Run the task's "Done when" check, plus the relevant lint and tests. Fix things until they pass. Report the actual results; never claim a check passed if you didn't run it.

5. **Record progress** in `PROGRESS.md`:
   - tick the task (`- [x]`)
   - update that phase's `n / total` counter and status in the overview table
   - add a one-line entry under today's date in the session log
   - add any metrics or decisions

6. **Commit and push:** one commit `{{TASK}}: <short summary>`, pushed to `origin main` (retry with backoff on network errors).

7. **Finish with a short summary** (3–6 lines): what you did, how you verified it, and anything Iulian needs to know or do.
