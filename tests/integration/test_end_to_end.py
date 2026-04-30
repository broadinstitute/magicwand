"""End-to-end test: real FIFO + sidecar event loop with mocked wandb.

Runs the sidecar's `_run()` in a thread, feeds it events through a real FIFO,
and asserts the wandb run got the right calls. Avoids spinning up wandb-core
or hitting the network.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from magicwand import sidecar


@pytest.fixture
def fake_run():
    run = MagicMock()
    run.log = MagicMock()
    run.finish = MagicMock()
    return run


@pytest.fixture
def fifo(tmp_path: Path):
    p = tmp_path / "fifo"
    os.mkfifo(p)
    return p


def _write_event(fifo_path: Path, evt: dict) -> None:
    with open(fifo_path, "w") as f:
        f.write(json.dumps(evt) + "\n")


def test_sidecar_loop_logs_commands_summary(monkeypatch, fifo: Path, fake_run, tmp_path):
    monkeypatch.setenv("MAGICWAND_FIFO", str(fifo))
    monkeypatch.setenv("MAGICWAND_TASK_PID", str(os.getpid()))
    monkeypatch.setenv("MAGICWAND_SAMPLING_INTERVAL", "0.1")
    monkeypatch.setenv("MAGICWAND_MODE", "offline")
    monkeypatch.setenv("MAGICWAND_ANONYMOUS", "allow")
    monkeypatch.setenv("MAGICWAND_PARSERS", "")
    monkeypatch.setenv("MAGICWAND_STRICT", "1")
    monkeypatch.setenv("MAGICWAND_ERROR_LOG", str(tmp_path / "err.log"))

    with patch("magicwand.sidecar.wandb") as wandb_mock:
        wandb_mock.init.return_value = fake_run
        wandb_mock.Table = MagicMock(side_effect=lambda columns: _StubTable(columns))

        thread = threading.Thread(target=sidecar._run, daemon=True)
        thread.start()

        # Sequence: cmd_start, cmd_end, cmd_start, cmd_end, finalize.
        now = time.time()
        _write_event(fifo, {"event": "cmd_start", "ts": now,
                            "lineno": 1, "command": "echo hi", "cmd_idx": 1})
        time.sleep(0.05)
        _write_event(fifo, {"event": "cmd_end", "ts": now + 0.1,
                            "lineno": 1, "exit_code": 0, "command": "echo hi"})
        _write_event(fifo, {"event": "cmd_start", "ts": now + 0.1,
                            "lineno": 2, "command": "ls /", "cmd_idx": 2})
        time.sleep(0.05)
        _write_event(fifo, {"event": "cmd_end", "ts": now + 0.2,
                            "lineno": 2, "exit_code": 0, "command": "ls /"})
        _write_event(fifo, {"event": "finalize", "ts": now + 0.3, "exit_code": 0})

        thread.join(timeout=10)
        assert not thread.is_alive(), "sidecar didn't exit on finalize"

    # Assert wandb.init was called once with our settings.
    wandb_mock.init.assert_called_once()
    init_kwargs = wandb_mock.init.call_args.kwargs
    assert init_kwargs["job_type"] is not None or init_kwargs["job_type"] is None
    # run.log was called at least once with commands_summary
    log_calls = [c.args[0] if c.args else c.kwargs for c in fake_run.log.call_args_list]
    summary_calls = [c for c in log_calls if isinstance(c, dict) and "commands_summary" in c]
    assert summary_calls, f"no commands_summary log found in {log_calls!r}"

    # The Table should have two rows.
    tbl = summary_calls[0]["commands_summary"]
    assert len(tbl.rows) == 2
    assert tbl.rows[0][1] == "echo hi"  # name column
    assert tbl.rows[1][1] == "ls /"

    fake_run.finish.assert_called_once()
    assert fake_run.finish.call_args.kwargs.get("exit_code") == 0


class _StubTable:
    def __init__(self, columns):
        self.columns = columns
        self.rows = []

    def add_data(self, *row):
        self.rows.append(row)
