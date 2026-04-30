"""Tests for magicwand.sidecar pure-function helpers.

The full event loop is exercised in tests/integration/test_end_to_end.py
where we run a real bash + offline wandb.
"""

from __future__ import annotations

import pytest

from magicwand.sidecar import (
    Cmd,
    Sample,
    _aggregate,
    canonicalize_cmd,
)


@pytest.mark.parametrize(
    "cmd,expected",
    [
        ("bwa mem ref.fa reads.fq.gz", "bwa mem"),
        ("samtools sort -o out.bam", "samtools sort"),
        ("echo hi", "echo hi"),
        ("set -euo pipefail", "set pipefail"),  # `-euo` is a flag, pipefail is the first non-flag arg
        ("ls", "ls"),
        ("", "unknown"),
        ("a | b | c", "a | b | c"),  # pipeline preserved
        ("foo > /dev/null", "foo > /dev/null"),  # redirect preserved
    ],
)
def test_canonicalize_cmd(cmd, expected):
    assert canonicalize_cmd(cmd) == expected


def test_canonicalize_cmd_truncates_long_pipelines():
    cmd = "x | " + ("a" * 200)
    out = canonicalize_cmd(cmd)
    assert len(out) <= 80
    assert out.endswith("...")


def test_aggregate_empty():
    c = Cmd(idx=1, name="x", command="x", lineno=1, start_ts=0.0)
    assert _aggregate(c) == {
        "peak_rss_mb": None,
        "mean_cpu_pct": None,
        "peak_cpu_pct": None,
        "n_samples": 0,
    }


def test_aggregate_with_samples():
    c = Cmd(idx=1, name="x", command="x", lineno=1, start_ts=0.0)
    c.samples = [
        Sample(ts=0.5, rss_bytes=1_000_000, cpu_pct=10.0),
        Sample(ts=1.0, rss_bytes=2_097_152, cpu_pct=20.0),
        Sample(ts=1.5, rss_bytes=3_000_000, cpu_pct=30.0),
    ]
    agg = _aggregate(c)
    assert agg["n_samples"] == 3
    assert agg["peak_rss_mb"] == pytest.approx(3_000_000 / (1024 * 1024))
    assert agg["mean_cpu_pct"] == pytest.approx(20.0)
    assert agg["peak_cpu_pct"] == pytest.approx(30.0)
