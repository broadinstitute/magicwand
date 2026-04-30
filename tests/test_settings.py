"""Tests for magicwand.settings.build_bundle()."""

from __future__ import annotations

import pytest

from magicwand.context import Context
from magicwand.settings import build_bundle


def _ctx(**overrides) -> Context:
    base = dict(
        engine="cromwell",
        workflow_name="WF",
        workflow_uuid="abc-123",
        task_name="MyTask",
        shard=None,
        attempt=None,
        cwd="/tmp",
        run_name="MyTask",
    )
    base.update(overrides)
    return Context(**base)


def test_bundle_settings_carries_x_stats(clean_wdl_env):
    bundle = build_bundle(_ctx(), task_pid=12345)
    assert bundle.settings.x_stats_pid == 12345
    assert bundle.settings.x_stats_track_process_tree is True
    assert bundle.settings.x_stats_sampling_interval == 2.0
    assert bundle.settings.x_stats_disk_paths


def test_bundle_init_kwargs_have_group_and_job_type(clean_wdl_env):
    bundle = build_bundle(_ctx(workflow_uuid="wf-1", task_name="t"), task_pid=1)
    kw = bundle.init_kwargs
    assert kw["group"] == "wf-1"
    assert kw["job_type"] == "t"
    # `name` is intentionally omitted — wandb picks a friendly default.
    assert "name" not in kw
    assert kw["config"]["engine"] == "cromwell"
    assert kw["config"]["wdl_workflow_uuid"] == "wf-1"
    assert kw["config"]["magicwand_version"]


def test_bundle_program_is_task_name(clean_wdl_env):
    bundle = build_bundle(_ctx(task_name="vcf_quickstats"), task_pid=1)
    assert bundle.settings.program == "vcf_quickstats"


def test_bundle_offline_mode(clean_wdl_env):
    bundle = build_bundle(_ctx(), task_pid=1, mode="offline")
    assert bundle.settings.mode == "offline"


