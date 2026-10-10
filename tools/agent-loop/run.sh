#!/usr/bin/env bash
# Works through PROGRESS.md one task at a time, each task in a fresh Claude Code
# session. After each task it checks that tests were added and that lint/tests
# pass, waits out usage limits, and resumes the same session afterwards.
# Start it with start.sh (inside tmux); see README.md.
set -uo pipefail
shopt -u patsub_replacement 2>/dev/null || true

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="${REPO:-$(cd "$HERE/../.." && pwd)}"
STATE="${STATE:-$HOME/.parking-loop}"
TASK_TIMEOUT="${TASK_TIMEOUT:-3h}"            # max wall time per session run
MAX_ATTEMPTS="${MAX_ATTEMPTS:-3}"             # session runs per task before it's marked blocked
MAX_TRANSIENT="${MAX_TRANSIENT:-10}"          # retries for network/API hiccups per session run
MAX_FIX_ROUNDS="${MAX_FIX_ROUNDS:-2}"         # chances to fix failing lint/tests before the loop stops
IDLE_POLL_S="${IDLE_POLL_S:-900}"             # how often to re-check when only blocked tasks remain
LIMIT_FALLBACK_S="${LIMIT_FALLBACK_S:-1800}"  # re-try interval when a limit's reset time is unknown
LIMIT_GRACE_S="${LIMIT_GRACE_S:-90}"          # extra wait after the announced reset time
TRANSIENT_WAIT_S="${TRANSIENT_WAIT_S:-300}"
VERIFY_TIMEOUT="${VERIFY_TIMEOUT:-30m}"

export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"   # uv and other user-level tools
PROGRESS="$REPO/PROGRESS.md"
CURRENT_LOG="$STATE/logs/current.log"
RULES="$(cat "$HERE/rules.md")"
MODEL_ARGS=()
[ -n "${CLAUDE_MODEL:-}" ] && MODEL_ARGS=(--model "$CLAUDE_MODEL")
DONE_THIS_RUN=()

mkdir -p "$STATE/logs" /tmp/parking-loop
exec 9>"$STATE/lock"
flock -n 9 || { echo "The loop is already running (lock: $STATE/lock)."; exit 1; }

now() { date '+%Y-%m-%d %H:%M:%S'; }

say() {
    local line="[$(date +%H:%M:%S)] [loop] $*"
    echo "$line"
    echo "$line" >>"$CURRENT_LOG"
    echo "[$(now)] $*" >>"$STATE/loop.log"
}

# ---------- PROGRESS.md helpers ----------
task_line()  { grep -m1 -E "^- \[.\] (⏸️ )?\*\*$1\*\*" "$PROGRESS"; }
task_title() { task_line "$1" | sed -E 's/^- \[.\] (⏸️ )?\*\*[^*]+\*\* ?//'; }
task_guide() { local n="${1#P}"; n="${n%%.*}"; (cd "$REPO" && ls docs/phases/phase-"$n"-*.md 2>/dev/null | head -1); }
next_task()  { grep -m1 -oP '^- \[ \] \*\*\KP\d+\.\d+(?=\*\*)' "$PROGRESS"; }
open_count() { grep -cE '^- \[ \] ' "$PROGRESS"; }
blocked_list() {
    local b; b="$(grep -oP '^- \[ \] ⏸️ \*\*\KP\d+\.\d+' "$PROGRESS" | paste -sd ' ' -)"
    echo "${b:-none}"
}

task_state() {
    local line; line="$(task_line "$1")"
    case "$line" in
        "- [x]"*) echo done ;;
        *"⏸️"*)   echo blocked ;;
        "")       echo missing ;;
        *)        echo open ;;
    esac
}

write_status() { # state [task] [detail]
    local state="$1" task="${2:-}" detail="${3:-}"
    {
        echo "Parking agent loop"
        echo "  State:    $state"
        if [ -n "$task" ]; then
            echo "  Task:     $task: $(task_title "$task")"
            echo "  Guide:    $(task_guide "$task")"
        fi
        [ -n "$detail" ] && echo "  Detail:   $detail"
        echo "  Updated:  $(now)"
        echo "  Done this run: ${DONE_THIS_RUN[*]:-none}"
        echo "  Blocked (waiting on you): $(blocked_list)"
        echo "  Open tasks left: $(open_count)"
        echo "  Live log: $CURRENT_LOG"
    } >"$STATE/status.txt"
}

# ---------- prompts ----------
render_prompt() { # task
    local task="$1" p
    p="$(cat "$HERE/task-prompt.md")"
    p="${p//\{\{TASK\}\}/$task}"
    p="${p//\{\{TITLE\}\}/$(task_title "$task")}"
    p="${p//\{\{GUIDE\}\}/$(task_guide "$task")}"
    printf '%s' "$p"
}

