# shellcheck shell=bash disable=SC2317
# SC2317: the `return 0 2>/dev/null || exit 0` idiom is reachable in both
# arms depending on whether the script is sourced or executed.
# magicwand bash trap library.
#
# Sourced into the WDL task's command shell by `magicwand init`. Installs:
#   DEBUG  — fires before every simple command. Emits cmd_start, plus
#            cmd_end for the previous command (using $? from the previous run).
#   ERR    — emits an err event without exiting.
#   EXIT   — emits the trailing cmd_end + finalize, then waits on the sidecar.
#
# Required env vars (set by `magicwand init` before sourcing):
#   MAGICWAND_FIFO       — path to the FIFO the sidecar reads
#   MAGICWAND_PIDFILE    — path containing the sidecar PID
#   MAGICWAND_ERROR_LOG  — path the sidecar's stderr is redirected to
#   MAGICWAND_STRICT     — '1' to propagate trap errors (test mode)

if [[ -z "${MAGICWAND_FIFO:-}" ]]; then
    echo "magicwand: traps.sh sourced without MAGICWAND_FIFO set" >&2
    return 0 2>/dev/null || exit 0
fi

# EPOCHREALTIME requires bash 5.0+. Hard-fail (within the soft-fail boundary)
# rather than silently lose timestamps.
if [[ -z "${EPOCHREALTIME:-}" ]]; then
    echo "magicwand: bash >= 5.0 required for EPOCHREALTIME (have ${BASH_VERSION:-unknown}); traps not installed" >&2
    return 0 2>/dev/null || exit 0
fi

# Tracking state. All __MW_* names are reserved.
export __MW_DEPTH=0
export __MW_CMD_IDX=0
unset __MW_LAST_CMD __MW_LAST_LINENO __MW_LAST_TS __MW_FINALIZED __MW_FIFO_FD

# Open the FIFO O_RDWR once for the lifetime of this shell. RDWR opens
# never block waiting for a counterpart, and writes succeed even after the
# sidecar (the original reader) exits — because the kernel still sees a
# reader (us, on the same fd). Without this, every `printf >> $FIFO` after
# sidecar shutdown blocks forever in O_WRONLY open().
#
# CAREFUL: `exec ... 2>/dev/null` (no command) PERMANENTLY redirects the
# shell's stderr to /dev/null — it's not just for the exec invocation.
# Wrap the exec in a `{ ... }` group so the stderr redirect applies only
# inside the group; on success the new fd persists in the parent shell;
# on failure the error message is swallowed and stderr reverts after the
# group.
if ! { exec {__MW_FIFO_FD}<>"$MAGICWAND_FIFO"; } 2>/dev/null; then
    echo "magicwand: could not open FIFO fd; trap emissions disabled" >&2
    unset __MW_FIFO_FD
fi

# Emit one JSON record. Best-effort: never blocks, never raises.
__mw_emit() {
    if [[ -n "${__MW_FINALIZED:-}" ]]; then
        return 0
    fi
    if [[ -z "${__MW_FIFO_FD:-}" ]]; then
        return 0
    fi
    # shellcheck disable=SC2261
    # SC2261 false positive: 1>&"$__MW_FIFO_FD" redirects fd 1, 2>/dev/null
    # redirects fd 2; they don't compete. The `|| true` swallows printf
    # failures (closed fd, EPIPE) without spamming the user's stderr.
    printf '%s\n' "$1" 1>&"$__MW_FIFO_FD" 2>/dev/null || true
}

# JSON-string-escape stdin. Avoids needing python/jq inside the trap.
__mw_json_escape() {
    local s="$1"
    s="${s//\\/\\\\}"
    s="${s//\"/\\\"}"
    s="${s//$'\n'/\\n}"
    s="${s//$'\r'/\\r}"
    s="${s//$'\t'/\\t}"
    printf '%s' "$s"
}

