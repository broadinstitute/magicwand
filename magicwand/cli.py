"""Command-line entry point for magicwand.

Subcommands:
    init     — print a shell snippet that sets up the FIFO, starts the sidecar,
               and sources traps.sh into the calling shell.
    finalize — emit a finalize event and wait on the sidecar.
    log      — push a one-shot key=value record into the running W&B run.
    doctor   — environment checks; pinpoints likely failure modes.
    parsers  — list built-in parsers.

`init` is intended to be evaluated in the calling shell::

    magicwand() {
        case "$1" in
            init) shift; eval "$(MAGICWAND_TASK_PID=$$ command magicwand-cli init "$@")" ;;
            *)    command magicwand-cli "$@" ;;
        esac
    }
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import socket
import sys
import time
from pathlib import Path
from typing import List, Optional

from . import __version__
from .context import detect
from .parsers import known as known_parsers


PACKAGE_DIR = Path(__file__).resolve().parent
TRAPS_SH = PACKAGE_DIR / "shell" / "traps.sh"


def _fifo_write(path: str, payload: str) -> None:
    """Write to a FIFO without blocking when no reader is present.

    O_RDWR opens never block, and any subsequent writes succeed because the
    kernel sees a reader (this fd, briefly). Falls back to noop on EPIPE.
    """
    fd = os.open(path, os.O_RDWR)
    try:
        os.write(fd, payload.encode())
    except BrokenPipeError:
        pass
    finally:
        os.close(fd)


def _strict() -> bool:
    return os.environ.get("MAGICWAND_STRICT", "") == "1"


def _outer(fn):
    """Soft-fail outer boundary. Logs traceback to stderr; exits 0 unless strict."""
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except SystemExit:
            raise
        except Exception:
            import traceback
            traceback.print_exc(file=sys.stderr)
            if _strict():
                raise
            return 0
    return wrapper


# ---------- init ----------


@_outer
def cmd_init(args: argparse.Namespace) -> int:
    task_pid_env = os.environ.get("MAGICWAND_TASK_PID")
    if not task_pid_env:
        print(
            "magicwand init: MAGICWAND_TASK_PID not set. Did you call this "
            "directly instead of via the `magicwand` shell function?",
            file=sys.stderr,
        )
        return 1
    task_pid = int(task_pid_env)

    fifo = f"/tmp/magicwand-{task_pid}.fifo"
    pidfile = f"/tmp/magicwand-{task_pid}.pid"
    error_log = f"/tmp/magicwand-{task_pid}-error.log"
    run_url_file = f"/tmp/magicwand-{task_pid}.url"
    task_log_fifo = f"/tmp/magicwand-{task_pid}.tasklog.fifo"

    # Anonymous online runs were deprecated by W&B (Dec 2025); the API
    # endpoint that minted ephemeral keys now returns 401. So mode auto-
    # detects: with a WANDB_API_KEY present (env or --api-key) we go
    # online and wandb prints the live "View run at <url>" line; without
    # a key we go offline and wandb writes a local run dir the user can
    # `wandb sync` from a machine that has a key. --offline forces offline
    # even if a key is set.
    api_key = args.api_key or os.environ.get("WANDB_API_KEY")
    if args.offline:
        mode = "offline"
    elif api_key:
        mode = "online"
    else:
        mode = "offline"
    if args.api_key:
        api_key_export = f'export WANDB_API_KEY={shlex.quote(args.api_key)}\n'
    else:
        api_key_export = ""

    parsers_csv = ",".join(args.parsers) if args.parsers else ""
    strict_export = "export MAGICWAND_STRICT=1\n" if args.strict else ""

    if mode == "online":
        mode_banner = (
            'magicwand: starting W&B online (look for "wandb: View run at ..." below)...'
        )
    else:
        mode_banner = (
            'magicwand: starting W&B offline (no WANDB_API_KEY set; '
            "sync later with 'wandb sync ./wandb/offline-run-*' from a "
            "machine that has a key)..."
        )

    sidecar_cmd = [sys.executable, "-m", "magicwand.sidecar"]

    # The shell snippet printed below is eval'd in the user's WDL command
    # shell. It MUST be idempotent against re-sourcing and MUST NOT exit on
    # error — this is the boundary where soft-fail starts.
    script = f"""\