continue_prompt() { # task
    printf 'Continue task %s where you left off: check `git status` and PROGRESS.md, finish the remaining steps of the task prompt (verify, record progress, commit, push), then give the short summary.' "$1"
}

tests_prompt() { # task
    printf 'The loop checked your work on %s: you changed application code but added or changed no tests. Add tests for the logic you wrote, as the phase guide and docs/design/testing.md require (unit tests at minimum), run them until they pass, then commit ("%s: tests") and push. If the change genuinely has no testable logic, explain why in one line and change nothing.' "$1" "$1"
}

fix_prompt() { # task verify_log
    printf 'After %s, the loop ran lint and tests itself and they FAIL. Output (last lines):\n\n```\n%s\n```\n\nFix the cause in the code (or the test, if the test itself is wrong). Never delete, skip or weaken tests just to make them pass. Re-run lint and tests until they pass, then commit ("%s: fix lint/tests") and push.' \
        "$1" "$(tail -n 80 "$2")" "$1"
}

# ---------- running Claude ----------
stop_requested() { [ -f "$STATE/STOP" ]; }

sleep_until() { # epoch — wakes early if a stop is requested
    local left
    while left=$(( $1 - $(date +%s) )); [ "$left" -gt 0 ]; do
        stop_requested && return 1
        sleep $(( left < 30 ? left : 30 ))
    done
}

run_claude() { # mode(new|resume) session_id prompt logfile ; sets RC and the result summary variables
    local mode="$1" sid="$2" prompt="$3" log="$4"
    local args=(-p "$prompt" --output-format stream-json --verbose
                --dangerously-skip-permissions --append-system-prompt "$RULES" "${MODEL_ARGS[@]}")
    if [ "$mode" = new ]; then args+=(--session-id "$sid"); else args+=(--resume "$sid"); fi
    rm -f "$STATE/last_result.env"
    # 9>&- : child processes must not inherit the loop's lock
    (cd "$REPO" && timeout "$TASK_TIMEOUT" claude "${args[@]}" </dev/null 2>&1) 9>&- \
        | python3 -I "$HERE/format_stream.py" "$STATE/last_result.env" 9>&- \
        | tee -a "$log" "$CURRENT_LOG" 9>&-
    RC=${PIPESTATUS[0]}
    SESSION_STARTED=0 LIMIT_HIT=0 RESET_EPOCH="" TRANSIENT=0 AUTH_ERROR=0
    # shellcheck disable=SC1091
    [ -f "$STATE/last_result.env" ] && source "$STATE/last_result.env"
}

# Like run_claude, but waits out usage limits and network/API hiccups and tries again.
# Returns 1 only if a stop was requested while waiting. Sets STARTED_ONCE=1 once the session exists.
run_claude_patiently() { # task mode session_id prompt logfile
    local task="$1" mode="$2" sid="$3" prompt="$4" log="$5" transient=0 until
    while :; do
        run_claude "$mode" "$sid" "$prompt" "$log"
        if [ "$SESSION_STARTED" = 1 ]; then STARTED_ONCE=1; mode=resume; fi
        if [ "$AUTH_ERROR" = 1 ]; then
            FINAL_STATE="STOPPED: Claude Code is not logged in on this machine (run 'claude' once and log in)"
            say "Claude Code isn't logged in. Stopping."
            exit 2
        fi
        if [ "$LIMIT_HIT" = 1 ]; then
            local t_now; t_now=$(date +%s)
            if [ -n "$RESET_EPOCH" ]; then   # known reset time: wait until then (+ a small grace)
                until=$(( (RESET_EPOCH > t_now ? RESET_EPOCH : t_now) + LIMIT_GRACE_S ))
            else                             # unknown: try again later
                until=$(( t_now + LIMIT_FALLBACK_S ))
            fi
            write_status "waiting: usage limit reached" "$task" "resuming at $(date -d "@$until" '+%a %H:%M') (session $sid)"
            say "Usage limit reached. Waiting until $(date -d "@$until" '+%a %H:%M'), then resuming the same session."
            sleep_until "$until" || return 1
            say "Usage limit over; resuming $task (session $sid)."
            write_status "working" "$task" "resumed after the usage limit · session $sid"
            [ "$mode" = resume ] && prompt="$(continue_prompt "$task")"
            continue
        fi
        if [ "$TRANSIENT" = 1 ] && [ "$transient" -lt "$MAX_TRANSIENT" ]; then
            transient=$((transient + 1))
            write_status "waiting: network/API problem" "$task" "retry $transient/$MAX_TRANSIENT in $((TRANSIENT_WAIT_S / 60)) min"
            say "Network/API problem; retrying in $((TRANSIENT_WAIT_S / 60)) min."
            sleep_until $(( $(date +%s) + TRANSIENT_WAIT_S )) || return 1
            say "Retrying $task (session $sid)."
            write_status "working" "$task" "retry $transient/$MAX_TRANSIENT after a network/API problem · session $sid"
            [ "$mode" = resume ] && prompt="$(continue_prompt "$task")"
            continue
        fi
        return 0
    done
}