__mw_debug_trap() {
    local _rc=$?
    # Recursion / self-fire guard.
    if (( __MW_DEPTH > 0 )); then
        return 0
    fi
    __MW_DEPTH=1

    local _cmd="$BASH_COMMAND"
    local _lineno="${BASH_LINENO[0]:-0}"
    local _ts="$EPOCHREALTIME"

    # Filter out our own machinery so the trap doesn't trace itself.
    case "$_cmd" in
        __mw_*|"trap "*|"unset "__MW_*) __MW_DEPTH=0; return 0 ;;
    esac

    # Close the previous command's window with its exit code.
    if [[ -n "${__MW_LAST_CMD:-}" ]]; then
        local _prev
        _prev=$(__mw_json_escape "$__MW_LAST_CMD")
        __mw_emit "{\"event\":\"cmd_end\",\"ts\":$_ts,\"lineno\":${__MW_LAST_LINENO:-0},\"exit_code\":$_rc,\"command\":\"$_prev\"}"
    fi

    local _esc
    _esc=$(__mw_json_escape "$_cmd")
    __MW_CMD_IDX=$((__MW_CMD_IDX + 1))
    __mw_emit "{\"event\":\"cmd_start\",\"ts\":$_ts,\"lineno\":$_lineno,\"command\":\"$_esc\",\"cmd_idx\":$__MW_CMD_IDX}"

    __MW_LAST_CMD="$_cmd"
    __MW_LAST_LINENO="$_lineno"
    __MW_LAST_TS="$_ts"
    __MW_DEPTH=0
}

__mw_err_trap() {
    local _rc=$?
    if (( __MW_DEPTH > 0 )); then
        return 0
    fi
    __MW_DEPTH=1
    local _ts="$EPOCHREALTIME"
    local _lineno="${BASH_LINENO[0]:-0}"
    local _esc
    _esc=$(__mw_json_escape "${__MW_LAST_CMD:-}")
    __mw_emit "{\"event\":\"err\",\"ts\":$_ts,\"lineno\":$_lineno,\"exit_code\":$_rc,\"command\":\"$_esc\"}"
    __MW_DEPTH=0
}

__mw_exit_trap() {
    local _rc=$?
    if [[ -n "${__MW_FINALIZED:-}" ]]; then
        return 0
    fi
    __MW_DEPTH=1  # disable DEBUG during shutdown

    local _ts="$EPOCHREALTIME"

    # Close the in-flight command, if any.
    if [[ -n "${__MW_LAST_CMD:-}" ]]; then
        local _esc
        _esc=$(__mw_json_escape "$__MW_LAST_CMD")
        __mw_emit "{\"event\":\"cmd_end\",\"ts\":$_ts,\"lineno\":${__MW_LAST_LINENO:-0},\"exit_code\":$_rc,\"command\":\"$_esc\"}"
    fi
    __mw_emit "{\"event\":\"finalize\",\"ts\":$_ts,\"exit_code\":$_rc}"

    # Now silence subsequent emits (DEBUG/ERR traps that fire during the
    # pidfile wait below). Must come AFTER the emits above so __mw_emit's
    # __MW_FINALIZED short-circuit doesn't swallow our own finalize event.
    __MW_FINALIZED=1

    # Wait for sidecar to flush. We watch for pidfile removal — the sidecar
    # unlinks its pidfile at the end of its `finally` block. `kill -0` is
    # unreliable here because container bash doesn't reap zombies.
    if [[ -n "${MAGICWAND_PIDFILE:-}" && -e "$MAGICWAND_PIDFILE" ]]; then
        local _waited=0
        while [[ -e "$MAGICWAND_PIDFILE" ]] && (( _waited < 30 )); do
            sleep 1
            _waited=$((_waited + 1))
        done
        # Last-resort SIGTERM to whatever PID was recorded, if pidfile is
        # still there after the timeout.
        if [[ -e "$MAGICWAND_PIDFILE" ]]; then
            local _pid
            _pid=$(<"$MAGICWAND_PIDFILE") 2>/dev/null || _pid=""
            [[ -n "$_pid" ]] && kill -TERM "$_pid" 2>/dev/null || true
        fi
    fi

    return 0
}

set -o functrace
set -o errtrace
trap '__mw_debug_trap' DEBUG
trap '__mw_err_trap'   ERR
trap '__mw_exit_trap'  EXIT
