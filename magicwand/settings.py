"""Build the wandb.Settings object plus init-time kwargs from a Context.

Everything here is built on documented or `x_`-prefixed wandb settings — no
private attribute access, no monkey-patching.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import List, Optional

import psutil  # noqa: F401  (imported for future disk-IO sampling integration)
import wandb

from . import __version__
from .context import Context


@dataclass
class WandbBundle:
    """Bundle of args to hand to wandb.init() — settings + the init kwargs."""

    settings: wandb.Settings
    init_kwargs: dict


def _autodetect_disk_paths(cwd: str) -> List[str]:
    """Mountpoint of cwd plus /tmp, deduped, real paths only."""
    paths: List[str] = []
    seen: set = set()

    def _add(p: str) -> None:
        real = os.path.realpath(p)
        if real not in seen and os.path.isdir(real):
            paths.append(real)
            seen.add(real)

    cwd_mount = cwd
    while cwd_mount and not os.path.ismount(cwd_mount):
        parent = os.path.dirname(cwd_mount)
        if parent == cwd_mount:
            break
        cwd_mount = parent
    _add(cwd_mount or cwd)
    _add("/tmp")
    return paths


def build_bundle(
    ctx: Context,
    task_pid: int,
    *,
    mode: str = "online",
    sampling_interval: float = 2.0,
    project: Optional[str] = None,
    extra_config: Optional[dict] = None,
) -> WandbBundle:
    """Construct the wandb.Settings + init kwargs used by sidecar and Tier 2.

    `mode` is one of 'online' | 'offline' | 'disabled'. Online requires
    WANDB_API_KEY; the sidecar selects mode from key presence at startup.
    Anonymous mode was removed because W&B deprecated the anonymous
    account-creation endpoint in Dec 2025.
    """
    config = {
        "engine": ctx.engine,
        "wdl_workflow_uuid": ctx.workflow_uuid,
        "wdl_workflow_name": ctx.workflow_name,
        "wdl_task_name": ctx.task_name,
        "wdl_shard": ctx.shard,
        "wdl_attempt": ctx.attempt,
        "wdl_script_sha256": ctx.wdl_script_sha256,
        "magicwand_version": __version__,
    }
    if extra_config:
        config.update(extra_config)

    # `program` is what wandb shows as "Command" in the run overview.
    # Without an override it would display "-m magicwand.sidecar", which is
    # implementation noise the WDL author doesn't care about. Use the
    # task name when we have one so the dashboard matches the WDL.
    program = ctx.task_name or "magicwand"

    settings = wandb.Settings(
        mode=mode,
        program=program,
        x_stats_pid=task_pid,
        x_stats_sampling_interval=sampling_interval,
        x_stats_disk_paths=_autodetect_disk_paths(ctx.cwd),
        x_stats_track_process_tree=True,
    )

    # Don't override `name`. wandb's auto-generated slugs ("lyric-snowflake-42")
    # are friendlier than anything we can compute from cwd; the hostname-only
    # name we used to set was useless. Group/job_type still let users find
    # related runs without setting an explicit name.
    init_kwargs = {
        "project": project or os.environ.get("WANDB_PROJECT") or "magicwand",
        "group": ctx.workflow_uuid,
        "job_type": ctx.task_name,
        "config": config,
    }
    return WandbBundle(settings=settings, init_kwargs=init_kwargs)
