#!/usr/bin/env bash
# Works through PROGRESS.md one task at a time, each task in a fresh Claude Code
# session. Waits out usage limits and resumes the same session afterwards.
# Start it with start.sh (inside tmux); see README.md.
set -uo pipefail
shopt -u patsub_replacement 2>/dev/null || true

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="${REPO:-$(cd "$HERE/../.." && pwd)}"
STATE="${STATE:-$HOME/.parking-loop}"
TASK_TIMEOUT="${TASK_TIMEOUT:-3h}"        # max wall time per session run
MAX_ATTEMPTS="${MAX_ATTEMPTS:-3}"         # session runs per task before giving up on it
MAX_TRANSIENT="${MAX_TRANSIENT:-10}"      # retries for network/API hiccups per task
IDLE_POLL_S="${IDLE_POLL_S:-900}"         # how often to re-check when only blocked tasks remain
LIMIT_FALLBACK_S="${LIMIT_FALLBACK_S:-1800}"  # re-try interval when a limit's reset time is unknown
TRANSIENT_WAIT_S="${TRANSIENT_WAIT_S:-300}"
LIMIT_GRACE_S="${LIMIT_GRACE_S:-90}"          # extra wait after the announced reset time

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

stop_requested() { [ -f "$STATE/STOP" ]; }

sleep_until() { # epoch — wakes early if a stop is requested
    local left
    while left=$(( $1 - $(date +%s) )); [ "$left" -gt 0 ]; do
        stop_requested && return 1
        sleep $(( left < 30 ? left : 30 ))
    done
}

run_claude() { # mode(new|resume) session_id prompt logfile ; sets RC and sources the result summary
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

git_sync() {
    git -C "$REPO" pull -q --ff-only origin main 2>>"$STATE/loop.log" \
        || say "note: git pull skipped (local changes or network); continuing with the local copy"
}

ensure_pushed() { # task session_id log
    local task="$1" sid="$2" log="$3"
    if [ -n "$(git -C "$REPO" status --porcelain)" ]; then
        say "$task left uncommitted changes; asking the session to commit and push them"
        run_claude resume "$sid" "Commit all remaining work for $task (never secrets or data/), then push to origin main. Reply with one line." "$log"
    fi
    if [ -n "$(git -C "$REPO" log '@{u}..HEAD' --oneline 2>/dev/null)" ]; then
        git -C "$REPO" push -q origin HEAD:main 2>>"$STATE/loop.log" || say "warning: push failed; it will be retried by the next task"
    fi
}

mark_blocked() { # task reason
    python3 -I - "$PROGRESS" "$1" "$2" <<'PY'
import re, sys
path, task, reason = sys.argv[1:4]
s = open(path).read()
pat = re.compile(r"^- \[ \] \*\*" + re.escape(task) + r"\*\* (.*)$", re.M)
s = pat.sub(lambda m: f"- [ ] ⏸️ **{task}** {m.group(1)} (needs: {reason})", s, count=1)
open(path, "w").write(s)
PY
    git -C "$REPO" add PROGRESS.md
    git -C "$REPO" commit -q -m "$1: blocked by the agent loop" -m "$2" \
        && git -C "$REPO" push -q origin HEAD:main 2>>"$STATE/loop.log"
}

run_task() { # task [session_id_to_resume]
    local task="$1" sid="${2:-}" mode=new prompt attempts=0 transient=0
    local log; log="$STATE/logs/$(date +%Y%m%d-%H%M%S)-$task.log"
    if [ -n "$sid" ]; then mode=resume; prompt="$(continue_prompt "$task")"
    else sid="$(python3 -I -c 'import uuid; print(uuid.uuid4())')"; prompt="$(render_prompt "$task")"; fi
    : >"$CURRENT_LOG"

    while :; do
        echo "$task $sid" >"$STATE/current_task"
        write_status "working" "$task" "attempt $((attempts + 1))/$MAX_ATTEMPTS · session $sid"
        say "=== $task: $(task_title "$task") ($mode session $sid)"
        run_claude "$mode" "$sid" "$prompt" "$log"
        [ "$SESSION_STARTED" = 1 ] && mode=resume && prompt="$(continue_prompt "$task")"

        if [ "$AUTH_ERROR" = 1 ]; then
            FINAL_STATE="STOPPED: Claude Code is not logged in on this machine (run 'claude' once and log in)"
            say "Claude Code isn't logged in. Stopping."
            exit 2
        fi
        if [ "$LIMIT_HIT" = 1 ]; then
            local until=$(( $(date +%s) + LIMIT_FALLBACK_S ))
            [ -n "$RESET_EPOCH" ] && [ "$RESET_EPOCH" -gt "$(date +%s)" ] && until=$((RESET_EPOCH + LIMIT_GRACE_S))
            write_status "waiting: usage limit reached" "$task" "resuming at $(date -d "@$until" '+%a %H:%M') (session $sid)"
            say "Usage limit reached. Waiting until $(date -d "@$until" '+%a %H:%M'), then resuming the same session."
            sleep_until "$until" || return 1
            continue
        fi

        case "$(task_state "$task")" in
            done)
                ensure_pushed "$task" "$sid" "$log"
                DONE_THIS_RUN+=("$task")
                say "$task done."
                rm -f "$STATE/current_task"
                return 0 ;;
            blocked)
                ensure_pushed "$task" "$sid" "$log"
                say "$task is blocked, waiting on Iulian: $(task_line "$task" | grep -oP '\(needs: .*\)$')"
                rm -f "$STATE/current_task"
                return 0 ;;
            missing)
                say "$task no longer exists in PROGRESS.md; moving on."
                rm -f "$STATE/current_task"
                return 0 ;;
        esac

        if [ "$TRANSIENT" = 1 ] && [ "$transient" -lt "$MAX_TRANSIENT" ]; then
            transient=$((transient + 1))
            write_status "waiting: network/API problem" "$task" "retry $transient/$MAX_TRANSIENT in $((TRANSIENT_WAIT_S / 60)) min"
            say "Network/API problem; retrying in $((TRANSIENT_WAIT_S / 60)) min."
            sleep_until $(( $(date +%s) + TRANSIENT_WAIT_S )) || return 1
            continue
        fi

        attempts=$((attempts + 1))
        if [ "$attempts" -ge "$MAX_ATTEMPTS" ]; then
            say "$task still not finished after $attempts session runs (exit code $RC); marking it blocked."
            mark_blocked "$task" "the agent loop couldn't finish it after $attempts tries; see $log"
            rm -f "$STATE/current_task"
            return 0
        fi
        say "$task isn't finished yet (exit code $RC); resuming the session (try $((attempts + 1))/$MAX_ATTEMPTS)."
    done
}

FINAL_STATE="stopped (start it again with start.sh; an interrupted task resumes where it left off)"
trap 'write_status "$FINAL_STATE"; say "Loop ended: $FINAL_STATE"' EXIT
say "Loop started in $REPO (state: $STATE)"

while :; do
    if stop_requested; then rm -f "$STATE/STOP"; say "Stop requested."; exit 0; fi
    git_sync

    if [ -f "$STATE/current_task" ]; then          # an interrupted task: resume its session
        read -r ctask csid <"$STATE/current_task"
        if [ "$(task_state "$ctask")" = open ]; then
            run_task "$ctask" "$csid" || continue
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
