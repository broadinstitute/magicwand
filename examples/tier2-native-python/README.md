# Tier 2 example — native Python with `wandb.init()`

When you own the Python process and the docker image, you don't need magicwand.
wandb's own `wandb.init()` already tracks the process tree, captures stdio into
the Logs tab, and shows the actual script you ran in the run's Command field.
magicwand is for the **bash-level** cases (Tier 0 / Tier 1) where you don't own
the process and want to instrument arbitrary shell commands without writing
Python.

This example is just `pip install pysam wandb numpy` and a Python
script that calls `wandb.init()`, iterates a VCF with pysam, and logs.
The docker image bakes those three packages in so the WDL command
block is one line.

## What `run_stats.py` does

A single pass over a VCF using `pysam`, mimicking what `bcftools
stats` reports — but streamed live into W&B as the iteration runs:

| Live (every `--log-every` records) | Final summary |
|---|---|
| `progress.records`, `progress.records_per_sec`, `progress.elapsed_s` | `summary.records`, `summary.snps`, `summary.mnps`, `summary.indels`, `summary.multiallelic`, `summary.pass`, `summary.filtered`, `summary.pass_rate`, `summary.ts`, `summary.tv`, `summary.ts_tv`, `summary.unique_chroms`, `summary.elapsed_s`, `summary.records_per_sec` |
| `running.snps`, `running.indels`, `running.ts_tv`, `running.pass_rate`, `running.multiallelic_rate` | Histograms: `distribution.af`, `distribution.qual`, `distribution.indel_length` |
| | Tables: `table.records_per_chrom`, `table.filters`, `table.per_sample` |

## The WDL

```wdl
task vcf_stats_pysam {
  input {
    File vcf
    ...
    String docker_image = "us-docker.pkg.dev/broad-dsde-methods/magicwand/vcf-stats:0.1"
  }

  command <<<
    set -e
    ~{"export WANDB_API_KEY=" + wandb_api_key}
    run_stats.py --vcf ~{vcf} --log-every ~{log_every} ~{"--region " + region}
  >>>

  runtime { docker: docker_image  cpu: "2"  memory: total_memory  disks: "..." }
}
```

That's it. Compare to Tier 0 (`examples/tier0-dropin.wdl`), which has
to apt-install bcftools, source `install.sh` from GitHub, and call
`magicwand init` from bash. Tier 2 trades that runtime bootstrap for
a one-time docker build.

## Build and push the image

```bash
cd examples/tier2-native-python
./push.sh
```

`push.sh` mirrors the be-s-t bcftools docker pattern: Darwin →
`docker buildx`, Linux/x86_64 → `docker`, builds for `linux/amd64`,
tags as `us-docker.pkg.dev/broad-dsde-methods/magicwand/vcf-stats:0.1`,
and pushes. **The `magicwand` GAR repo under
`us-docker.pkg.dev/broad-dsde-methods/` may need to be created
before the first push** — check with `gcloud artifacts repositories
list --project=broad-dsde-methods` and create with `gcloud artifacts
repositories create magicwand --repository-format=docker --location=us`.

## Run locally (no docker, no W&B account)

```bash
pip install pysam wandb numpy
WANDB_MODE=offline python run_stats.py --vcf path/to/your.vcf.gz --log-every 1000
```

## Run online with a key

```bash
export WANDB_API_KEY=...
python run_stats.py --vcf path/to/your.vcf.gz
```

## Run as a WDL task

```bash
miniwdl run tier2-pysam-stats.wdl vcf=path/to/your.vcf.gz
```

In Cromwell/Terra, supply `wandb_api_key` from a workspace secret to
get a live online run.
