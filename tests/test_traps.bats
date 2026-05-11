#!/usr/bin/env bats
# Bash-level tests for traps.sh. Requires bats-core.
#
# Strategy: source traps.sh inside a sub-bash with MAGICWAND_FIFO pointed at
# a temp FIFO; run a small script; collect what was written to the FIFO and
# parse it with jq (or a python one-liner) to make assertions.

setup() {
    BATS_TMPDIR="${BATS_TMPDIR:-/tmp}"
    TMP="$(mktemp -d "$BATS_TMPDIR/mw-bats-XXXXXX")"
    FIFO="$TMP/fifo"
    OUT="$TMP/out"
    mkfifo "$FIFO"
    # Hold the FIFO open RDWR on fd 9 in this shell so (a) the open doesn't
    # block waiting for a reader and (b) the backgrounded `cat` doesn't EOF
    # while the test is in flight. `<>` is bash's read-write open and
    # never blocks on a FIFO.
    exec 9<>"$FIFO"
    cat "$FIFO" > "$OUT" &
    READER_PID=$!
    export MAGICWAND_FIFO="$FIFO"
    export MAGICWAND_PIDFILE="$TMP/pid"
    export MAGICWAND_ERROR_LOG="$TMP/err"
    export MAGICWAND_STRICT=1
    TRAPS_SH="${BATS_TEST_DIRNAME}/../magicwand/shell/traps.sh"
}

teardown() {
    exec 9>&- 2>/dev/null || true
    kill "$READER_PID" 2>/dev/null || true
    wait "$READER_PID" 2>/dev/null || true
    rm -rf "$TMP"
}

# Run a script in a sub-bash with traps installed and return the FIFO output.
run_with_traps() {
    local script="$1"
    bash -c "
        set -E
        source '$TRAPS_SH'
        $script
    " >/dev/null 2>&1 || true
    # Give the cat a moment to flush.
    sleep 0.2
    cat "$OUT"
}

@test "DEBUG trap emits cmd_start for each command" {
    out="$(run_with_traps 'echo one > /dev/null; echo two > /dev/null; echo three > /dev/null')"
    count=$(grep -c '"event":"cmd_start"' <<<"$out")
    [ "$count" -ge 3 ]
}

@test "EXIT trap emits a finalize event" {
    out="$(run_with_traps 'true')"
    grep -q '"event":"finalize"' <<<"$out"
}

@test "finalize event carries the script exit code" {
    out="$(run_with_traps 'exit 7')"
    grep -q '"event":"finalize".*"exit_code":7' <<<"$out"
}

@test "ERR trap emits err event when a command fails (set -e)" {
    out="$(run_with_traps 'set -e; false')"
    grep -q '"event":"err"' <<<"$out"
}

@test "EXIT trap fires only once" {
    out="$(run_with_traps 'true')"
    count=$(grep -c '"event":"finalize"' <<<"$out")
    [ "$count" -eq 1 ]
}

@test "command strings are JSON-escaped" {
    out="$(run_with_traps 'echo "hi \"there\"" > /dev/null')"
    # Must not break the JSON line by the embedded quotes.
    while IFS= read -r line; do
        [ -z "$line" ] && continue
        echo "$line" | python3 -c 'import json,sys;json.loads(sys.stdin.read())'
    done <<<"$out"
}
