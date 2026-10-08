# Agent loop

Runs the plan by itself on the **dev Raspberry Pi**: it takes the next open task from [`PROGRESS.md`](../../PROGRESS.md), gives it to a **fresh Claude Code session**, checks the task was ticked and pushed, then moves on to the next one, until everything is done.

## Commands

```bash
tools/agent-loop/start.sh          # start in the background (tmux session "parking-loop")
tmux attach -t parking-loop        # watch live; leave it running with Ctrl+B, then D
tools/agent-loop/status.sh         # what it's doing now + the last few lines
tools/agent-loop/status.sh -f      # follow the live log
tools/agent-loop/stop.sh           # stop after the current task
tools/agent-loop/stop.sh --now     # stop immediately (the task resumes on the next start)
```

From your phone: [`PROGRESS.md` on GitHub](https://github.com/iulian-redinciuc/parking-app/blob/main/PROGRESS.md) updates after every task, and every task is its own commit (`P1.5: …`).

## How it works

```mermaid
flowchart TD
  A[git pull] --> B{next open task<br/>in PROGRESS.md?}
  B -- none, only ⏸️ left --> W[wait 15 min, check again] --> A
  B -- none at all --> F[FINISHED]
  B -- P1.5 --> S[new Claude session for P1.5]
  S --> R{result}
  R -- ticked ✅ --> Q{tests added?<br/>lint + tests pass?}
  Q -- yes --> P[make sure it's pushed] --> A
  Q -- no --> FX[send the problem back to the same session] --> Q
  Q -- still failing after 2 fixes --> STOP[loop stops, task marked ⏸️]
  R -- marked ⏸️ needs you --> A
  R -- usage limit --> L[wait until the reset time] --> S2[resume the same session] --> R
  R -- network/API error --> T[wait 5 min] --> S2
  R -- unfinished --> S2
  R -- unfinished 3× --> X[mark ⏸️ for you] --> A
```

- **One task per session.** Each task starts a new session with the task prompt ([task-prompt.md](task-prompt.md)) and the loop's rules ([rules.md](rules.md)).
- **Pushed after every task.** The session commits and pushes. Then the loop checks: any leftover changes go back to the session to commit, and any unpushed commits are pushed by the loop.
- **Quality check after every task**, run by the loop itself (not trusted to the session):
  1. **Tests added?** If the task changed app code (`backend/parking/`, `frontend/src/`, `tools/…`) but no test files, the session is sent back to add tests.
  2. **Lint + tests pass?** The loop runs `ruff` + `pytest` (backend) and `lint` + `vitest` (frontend), for whichever parts exist yet. If they fail, the error output goes back to the session to fix. Deleting or skipping tests is forbidden.
  3. If they still fail after 2 fix rounds, the task is marked ⏸️ and **the loop stops**, so no new work gets built on broken code.
  - Slow model tests (`-m slow`) and the Playwright browser tests run in CI on GitHub, not in this check.
- **Usage limits.** When your Claude plan's limit is reached, the loop reads the reset time from the error message, or checks again every 30 min if it can't. It then **resumes the same session**, so no context is lost.
- **Interruptions.** If the Pi reboots or you stop it with `--now`, the next `start.sh` resumes the interrupted task's session.
- **Tasks only you can do** (photos, hardware, accounts, a domain, answers to open questions): the session marks them `⏸️ … (needs: …)` and lists them under **Waiting on Iulian** in PROGRESS.md, and the loop moves on to the next task it *can* do. To unblock a task, provide what it needs, then **delete the `⏸️ ` from its line** (on the Pi or directly on GitHub). The loop pulls changes before every task and picks it up again.
- When only blocked tasks are left, it checks again every 15 minutes. When everything is ticked, it stops.

## Where things are

| What | Where |
|------|-------|
| Status | `~/.parking-loop/status.txt` |
| Live log (current task) | `~/.parking-loop/logs/current.log` |
| Full log per task | `~/.parking-loop/logs/<date>-<task>.log` |
| Lint/test output per task | `~/.parking-loop/logs/<date>-<task>-verify.log` |
| Loop history | `~/.parking-loop/loop.log` |
| A task's full conversation | `cd ~/workspace/parking-app && claude --resume <session id>` (the id is in the status and the log) |

Logs live outside the repo, so they're never committed.

## Settings (environment variables for `start.sh`)

| Variable | Default | Meaning |
|----------|---------|---------|
| `TASK_TIMEOUT` | `3h` | Max time for one session run |
| `MAX_ATTEMPTS` | `3` | Session runs per task before it's marked ⏸️ |
| `IDLE_POLL_S` | `900` | How often to re-check when only blocked tasks remain |
| `LIMIT_FALLBACK_S` | `1800` | Re-try interval when a limit's reset time can't be read |
| `MAX_FIX_ROUNDS` | `2` | Chances to fix failing lint/tests before the loop stops |
| `VERIFY_TIMEOUT` | `30m` | Max time for each lint/test command |
| `CLAUDE_MODEL` | (your default) | Model for the sessions |

## Important

- Sessions run with **all permission prompts skipped** (`--dangerously-skip-permissions`), so nobody has to approve commands. [rules.md](rules.md) limits them: repo only, no other Docker containers or services on the Pi, no secrets in git, no force-push. They're instructions, not a sandbox, so check the commits now and then.
- **Don't edit the repo yourself while the loop is running.** Stop it first, or edit only `PROGRESS.md` on GitHub (it's pulled before each task).
- Every session uses your Claude plan's usage.
