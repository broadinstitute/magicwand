"""WDL execution context detection.

Recognises Cromwell and miniwdl directory layouts plus an env-var override.
The detected fields populate W&B's `group`, `job_type`, and `name`.
"""

from __future__ import annotations

import hashlib
import os
import re
import socket
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

CROMWELL_RE = re.compile(
    r"/cromwell-executions/(?P<wf>[^/]+)/(?P<wf_uuid>[0-9a-f-]{36})"
    r"/call-(?P<task>[^/]+)"
    r"(?:/shard-(?P<shard>\d+))?"
    r"(?:/attempt-(?P<attempt>\d+))?"
    r"/execution/?$"
)

# Cromwell PAPIv2 / Terra writes a `gcs_delocalization.sh` next to the task,
# whose `gs://...` upload paths encode the same workflow metadata that
# /cromwell-executions/... does on the local backend. Pattern:
#
#   gs://<bucket>/submissions/<submission_uuid>/<workflow>/<workflow_uuid>
#       /call-<task>/[shard-N/][attempt-M/]<filename>
#
# Or, if workspace lifecycle rules are enabled:
#
#   gs://<bucket>/submissions/intermediates/<submission_uuid>/<workflow>/<workflow_uuid>
#       /call-<task>/[shard-N/][attempt-M/]<filename>
GCS_DELOC_RE = re.compile(
    r"gs://(?P<bucket>[a-zA-Z0-9._\-]+)"
    r"/submissions"
    r"(?:/intermediates)?"
    r"/(?P<sub>[0-9a-f-]{36})"
    r"/(?P<wf>[^/\s\"']+)"
    r"/(?P<wf_uuid>[0-9a-f-]{36})"
    r"/call-(?P<task>[^/\s\"']+)"
    r"(?:/shard-(?P<shard>\d+))?"
    r"(?:/attempt-(?P<attempt>\d+))?"
    r"/[^/\s\"']+"
)

MINIWDL_RE = re.compile(
    r"(?P<root>.+?)/(?P<ts>\d{8}_\d{6})_(?P<wf>[^/]+)"
    r"/call-(?P<task>[^/]+)/work/?$"
)


@dataclass
class Context:
    engine: Optional[str]
    workflow_name: Optional[str]
    workflow_uuid: Optional[str]
    task_name: Optional[str]
    shard: Optional[int]
    attempt: Optional[int]
    cwd: str
    run_name: str
    wdl_script_sha256: Optional[str] = None
    extras: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


def _build_run_name(task: Optional[str], shard, attempt) -> str:
    if not task:
        return socket.gethostname() or "magicwand-run"
    parts = [task]
    if shard is not None:
        parts.append(f"shard{shard}")
    if attempt is not None and int(attempt) > 1:
        parts.append(f"attempt{attempt}")
    return "-".join(parts)


def _hash_wdl_script(cwd: Path) -> Optional[str]:
    """Cromwell writes the script next to the execution directory."""
    candidates = [
        cwd.parent / "script",
        cwd / "script",
    ]
    for p in candidates:
        if p.is_file():
            h = hashlib.sha256()
            h.update(p.read_bytes())
            return h.hexdigest()
    return None


def _read_delocalization_script(cwd: str) -> Optional[str]:
    """Return the contents of Cromwell's gcs_delocalization.sh if present.

    On the PAPIv2 / Terra backend Cromwell writes this next to the task's
    working directory; the file is mostly an array of `gs://...` upload
    targets for each declared output. We parse those URLs to recover the
    workflow metadata that the local-backend cwd regex doesn't see.
    """
    candidates = [
        Path("/mnt/disks/cromwell_root/gcs_delocalization.sh"),
        Path(cwd) / "gcs_delocalization.sh",
        Path(cwd).parent / "gcs_delocalization.sh",
    ]
    for p in candidates:
        try:
            if p.is_file():
                return p.read_text(errors="replace")
        except OSError:
            continue
    return None


def _detect_cromwell_gcs(cwd: str) -> Optional[Context]:
    """Cromwell PAPIv2/Terra: cwd is /mnt/disks/cromwell_root rather than
    /cromwell-executions/..., so the directory regex misses. Recover the
    grouping from the `gs://...` upload paths in `gcs_delocalization.sh`,
    which Cromwell writes alongside the task."""
    text = _read_delocalization_script(cwd)
    if not text:
        return None
    m = GCS_DELOC_RE.search(text)
    if not m:
        return None
    shard = int(m.group("shard")) if m.group("shard") else None
    attempt = int(m.group("attempt")) if m.group("attempt") else None
    return Context(
        engine="cromwell",
        workflow_name=m.group("wf"),
        workflow_uuid=m.group("wf_uuid"),
        task_name=m.group("task"),
        shard=shard,
        attempt=attempt,
        cwd=cwd,
        run_name=_build_run_name(m.group("task"), shard, attempt),
        extras={
            "submission_id": m.group("sub"),
            "workspace_bucket": m.group("bucket"),
            "backend": "papiv2",
        },
    )


def _detect_cromwell(cwd: str) -> Optional[Context]:
    m = CROMWELL_RE.search(cwd)
    if not m:
        return None
    shard = int(m.group("shard")) if m.group("shard") else None
    attempt = int(m.group("attempt")) if m.group("attempt") else None
    return Context(
        engine="cromwell",
        workflow_name=m.group("wf"),
        workflow_uuid=m.group("wf_uuid"),
        task_name=m.group("task"),
        shard=shard,
        attempt=attempt,
        cwd=cwd,
        run_name=_build_run_name(m.group("task"), shard, attempt),
        wdl_script_sha256=_hash_wdl_script(Path(cwd)),
    )


def _detect_miniwdl(cwd: str) -> Optional[Context]:
    m = MINIWDL_RE.search(cwd)
    if not m:
        return None
    return Context(
        engine="miniwdl",
        workflow_name=m.group("wf"),
        workflow_uuid=None,
        task_name=m.group("task"),
        shard=None,
        attempt=None,
        cwd=cwd,
        run_name=_build_run_name(m.group("task"), None, None),
        extras={"miniwdl_timestamp": m.group("ts")},
    )


def _detect_envvars(cwd: str) -> Optional[Context]:
    wf_id = os.environ.get("WDL_WORKFLOW_ID")
    task = os.environ.get("WDL_TASK_NAME")
    fqn = os.environ.get("WDL_CALL_FQN")
    if not (wf_id or task or fqn):
        return None
    return Context(
        engine="env",
        workflow_name=None,
        workflow_uuid=wf_id,
        task_name=task,
        shard=None,
        attempt=None,
        cwd=cwd,
        run_name=_build_run_name(task or fqn, None, None),
        extras={"call_fqn": fqn} if fqn else {},
    )


def _fallback(cwd: str) -> Context:
    return Context(
        engine=None,
        workflow_name=None,
        workflow_uuid=None,
        task_name=None,
        shard=None,
        attempt=None,
        cwd=cwd,
        run_name=socket.gethostname() or "magicwand-run",
    )


def detect(cwd: Optional[str] = None) -> Context:
    """Return the best-matching Context for `cwd` (default: os.getcwd()).

    Priority: env-vars > Cromwell PAPIv2/GCS delocalization >
    Cromwell local cwd regex > miniwdl cwd regex > fallback.
    """
    cwd = cwd or os.getcwd()
    for fn in (
        _detect_envvars,
        _detect_cromwell_gcs,
        _detect_cromwell,
        _detect_miniwdl,
    ):
        ctx = fn(cwd)
        if ctx is not None:
            return ctx
    return _fallback(cwd)