# --- magicwand init (v{__version__}) ---
{strict_export}{api_key_export}\
export MAGICWAND_FIFO={shlex.quote(fifo)}
export MAGICWAND_PIDFILE={shlex.quote(pidfile)}
export MAGICWAND_ERROR_LOG={shlex.quote(error_log)}
export MAGICWAND_RUN_URL_FILE={shlex.quote(run_url_file)}
export MAGICWAND_TASK_LOG_FIFO={shlex.quote(task_log_fifo)}
export MAGICWAND_TASK_PID={task_pid}
export MAGICWAND_MODE={shlex.quote(mode)}
export MAGICWAND_SAMPLING_INTERVAL={args.sampling_interval}
export MAGICWAND_PARSERS={shlex.quote(parsers_csv)}

if [[ ! -p "$MAGICWAND_FIFO" ]]; then
    rm -f "$MAGICWAND_FIFO" 2>/dev/null
    mkfifo "$MAGICWAND_FIFO" || {{
        echo "magicwand: failed to create FIFO at $MAGICWAND_FIFO" >&2
        return 0 2>/dev/null || true
    }}
fi

# Set up a task-log FIFO. Bash will tee its stdout/stderr through it, and
# the sidecar reads it in a thread and forwards lines to its own
# (wandb-captured) sys.stdout — that's how wandb's Logs tab gets the
# user's bash output.
if [[ ! -p "$MAGICWAND_TASK_LOG_FIFO" ]]; then
    rm -f "$MAGICWAND_TASK_LOG_FIFO" 2>/dev/null
    mkfifo "$MAGICWAND_TASK_LOG_FIFO" || {{
        echo "magicwand: failed to create task-log FIFO" >&2
        return 0 2>/dev/null || true
    }}
fi
# Hold the task-log FIFO open RDWR in this shell so tee writers never
# block on a missing reader (bash itself is the safety reader).
exec {{__MW_TASK_LOG_FD}}<>"$MAGICWAND_TASK_LOG_FIFO"

# Dup the WDL task's original stdout/stderr to dedicated high fds. The
# tee pipelines below write to these dups so user output still flows
# straight to the Cromwell task log; the sidecar uses them too.
exec {{__MW_TASK_OUT}}>&1
exec {{__MW_TASK_ERR}}>&2

# Tee bash stdio into (TASK_LOG_FIFO, original_fd). All subsequent
# bash output is duplicated: one copy reaches the user's task log
# unchanged, one copy is read by the sidecar and pushed into wandb's
# Logs tab. The teefiles use `tee -a` to be append-safe across
# re-sourcing.
exec > >(tee -a "$MAGICWAND_TASK_LOG_FIFO" >&"${{__MW_TASK_OUT}}") \
    2> >(tee -a "$MAGICWAND_TASK_LOG_FIFO" >&"${{__MW_TASK_ERR}}")

rm -f "$MAGICWAND_RUN_URL_FILE" 2>/dev/null
echo {shlex.quote(mode_banner)} >&2

# Background the sidecar. Stdout -> /dev/null so the sidecar's own
# print()s (including the tail-thread's reprints of bash output) don't
# loop back through tee into the FIFO. Stderr -> the dup'd original
# stderr so wandb's "View run at <url>" message and any sidecar
# tracebacks reach the user's task log directly. PYTHONWARNINGS=ignore
# silences the pydantic UnsupportedFieldAttribute spam wandb 0.21 emits
# on import — pure noise, only thing suppressed.
setsid env PYTHONWARNINGS=ignore {' '.join(shlex.quote(c) for c in sidecar_cmd)} \
    >/dev/null 2>&"${{__MW_TASK_ERR}}" &
echo $! >"$MAGICWAND_PIDFILE"