# ---------- git ----------
git_sync() {
    git -C "$REPO" pull -q --ff-only origin main 2>>"$STATE/loop.log" \
        || say "note: git pull skipped (local changes or network); continuing with the local copy"
}

ensure_pushed() { # task session_id log
    local task="$1" sid="$2" log="$3"
    if [ -n "$(git -C "$REPO" status --porcelain)" ]; then
        say "$task left uncommitted changes; asking the session to commit and push them"
        run_claude_patiently "$task" resume "$sid" "Commit all remaining work for $task (never secrets or data/), then push to origin main. Reply with one line." "$log" || return 1
    fi
    if [ -n "$(git -C "$REPO" log '@{u}..HEAD' --oneline 2>/dev/null)" ]; then
        git -C "$REPO" push -q origin HEAD:main 2>>"$STATE/loop.log" || say "warning: push failed; it will be retried after the next task"
    fi
}

set_blocked() { # task reason — works whether the line is ticked or not
    python3 -I - "$PROGRESS" "$1" "$2" <<'PY'
import re, sys
path, task, reason = sys.argv[1:4]
s = open(path).read()
pat = re.compile(r"^- \[[ x]\] (?:⏸️ )?\*\*" + re.escape(task) + r"\*\* (.*)$", re.M)
s = pat.sub(lambda m: f"- [ ] ⏸️ **{task}** {m.group(1)} (needs: {reason})", s, count=1)
open(path, "w").write(s)
PY
    git -C "$REPO" add PROGRESS.md
    git -C "$REPO" commit -q -m "$1: blocked by the agent loop" -m "$2" \
        && git -C "$REPO" push -q origin HEAD:main 2>>"$STATE/loop.log"
}

# ---------- quality gate ----------
# Lint + tests for whatever parts of the project exist yet. Output goes to $1.
verify_repo() { # logfile
    local log="$1" ok=0
    : >"$log"
    if [ -f "$REPO/backend/pyproject.toml" ]; then
        echo "### backend: ruff + pytest" >>"$log"
        (cd "$REPO/backend" && timeout "$VERIFY_TIMEOUT" uv run ruff check . \
            && timeout "$VERIFY_TIMEOUT" uv run pytest -q -m "not slow") >>"$log" 2>&1 9>&- || ok=1
    fi
    if [ -f "$REPO/frontend/package.json" ]; then
        echo "### frontend: lint + tests" >>"$log"
        (cd "$REPO/frontend" && timeout "$VERIFY_TIMEOUT" npm run --silent lint \
            && timeout "$VERIFY_TIMEOUT" npm test --silent -- --run) >>"$log" 2>&1 9>&- || ok=1
    fi
    return $ok
}

code_without_tests() { # start_sha — true if app code changed since start_sha but no tests did
    local files; files="$(git -C "$REPO" diff --name-only "$1" HEAD 2>/dev/null)"
    local code tests
    code="$(grep -E '^(backend/parking/|frontend/src/|tools/slot-editor/|tools/flow-tally/).*\.(py|ts|tsx|js)$' <<<"$files" \
            | grep -vE '(\.test\.|\.spec\.|/__fixtures__/)')"
    tests="$(grep -E '^backend/tests/|\.(test|spec)\.(ts|tsx|js)$|^frontend/e2e/' <<<"$files")"
    [ -n "$code" ] && [ -z "$tests" ]
}

quality_gate() { # task session_id log start_sha — returns 1 if it still fails after the fix rounds
    local task="$1" sid="$2" log="$3" start="$4" round=0
    local vlog="$STATE/logs/$(date +%Y%m%d-%H%M%S)-$task-verify.log"

    if [ -n "$start" ] && code_without_tests "$start"; then
        say "$task changed code but added no tests; sending it back to add them"
        write_status "checking: asking for tests" "$task"
        run_claude_patiently "$task" resume "$sid" "$(tests_prompt "$task")" "$log" || return 2
    fi

    while :; do
        write_status "checking: running lint + tests" "$task"
        say "Running lint + tests after $task"
        if verify_repo "$vlog"; then
            say "Lint + tests pass."
            return 0
        fi
        round=$((round + 1))
        if [ "$round" -gt "$MAX_FIX_ROUNDS" ]; then
            say "Lint/tests still fail after $MAX_FIX_ROUNDS fix rounds. See $vlog"
            return 1
        fi
        say "Lint/tests fail after $task; sending the output back to the session (fix round $round/$MAX_FIX_ROUNDS)"
        write_status "fixing: lint/tests fail" "$task" "fix round $round/$MAX_FIX_ROUNDS · $vlog"
        run_claude_patiently "$task" resume "$sid" "$(fix_prompt "$task" "$vlog")" "$log" || return 2
    done
}

