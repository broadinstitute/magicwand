"""magicwand — drop-in observability shim for WDL tasks.

Public surface:
    magicwand.run(...)   — Tier 2 context manager wrapping wandb.init/finish
    magicwand.__version__
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Iterator, Optional

__version__ = "0.1.0"


@contextmanager
def run(
    *,
    project: Optional[str] = None,
    mode: Optional[str] = None,
    sampling_interval: float = 2.0,
    extra_config: Optional[dict] = None,
):
    """Tier 2 context manager. Same WDL context detection as Tier 0.

    Mode auto-detects the same way `magicwand init` does: explicit `mode=`
    wins, then `WANDB_MODE` env, then `online` if `WANDB_API_KEY` is set,
    else `offline`. (W&B deprecated anonymous account creation in Dec 2025,
    so there is no zero-config online path anymore.)

    Outside a WDL context, engine fields are None and the run name falls back
    to wandb's auto-generated default. `wandb.log()` calls inside the block
    work as normal.

    Example::

        import magicwand
        with magicwand.run(project="vcf-stats") as r:
            r.log({"records_per_sec": rate})
    """
    # Imported lazily so `import magicwand` doesn't pay for wandb setup unless
    # the user actually opens a run.
    import wandb

    from .context import detect
    from .settings import build_bundle

    if mode is None:
        env_mode = os.environ.get("WANDB_MODE")
        if env_mode:
            mode = env_mode
        elif os.environ.get("WANDB_API_KEY"):
            mode = "online"
        else:
            mode = "offline"

    ctx = detect()
    bundle = build_bundle(
        ctx,
        task_pid=os.getpid(),
        mode=mode,
        sampling_interval=sampling_interval,
        project=project,
        extra_config=extra_config,
    )
    r = wandb.init(settings=bundle.settings, **bundle.init_kwargs)
    try:
        yield r
    finally:
        r.finish()


__all__ = ["run", "__version__"]
