# Failure modes

magicwand is honest about what it can and can't see. The DEBUG/ERR/EXIT trap
pattern gets us 90% of the way to per-command tracking; the remaining 10% is
documented here, not papered over.

| Scenario | Behavior | Mitigation |
|---|---|---|
| Sequential commands | Works perfectly. | — |
| Pipelines (`a \| b`) | One command, full pipeline string preserved. | Pipelines are a single `BASH_COMMAND` — this is a bash-level limit. |
| Subshells `(...)` with `functrace` | Tracked. | We set `set -o functrace` so DEBUG inherits. |
| Subshells without `functrace` | Lost. | If you `unset functrace`, you opt out. |
| `exec <binary>` | Trap dies; resource tracking continues via `x_stats_pid` (PID is reused). Per-command info ends. | Use `bash -c '...'` instead if you want command boundaries. |
| Background jobs (`&`) | Launch tracked, completion not. | Use WDL `scatter` for parallelism, not bash `&`. |
| `docker run` inside task | Command tracked, but the container's PIDs are children of `dockerd`, not the task. Resource tracking misses them. | If the inner tool exposes Prometheus, set `x_stats_open_metrics_endpoints`. Otherwise: known gap. |
| `set -e` interaction | ERR + EXIT both fire; `__MW_FINALIZED` guard dedupes. | Just works. |
| SIGKILL (job timeout, OOM) | Sidecar may not get to flush — partial run. | The W&B run still exists with whatever was logged before the kill. |
| Bash <5.0 | EPOCHREALTIME unavailable; traps refuse to install with a stderr message. | Upgrade bash, or set up a base image with bash >=5.0. |
| No `WANDB_API_KEY` | Auto-detects offline mode; the run is written to `./wandb/offline-run-…/`. | `wandb sync ./wandb/offline-run-*` from a machine that has a key. |
| `api.wandb.ai` unreachable when `WANDB_API_KEY` is set | `wandb.init()` raises; the traceback prints to the task log. | Whitelist `api.wandb.ai:443`, or rerun with `--offline`. |

## What magicwand never does

- **Modify the user's exit code.** ERR doesn't add `exit`; finalize forwards
  `$?` exactly.
- **Block the WDL task.** All FIFO writes are best-effort; the EXIT trap
  caps its sidecar wait at 30 s.
- **Touch wandb internals.** Only documented or `x_`-prefixed `wandb.Settings`
  fields are used.
- **Hide errors silently.** Errors at the soft-fail boundary are written to
  stderr and to `$MAGICWAND_ERROR_LOG`. The boundary catches; nothing
  internal does.