# Source traps last so the trap installation itself doesn't trigger DEBUG.
source {shlex.quote(str(TRAPS_SH))}
# --- end magicwand init ---
"""
    sys.stdout.write(script)
    sys.stdout.flush()
    return 0


# ---------- finalize ----------


@_outer
def cmd_finalize(args: argparse.Namespace) -> int:
    fifo = os.environ.get("MAGICWAND_FIFO")
    pidfile = os.environ.get("MAGICWAND_PIDFILE")
    if not fifo or not os.path.exists(fifo):
        # Nothing to finalize. Quiet exit so traps don't spam stderr.
        return 0
    rec = {"event": "finalize", "ts": time.time(), "exit_code": args.exit_code}
    _fifo_write(fifo, json.dumps(rec) + "\n")

    # Wait for the sidecar to clear its pidfile, which it does at the end of
    # its `finally` block. Polling `kill -0 PID` is unreliable in container
    # PID-1-is-bash setups: bash doesn't reap zombies, so the PID stays
    # valid long after run.finish() has returned.
    if pidfile:
        deadline = time.time() + 30
        while time.time() < deadline:
            if not os.path.exists(pidfile):
                break
            time.sleep(0.5)
    return 0


# ---------- log ----------


def _coerce(v: str):
    try:
        return int(v)
    except ValueError:
        pass
    try:
        return float(v)
    except ValueError:
        pass
    return v


@_outer
def cmd_log(args: argparse.Namespace) -> int:
    fifo = os.environ.get("MAGICWAND_FIFO")
    if not fifo or not os.path.exists(fifo):
        print("magicwand log: MAGICWAND_FIFO not set or missing; "
              "did you run `magicwand init` first?", file=sys.stderr)
        return 1
    data: dict = {}
    for kv in args.kvs:
        if "=" not in kv:
            print(f"magicwand log: expected key=value, got {kv!r}", file=sys.stderr)
            return 1
        k, v = kv.split("=", 1)
        data[k] = _coerce(v)
    rec = {"event": "log", "ts": time.time(), "data": data}
    _fifo_write(fifo, json.dumps(rec) + "\n")
    return 0


# ---------- doctor ----------


@_outer
def cmd_doctor(args: argparse.Namespace) -> int:
    print(f"magicwand {__version__}")
    print(f"  python: {sys.version.split()[0]} ({sys.executable})")
    print(f"  hostname: {socket.gethostname()}")

    # bash version
    bash = os.environ.get("BASH_VERSION") or _shell_out("bash --version | head -1")
    print(f"  bash: {bash.strip()}")

    # wandb import
    try:
        import wandb
        print(f"  wandb: {wandb.__version__}")
    except ImportError as e:
        print(f"  wandb: NOT INSTALLED ({e})")

    # FIFO permissions
    fifo_dir = "/tmp"
    print(f"  fifo dir writable: {os.access(fifo_dir, os.W_OK)} ({fifo_dir})")

    # Context detection
    ctx = detect()
    print(f"  detected engine: {ctx.engine}")
    print(f"  detected task: {ctx.task_name}")
    print(f"  detected workflow: {ctx.workflow_uuid or ctx.workflow_name}")
    print(f"  run name: {ctx.run_name}")

    # Parsers
    parsers = known_parsers()
    print(f"  parsers built-in: {len(parsers)}")
    for name in parsers:
        print(f"    - {name}")

    return 0


def _shell_out(cmd: str) -> str:
    import subprocess
    try:
        return subprocess.check_output(cmd, shell=True, text=True, timeout=5)
    except subprocess.SubprocessError:
        return ""


# ---------- parsers ----------


@_outer
def cmd_parsers(args: argparse.Namespace) -> int:
    parsers = known_parsers()
    if not parsers:
        print("(no parsers built in)")
        return 0
    for name in parsers:
        print(name)
    return 0


# ---------- argparse ----------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="magicwand-cli",
        description="WDL task observability shim for Weights & Biases.",
    )
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd", required=True)

    init = sub.add_parser("init", help="emit shell snippet to instrument the current shell")
    init.add_argument("--offline", action="store_true",
                      help="run W&B in offline mode (default: online + anonymous)")
    init.add_argument("--api-key", default=None, help="set WANDB_API_KEY for this run")
    # NOTE: --anonymous removed. W&B deprecated anonymous account creation
    # in Dec 2025; the endpoint now returns 401. Mode auto-detects from
    # WANDB_API_KEY presence (online with key, offline without).
    init.add_argument("--sampling-interval", type=float, default=2.0,
                      help="seconds between resource samples (default: 2.0)")
    init.add_argument("--strict", action="store_true",
                      help="propagate errors instead of soft-failing")
    init.add_argument("--parsers", action="append", default=[],
                      help="comma-separated parser names; can be repeated")
    init.set_defaults(func=cmd_init)

    fin = sub.add_parser("finalize", help="emit finalize event and wait on sidecar")
    fin.add_argument("exit_code", type=int)
    fin.set_defaults(func=cmd_finalize)

    lg = sub.add_parser("log", help="push key=value records to the running W&B run")
    lg.add_argument("kvs", nargs="+", help="one or more key=value pairs")
    lg.set_defaults(func=cmd_log)

    doc = sub.add_parser("doctor", help="environment + detection sanity checks")
    doc.set_defaults(func=cmd_doctor)

    par = sub.add_parser("parsers", help="list registered parsers")
    par.set_defaults(func=cmd_parsers)

    return p


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    # Flatten --parsers (may be repeated and/or comma-separated).
    if hasattr(args, "parsers") and args.parsers:
        flat: List[str] = []
        for chunk in args.parsers:
            flat.extend(p.strip() for p in chunk.split(",") if p.strip())
        args.parsers = flat
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
