"""Tests for magicwand.cli."""

from __future__ import annotations

import io
import os

import pytest

from magicwand import cli


def test_doctor_runs(capsys, clean_wdl_env):
    rc = cli.main(["doctor"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "magicwand" in out
    assert "python:" in out
    assert "detected engine" in out


def test_parsers_lists_builtins(capsys, clean_wdl_env):
    rc = cli.main(["parsers"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "bcftools" in out


def test_init_requires_task_pid(capsys, monkeypatch, clean_wdl_env):
    monkeypatch.delenv("MAGICWAND_TASK_PID", raising=False)
    rc = cli.main(["init"])
    assert rc == 1
    assert "MAGICWAND_TASK_PID not set" in capsys.readouterr().err


def test_init_emits_shell_snippet(capsys, monkeypatch, clean_wdl_env):
    monkeypatch.setenv("MAGICWAND_TASK_PID", "99999")
    rc = cli.main(["init", "--offline", "--sampling-interval=0.5"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "export MAGICWAND_FIFO=" in out
    assert "export MAGICWAND_TASK_PID=99999" in out
    assert "export MAGICWAND_MODE=offline" in out
    assert "export MAGICWAND_SAMPLING_INTERVAL=0.5" in out
    assert "mkfifo " in out
    assert "setsid" in out
    assert "source " in out
    assert "traps.sh" in out


def test_init_passes_parsers(capsys, monkeypatch, clean_wdl_env):
    monkeypatch.setenv("MAGICWAND_TASK_PID", "1234")
    rc = cli.main(["init", "--parsers", "bwa,samtools", "--parsers", "gatk"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "MAGICWAND_PARSERS='bwa,samtools,gatk'" in out or "MAGICWAND_PARSERS=bwa,samtools,gatk" in out


def test_finalize_quiet_when_no_fifo(monkeypatch, clean_wdl_env):
    monkeypatch.setenv("MAGICWAND_FIFO", "/tmp/does-not-exist-magicwand")
    rc = cli.main(["finalize", "0"])
    assert rc == 0


def test_log_requires_fifo(capsys, monkeypatch, clean_wdl_env):
    monkeypatch.delenv("MAGICWAND_FIFO", raising=False)
    rc = cli.main(["log", "x=1"])
    assert rc == 1
    assert "MAGICWAND_FIFO not set" in capsys.readouterr().err


def test_log_writes_event_to_fifo(tmp_path, monkeypatch, clean_wdl_env):
    fifo = tmp_path / "fifo"
    os.mkfifo(str(fifo))
    monkeypatch.setenv("MAGICWAND_FIFO", str(fifo))

    import json
    import threading

    captured: list[str] = []

    def reader():
        with open(fifo, "r") as f:
            captured.append(f.readline())

    t = threading.Thread(target=reader)
    t.start()
    rc = cli.main(["log", "records=15234", "tstv=2.10", "label=hello"])
    t.join(timeout=5)

    assert rc == 0
    assert captured, "no line read from FIFO"
    rec = json.loads(captured[0])
    assert rec["event"] == "log"
    assert rec["data"] == {"records": 15234, "tstv": 2.10, "label": "hello"}


def test_log_rejects_bad_kv(capsys, tmp_path, monkeypatch, clean_wdl_env):
    fifo = tmp_path / "fifo"
    os.mkfifo(str(fifo))
    monkeypatch.setenv("MAGICWAND_FIFO", str(fifo))
    rc = cli.main(["log", "no-equals-here"])
    assert rc == 1
    assert "expected key=value" in capsys.readouterr().err
