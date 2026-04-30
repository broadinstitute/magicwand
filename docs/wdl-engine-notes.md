# WDL engine notes

Cromwell is the target. magicwand auto-detects the workflow / task /
shard / attempt grouping on both backends without configuration:

- **Local backend** — parsed from the cwd
  (`/cromwell-executions/<workflow>/<uuid>/call-<task>/[shard-N/][attempt-M/]execution`).
- **PAPIv2 / Terra** — cwd is `/mnt/disks/cromwell_root` and exposes
  no metadata, so magicwand reads
  `/mnt/disks/cromwell_root/gcs_delocalization.sh` and parses the same
  workflow / uuid / task / shard / attempt out of the `gs://...` upload
  paths Cromwell wrote there. Plus the workspace bucket and
  submission id, stored on the run as
  `config.workspace_bucket` / `config.submission_id`.

If you need to override either path (custom backend, ad-hoc shell),
set `WDL_WORKFLOW_ID` / `WDL_TASK_NAME` in the task env — env vars
win over the auto-detection.

## W&B mode

Set `WANDB_API_KEY` (workflow input or backend secret-injection) for a
live online run. Without a key, magicwand runs W&B offline; sync after
with `wandb sync` from a machine that has a key.

## miniwdl

Used here only as a local test harness; the
`<root>/<YYYYMMDD_HHMMSS>_<workflow>/call-<task>/work/` cwd is also
auto-detected if you want to validate a WDL change before pushing to
Cromwell.