# ---------- one task ----------
run_task() { # task [session_id_to_resume] [start_sha]
    local task="$1" sid="${2:-}" start="${3:-}" mode=new prompt attempts=0 gate
    local log; log="$STATE/logs/$(date +%Y%m%d-%H%M%S)-$task.log"
    STARTED_ONCE=0
    if [ -n "$sid" ]; then mode=resume; STARTED_ONCE=1; prompt="$(continue_prompt "$task")"
    else sid="$(python3 -I -c 'import uuid; print(uuid.uuid4())')"; prompt="$(render_prompt "$task")"; fi
    [ -z "$start" ] && start="$(git -C "$REPO" rev-parse HEAD)"
    : >"$CURRENT_LOG"

    while :; do
        echo "$task $sid $start" >"$STATE/current_task"
        write_status "working" "$task" "attempt $((attempts + 1))/$MAX_ATTEMPTS · session $sid"
        say "=== $task: $(task_title "$task") ($mode session $sid)"
        run_claude_patiently "$task" "$mode" "$sid" "$prompt" "$log" || return 1
        [ "$STARTED_ONCE" = 1 ] && mode=resume && prompt="$(continue_prompt "$task")"

        case "$(task_state "$task")" in
            done)
                quality_gate "$task" "$sid" "$log" "$start"; gate=$?
                [ "$gate" -eq 2 ] && return 1                      # stop requested while waiting
                ensure_pushed "$task" "$sid" "$log" || return 1
                if [ "$gate" -ne 0 ]; then
                    set_blocked "$task" "lint/tests fail after this task and the session couldn't fix them; see ~/.parking-loop/logs"
                    rm -f "$STATE/current_task"
                    FINAL_STATE="STOPPED: lint/tests fail after $task. Fix them (or ask Claude to), remove the ⏸️ from $task in PROGRESS.md, then start the loop again"
                    say "$FINAL_STATE"
                    exit 3
                fi
                DONE_THIS_RUN+=("$task")
                say "$task done."
                rm -f "$STATE/current_task"
                return 0 ;;
            blocked)
                ensure_pushed "$task" "$sid" "$log" || return 1
                say "$task is blocked, waiting on Iulian: $(task_line "$task" | grep -oP '\(needs: .*\)$')"
                rm -f "$STATE/current_task"
                return 0 ;;
            missing)
                say "$task no longer exists in PROGRESS.md; moving on."
                rm -f "$STATE/current_task"
                return 0 ;;
        esac

        attempts=$((attempts + 1))
        if [ "$attempts" -ge "$MAX_ATTEMPTS" ]; then
            say "$task still not finished after $attempts session runs (exit code $RC); marking it blocked."
            set_blocked "$task" "the agent loop couldn't finish it after $attempts tries; see ~/.parking-loop/logs"
            rm -f "$STATE/current_task"
            return 0
        fi
        say "$task isn't finished yet (exit code $RC); resuming the session (try $((attempts + 1))/$MAX_ATTEMPTS)."
    done
}

# ---------- main ----------
FINAL_STATE="stopped (start it again with start.sh; an interrupted task resumes where it left off)"
trap 'write_status "$FINAL_STATE"; say "Loop ended: $FINAL_STATE"' EXIT
say "Loop started in $REPO (state: $STATE)"

while :; do
    if stop_requested; then rm -f "$STATE/STOP"; say "Stop requested."; exit 0; fi
    git_sync

    if [ -f "$STATE/current_task" ]; then          # an interrupted task: resume its session
        read -r ctask csid cstart <"$STATE/current_task"
        if [ "$(task_state "$ctask")" != blocked ] && [ "$(task_state "$ctask")" != missing ]; then
            run_task "$ctask" "$csid" "${cstart:-}" || true
            continue
        fi
        rm -f "$STATE/current_task"
    fi

    task="$(next_task)"
    if [ -z "$task" ]; then
        if [ "$(open_count)" -eq 0 ]; then
            FINAL_STATE="FINISHED: every task in PROGRESS.md is done"
            exit 0
        fi
        write_status "waiting for you: only blocked tasks are left" "" \
            "unblock one by removing ⏸️ from its line in PROGRESS.md (GitHub works too); next check $(date -d "+$IDLE_POLL_S sec" +%H:%M)"
        say "Only blocked tasks left ($(blocked_list)). Checking again in $((IDLE_POLL_S / 60)) min."
        sleep_until $(( $(date +%s) + IDLE_POLL_S )) || true
        continue
    fi
    run_task "$task" || true
done
