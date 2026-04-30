"""Background sidecar process: reads the FIFO, owns the wandb run.

Started by `magicwand init` via setsid. Reads JSON-line events from
$MAGICWAND_FIFO, drives a wandb run keyed to the task's PID, and emits a
per-command resource summary table on finalize.

"cmd" throughout this module = a single bash command from the user's WDL
command block (each one becomes its own row in the summary table and a
categorical axis on the dashboard charts).

Strict-mode behavior:
    MAGICWAND_STRICT=1 → exceptions propagate (test/CI mode).
    Otherwise → outer try/except logs full traceback + exits 0 so the WDL
    task is never broken by sidecar bugs.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
import traceback
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Deque, Dict, List, Optional

import psutil
import wandb

from .context import detect
from .parsers import known as known_parsers, parse_line
from .settings import build_bundle

logger = logging.getLogger("magicwand.sidecar")

CMD_TABLE_COLUMNS = [
    "idx",
    "name",
    "command",
    "lineno",
    "start_ts",
    "end_ts",
    "duration_s",
    "exit_code",
    "peak_rss_mb",
    "mean_cpu_pct",
    "peak_cpu_pct",
    "n_samples",
]


# ---------- command canonicalization ----------

_SHELL_META = set("|;&><`$()")


def canonicalize_cmd(cmd: str) -> str:
    """Return a short label for a bash command (used as the categorical
    "cmd" axis on dashboard charts and the "name" column in the summary
    table)."""
    cmd = cmd.strip()
    if not cmd:
        return "unknown"
    if any(c in cmd for c in _SHELL_META):
        return cmd if len(cmd) <= 80 else cmd[:77] + "..."
    parts = cmd.split()
    head = parts[0]
    for p in parts[1:]:
        if not p.startswith("-"):
            return f"{head} {p}"
    return head


# ---------- per-cmd sampling ----------


@dataclass
class Sample:
    ts: float
    rss_bytes: int
    cpu_pct: float


@dataclass
class Cmd:
    """One bash command from the user's command block — start ts, line
    number, canonical name, and the resource samples taken while it ran."""
    idx: int
    name: str
    command: str
    lineno: int
    start_ts: float
    end_ts: Optional[float] = None
    exit_code: Optional[int] = None
    samples: List[Sample] = field(default_factory=list)


class ProcessTreeSampler:
    """Polls the task PID + descendants every `interval` seconds.

    A separate thread, started on init, stopped on finalize. Independent of
    wandb-core's own sampling — this one is for per-cmd aggregation, not
    dashboard charts. wandb-core handles those.
    """

    def __init__(self, task_pid: int, interval: float):
        self.task_pid = task_pid
        self.interval = interval
        self._buffer: Deque[Sample] = deque(maxlen=100_000)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        try:
            self._root = psutil.Process(task_pid)
        except psutil.NoSuchProcess:
            self._root = None

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run, name="mw-sampler", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.interval * 2 + 1)

    def _run(self) -> None:
        # Prime cpu_percent on each PID we see so subsequent calls report a delta.
        primed: Dict[int, psutil.Process] = {}
        if self._root is not None:
            primed[self._root.pid] = self._root
            self._root.cpu_percent(None)
        while not self._stop.wait(self.interval):
            sample = self._take_sample(primed)
            if sample is not None:
                with self._lock:
                    self._buffer.append(sample)

    def _take_sample(self, primed: Dict[int, psutil.Process]) -> Optional[Sample]:
        if self._root is None:
            return None
        if not self._root.is_running():
            return None
        try:
            children = [self._root] + self._root.children(recursive=True)
        except psutil.NoSuchProcess:
            return None
        rss_total = 0
        cpu_total = 0.0
        for proc in children:
            if proc.pid not in primed:
                primed[proc.pid] = proc
                try:
                    proc.cpu_percent(None)
                except psutil.Error:
                    continue
            try:
                cpu_total += proc.cpu_percent(None)
                rss_total += proc.memory_info().rss
            except psutil.Error:
                continue
        return Sample(ts=time.time(), rss_bytes=rss_total, cpu_pct=cpu_total)

    def slice(self, start_ts: float, end_ts: float) -> List[Sample]:
        with self._lock:
            return [s for s in self._buffer if start_ts <= s.ts <= end_ts]


# ---------- cmd axis logger ----------


class CmdAxisLogger:
    """Logs the active bash command as a wandb metric on every sampling tick.

    Gives charts a categorical "which command was running" axis. Runs in
    its own thread, independent of the metrics sampler.
    """

    def __init__(self, run, interval: float, get_cmd):
        self._run = run
        self._interval = interval
        self._get_cmd = get_cmd
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run_loop, name="mw-cmd-axis", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self._interval * 2 + 1)

    def _run_loop(self) -> None:
        while not self._stop.wait(self._interval):
            cmd = self._get_cmd()
            if cmd is None:
                continue
            self._run.log(
                {
                    "cmd": cmd.name,
                    "cmd_idx": cmd.idx,
                    "cmd_lineno": cmd.lineno,
                }
            )


# ---------- main loop ----------


def _aggregate(cmd: Cmd) -> dict:
    if not cmd.samples:
        return {
            "peak_rss_mb": None,
            "mean_cpu_pct": None,
            "peak_cpu_pct": None,
            "n_samples": 0,
        }
    rss = [s.rss_bytes for s in cmd.samples]
    cpu = [s.cpu_pct for s in cmd.samples]
    return {
        "peak_rss_mb": max(rss) / (1024 * 1024),
        "mean_cpu_pct": sum(cpu) / len(cpu),
        "peak_cpu_pct": max(cpu),
        "n_samples": len(cmd.samples),
    }


def _open_fifo(path: str):
    """Open the FIFO O_RDWR so reads never see EOF if a writer closes."""
    fd = os.open(path, os.O_RDWR)
    return os.fdopen(fd, "r", buffering=1)


def _required_env(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        raise RuntimeError(f"sidecar missing required env var {name}")
    return val


def _strict() -> bool:
    return os.environ.get("MAGICWAND_STRICT", "") == "1"


def _publish_status(status: str) -> None:
    """Write the run status (URL, OFFLINE, or FAIL: ...) to the file the
    bash snippet polls. Best-effort — never raises."""
    path = os.environ.get("MAGICWAND_RUN_URL_FILE")
    if not path:
        return
    try:
        with open(path, "w") as f:
            f.write(status + "\n")
    except OSError:
        pass


def _forward_task_log_to_wandb(run_dir: str) -> None:
    """Read bash's stdio from MAGICWAND_TASK_LOG_FIFO and append each line
    directly to wandb's `<run.dir>/output.log` — that's the file the run's
    Logs tab is sourced from. Writing it ourselves sidesteps wandb's stdio
    capture (which wraps sys.stdout, but only sees writes from the
    sidecar's own Python — bash's output never touches our stdout)."""
    path = os.environ.get("MAGICWAND_TASK_LOG_FIFO")
    if not path:
        return
    out_log = os.path.join(run_dir, "output.log")
    try:
        with open(path, "r", buffering=1) as src, open(out_log, "a", buffering=1) as dst:
            for line in src:
                dst.write(line)
                dst.flush()
    except FileNotFoundError:
        return
    except Exception:
        logger.exception("task-log forwarder failed")


def main() -> int:
    # Log to stderr so the user's task log sees everything. We deliberately
    # don't capture into a hidden file — wandb's "View run at" line, our
    # own startup/info messages, and any tracebacks all belong in the
    # user's stream. The MAGICWAND_ERROR_LOG env var is still defined for
    # backwards compat but no longer written to.
    logging.basicConfig(
        level=logging.INFO,
        format="magicwand: %(message)s",
    )
    logger.info("sidecar starting (pid=%d)", os.getpid())
    try:
        return _run()
    except Exception as e:
        _publish_status(f"FAIL: {type(e).__name__}: {e}")
        traceback.print_exc(file=sys.stderr)
        logger.exception("sidecar failed")
        if _strict():
            raise
        return 0


def _run() -> int:
    fifo_path = _required_env("MAGICWAND_FIFO")
    task_pid = int(_required_env("MAGICWAND_TASK_PID"))
    sampling_interval = float(os.environ.get("MAGICWAND_SAMPLING_INTERVAL", "2.0"))
    # MAGICWAND_MODE was decided at init time (online if WANDB_API_KEY is
    # set, offline otherwise). Anonymous mode was removed when W&B
    # deprecated the anonymous account-creation endpoint.
    mode = os.environ.get("MAGICWAND_MODE", "offline")
    parser_names = [
        n.strip()
        for n in os.environ.get("MAGICWAND_PARSERS", "").split(",")
        if n.strip()
    ]

    ctx = detect()
    bundle = build_bundle(
        ctx,
        task_pid=task_pid,
        mode=mode,
        sampling_interval=sampling_interval,
    )
    run = wandb.init(settings=bundle.settings, **bundle.init_kwargs)

    # Bash tees its stdio into MAGICWAND_TASK_LOG_FIFO. A daemon thread
    # reads the FIFO and appends each line to wandb's output.log so the
    # run's Logs tab shows the user's bash output.
    threading.Thread(
        target=_forward_task_log_to_wandb,
        args=(run.dir,),
        name="mw-tasklog-forwarder",
        daemon=True,
    ).start()

    # Publish a status marker so the EXIT trap can tell whether the sidecar
    # ever got past wandb.init. wandb itself prints the user-facing
    # "View run at <url>" line on stderr, so this file is a diagnostic
    # signal only — not the channel the user sees.
    if mode == "offline":
        _publish_status("OFFLINE")
        logger.info("wandb run started in offline mode")
    else:
        url = getattr(run, "url", None)
        if url:
            _publish_status(url)
            logger.info("wandb run live at %s", url)
        else:
            _publish_status("(W&B run started; no URL available)")

    # Drop unknown parser names with a warning — never fail the run for it.
    valid_parsers = []
    for n in parser_names:
        if n in known_parsers():
            valid_parsers.append(n)
        else:
            logger.warning("unknown parser %r; available: %s", n, known_parsers())

    sampler = ProcessTreeSampler(task_pid, sampling_interval)
    sampler.start()

    current: Optional[Cmd] = None
    cmd_lock = threading.Lock()

    def get_current_cmd() -> Optional[Cmd]:
        with cmd_lock:
            return current

    axis = CmdAxisLogger(run, sampling_interval, get_current_cmd)
    axis.start()

    table = wandb.Table(columns=CMD_TABLE_COLUMNS)
    final_exit = 0
    finalized = False

    try:
        with _open_fifo(fifo_path) as fifo:
            while not finalized:
                line = fifo.readline()
                if not line:
                    continue
                line = line.strip()
                if not line:
                    continue
                try:
                    evt = json.loads(line)
                except json.JSONDecodeError:
                    logger.warning("malformed FIFO line: %r", line)
                    continue
                etype = evt.get("event")
                if etype == "cmd_start":
                    with cmd_lock:
                        current = Cmd(
                            idx=evt.get("cmd_idx", 0),
                            name=canonicalize_cmd(evt.get("command", "")),
                            command=evt.get("command", ""),
                            lineno=evt.get("lineno", 0),
                            start_ts=float(evt.get("ts", time.time())),
                        )
                elif etype == "cmd_end":
                    end_ts = float(evt.get("ts", time.time()))
                    exit_code = int(evt.get("exit_code", 0))
                    with cmd_lock:
                        if current is None:
                            continue
                        current.end_ts = end_ts
                        current.exit_code = exit_code
                        current.samples = sampler.slice(current.start_ts, end_ts)
                        agg = _aggregate(current)
                        table.add_data(
                            current.idx,
                            current.name,
                            current.command,
                            current.lineno,
                            current.start_ts,
                            current.end_ts,
                            current.end_ts - current.start_ts,
                            current.exit_code,
                            agg["peak_rss_mb"],
                            agg["mean_cpu_pct"],
                            agg["peak_cpu_pct"],
                            agg["n_samples"],
                        )
                        current = None
                elif etype == "err":
                    with cmd_lock:
                        if current is not None:
                            current.exit_code = int(evt.get("exit_code", 1))
                elif etype == "log":
                    payload = evt.get("data") or {}
                    if isinstance(payload, dict) and payload:
                        with cmd_lock:
                            cmd_name = current.name if current else None
                        run.log({**payload, "cmd": cmd_name})
                elif etype == "finalize":
                    final_exit = int(evt.get("exit_code", 0))
                    finalized = True
                else:
                    logger.warning("unknown event %r", etype)

                # stdio events feed the parsers. The producer side (tee'ing
                # tool stdio into the FIFO) is documented as a future hook;
                # for now this loop is here so adding a producer is one
                # small change away.
                if etype in ("stdout", "stderr") and valid_parsers:
                    payload = evt.get("line", "")
                    with cmd_lock:
                        cmd_name = current.name if current else None
                    for n in valid_parsers:
                        for record in parse_line(payload, n):
                            run.log({**record, "cmd": cmd_name})
    finally:
        axis.stop()
        sampler.stop()
        run.log({"commands_summary": table})
        run.finish(exit_code=final_exit)
        # Pidfile removal is the "I'm fully done" signal that finalize and
        # the EXIT trap watch for. `kill -0` can't be used because in
        # container PID 1 is bash, which doesn't reap zombies — the sidecar
        # PID stays valid for kill(0) long after run.finish() returns.
        pidfile = os.environ.get("MAGICWAND_PIDFILE")
        if pidfile:
            try:
                os.unlink(pidfile)
            except OSError:
                pass

    return 0


if __name__ == "__main__":
    sys.exit(main())
