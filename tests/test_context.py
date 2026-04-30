"""Tests for magicwand.context.detect()."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from magicwand import context


CROMWELL_CWD = (
    "/cromwell-executions/MyWorkflow/"
    "12345678-1234-1234-1234-123456789012/call-AlignAndCall/execution"
)
CROMWELL_SHARDED_CWD = (
    "/cromwell-executions/MyWorkflow/"
    "12345678-1234-1234-1234-123456789012/call-AlignAndCall/shard-3/attempt-2/execution"
)
MINIWDL_CWD = "/data/runs/20260101_120000_MyWorkflow/call-AlignAndCall/work"


def test_detect_cromwell_simple(clean_wdl_env):
    ctx = context.detect(CROMWELL_CWD)
    assert ctx.engine == "cromwell"
    assert ctx.workflow_name == "MyWorkflow"
    assert ctx.workflow_uuid == "12345678-1234-1234-1234-123456789012"
    assert ctx.task_name == "AlignAndCall"
    assert ctx.shard is None
    assert ctx.attempt is None
    assert ctx.run_name == "AlignAndCall"


def test_detect_cromwell_sharded(clean_wdl_env):
    ctx = context.detect(CROMWELL_SHARDED_CWD)
    assert ctx.engine == "cromwell"
    assert ctx.shard == 3
    assert ctx.attempt == 2
    assert ctx.run_name == "AlignAndCall-shard3-attempt2"


def test_detect_miniwdl(clean_wdl_env):
    ctx = context.detect(MINIWDL_CWD)
    assert ctx.engine == "miniwdl"
    assert ctx.workflow_name == "MyWorkflow"
    assert ctx.workflow_uuid is None
    assert ctx.task_name == "AlignAndCall"
    assert ctx.extras["miniwdl_timestamp"] == "20260101_120000"


def test_detect_envvars_override_cwd(monkeypatch, clean_wdl_env):
    monkeypatch.setenv("WDL_WORKFLOW_ID", "abc-123")
    monkeypatch.setenv("WDL_TASK_NAME", "MyTask")
    ctx = context.detect(CROMWELL_CWD)
    assert ctx.engine == "env"
    assert ctx.workflow_uuid == "abc-123"
    assert ctx.task_name == "MyTask"


def test_detect_fallback(clean_wdl_env):
    ctx = context.detect("/some/random/path")
    assert ctx.engine is None
    assert ctx.task_name is None
    assert ctx.run_name  # hostname-ish, never empty


GCS_DELOC_SCRIPT = """\
source '/mnt/disks/cromwell_root/gcs_transfer.sh'
timestamped_message 'Delocalization script execution started...'
# fc-38264d1f-9676-44bd-80ea-2956d2acaa69
delocalize_7bc801bf08f25fd169b91cdae825df76=(
  "terra-1a086a2f"
  "3"
  "0"
  "file"
  "gs://fc-38264d1f-9676-44bd-80ea-2956d2acaa69/submissions/10a9c218-1d7b-446c-aa23-b74ebb299dd0/magicwand_tier0_demo/76cddf99-ff07-4d3c-87ad-1b278cbbb774/call-vcf_quickstats/stats.txt"
  "/mnt/disks/cromwell_root/stats.txt"
  "required"
  ""
)
delocalize "${delocalize_7bc801bf08f25fd169b91cdae825df76[@]}"
"""


def test_detect_cromwell_papiv2_via_delocalization(
    tmp_path: Path, monkeypatch, clean_wdl_env
):
    """Cromwell PAPIv2/Terra: cwd isn't /cromwell-executions/...; metadata
    is recovered from gcs_delocalization.sh's gs:// upload paths."""
    deloc = tmp_path / "gcs_delocalization.sh"
    deloc.write_text(GCS_DELOC_SCRIPT)
    ctx = context.detect(str(tmp_path))
    assert ctx.engine == "cromwell"
    assert ctx.workflow_name == "magicwand_tier0_demo"
    assert ctx.workflow_uuid == "76cddf99-ff07-4d3c-87ad-1b278cbbb774"
    assert ctx.task_name == "vcf_quickstats"
    assert ctx.shard is None
    assert ctx.attempt is None
    assert ctx.run_name == "vcf_quickstats"
    assert ctx.extras["submission_id"] == "10a9c218-1d7b-446c-aa23-b74ebb299dd0"
    assert ctx.extras["workspace_bucket"] == "fc-38264d1f-9676-44bd-80ea-2956d2acaa69"
    assert ctx.extras["backend"] == "papiv2"


def test_detect_cromwell_papiv2_sharded(tmp_path: Path, clean_wdl_env):
    sharded = GCS_DELOC_SCRIPT.replace(
        "/call-vcf_quickstats/stats.txt",
        "/call-vcf_quickstats/shard-7/attempt-2/stats.txt",
    )
    deloc = tmp_path / "gcs_delocalization.sh"
    deloc.write_text(sharded)
    ctx = context.detect(str(tmp_path))
    assert ctx.shard == 7
    assert ctx.attempt == 2
    assert ctx.run_name == "vcf_quickstats-shard7-attempt2"


def test_detect_wdl_script_hash(tmp_path: Path, clean_wdl_env):
    # Cromwell writes the script next to the execution dir.
    call_dir = tmp_path / "cromwell-executions" / "WF" / (
        "11111111-2222-3333-4444-555555555555"
    ) / "call-MyTask"
    exec_dir = call_dir / "execution"
    exec_dir.mkdir(parents=True)
    (call_dir / "script").write_text("#!/bin/bash\necho hi\n")
    ctx = context.detect(str(exec_dir))
    assert ctx.engine == "cromwell"
    assert ctx.wdl_script_sha256 is not None
    assert len(ctx.wdl_script_sha256) == 64  # sha256 hex
