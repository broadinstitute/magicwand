#!/usr/bin/env python3
"""Tier 2 demo: pysam-based VCF stats with rich live W&B metrics.

Mimics what `bcftools stats` does — record counts, ts/tv, AF
distribution, indel sizes, per-sample call rate / het / hom, FILTER
breakdown — but streams the metrics into the running W&B dashboard as
it iterates instead of dumping a text file at the end.

This Tier 2 example uses wandb directly. When you own the Python
process (and the docker image), there's no need for the magicwand
shim — wandb's own `wandb.init()` already gives you per-process
resource graphs, captured stdio in the Logs tab, and an honest
Command field. magicwand is for the *bash-level* cases (Tier 0 / 1)
where you don't own the process and want to instrument arbitrary
shell commands.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from collections import Counter
from typing import Optional

import pysam
import wandb


_PURINES = {"A", "G"}
_PYRIMIDINES = {"C", "T"}


def _resolve_wandb_api_key() -> None:
    """If WANDB_API_KEY is a gs:// URL, fetch the file (gcloud, then gsutil)
    and replace the env var with its contents. The task's service account
    auths the read, so the literal key never lands in workflow inputs.
    On failure: warn and fall back to offline mode."""
    key = os.environ.get("WANDB_API_KEY", "")
    if not key.startswith("gs://"):
        return
    url = key
    for cmd in (["gcloud", "storage", "cat", url], ["gsutil", "cat", url]):
        try:
            out = subprocess.check_output(
                cmd, text=True, stderr=subprocess.DEVNULL
            ).strip()
        except (subprocess.CalledProcessError, FileNotFoundError):
            continue
        if out:
            os.environ["WANDB_API_KEY"] = out
            print(f"magicwand: resolved WANDB_API_KEY from {url}", file=sys.stderr)
            return
    print(
        f"magicwand: could not read WANDB_API_KEY from {url}; running W&B offline",
        file=sys.stderr,
    )
    os.environ.pop("WANDB_API_KEY", None)
    os.environ["WANDB_MODE"] = "offline"


def is_transition(ref: str, alt: str) -> bool:
    return (ref in _PURINES and alt in _PURINES) or (
        ref in _PYRIMIDINES and alt in _PYRIMIDINES
    )


def variant_kind(ref: str, alt: str) -> str:
    """SNP / MNP / INDEL classification for one (ref, alt) pair."""
    if len(ref) == 1 and len(alt) == 1:
        return "snp"
    if len(ref) == len(alt):
        return "mnp"
    return "indel"


def _af_value(rec) -> Optional[float]:
    af = rec.info.get("AF")
    if af is None:
        return None
    if isinstance(af, tuple):
        af = af[0] if af else None
    if af is None:
        return None
    try:
        return float(af)
    except (TypeError, ValueError):
        return None


def main() -> None:
    parser = argparse.ArgumentParser(
        description="pysam VCF stats with live W&B metrics — Tier 2 magicwand demo."
    )
    parser.add_argument("--vcf", required=True, help="path to .vcf, .vcf.gz, or .bcf")
    parser.add_argument("--region", default=None,
                        help="restrict to a region (e.g. chr22 or 22:1-1000000)")
    parser.add_argument("--log-every", type=int, default=10000,
                        help="emit a running-totals log every N records")
    parser.add_argument("--project", default="magicwand-vcf-stats",
                        help="W&B project name")
    args = parser.parse_args()

    _resolve_wandb_api_key()

    r = wandb.init(
        project=args.project,
        config={
            "vcf": args.vcf,
            "region": args.region,
            "log_every": args.log_every,
        },
    )
    try:
        vf = pysam.VariantFile(args.vcf)
        iterator = vf.fetch(args.region) if args.region else vf

        samples = list(vf.header.samples)
        per_sample_called = {s: 0 for s in samples}
        per_sample_het = {s: 0 for s in samples}
        per_sample_hom_alt = {s: 0 for s in samples}

        n_records = 0
        n_snps = 0
        n_mnps = 0
        n_indels = 0
        n_multiallelic = 0
        n_pass = 0
        n_filtered = 0
        ts = 0
        tv = 0

        af_values: list[float] = []
        qual_values: list[float] = []
        indel_lengths: list[int] = []
        chrom_counter: Counter = Counter()
        filter_counter: Counter = Counter()

        t0 = time.perf_counter()

        for rec in iterator:
            n_records += 1
            chrom_counter[rec.chrom] += 1

            filters = list(rec.filter.keys()) if rec.filter else []
            if not filters or filters == ["PASS"]:
                n_pass += 1
            else:
                n_filtered += 1
            for f in filters:
                filter_counter[f] += 1

            if rec.qual is not None:
                qual_values.append(float(rec.qual))

            if rec.alts:
                if len(rec.alts) > 1:
                    n_multiallelic += 1
                # Classify by the first ALT (matches bcftools-stats convention).
                kind = variant_kind(rec.ref, rec.alts[0])
                if kind == "snp":
                    n_snps += 1
                    if is_transition(rec.ref, rec.alts[0]):
                        ts += 1
                    else:
                        tv += 1
                elif kind == "mnp":
                    n_mnps += 1
                else:
                    n_indels += 1
                    indel_lengths.append(len(rec.alts[0]) - len(rec.ref))

            af = _af_value(rec)
            if af is not None:
                af_values.append(af)

            for sample, sample_rec in rec.samples.items():
                gt = sample_rec.get("GT")
                if not gt or any(a is None for a in gt):
                    continue
                per_sample_called[sample] += 1
                if all(a == 0 for a in gt):
                    continue  # hom-ref — don't count as called-alt
                if len(set(gt)) == 1:
                    per_sample_hom_alt[sample] += 1
                else:
                    per_sample_het[sample] += 1

            if n_records % args.log_every == 0:
                elapsed = time.perf_counter() - t0
                r.log({
                    "progress.records": n_records,
                    "progress.records_per_sec": n_records / elapsed if elapsed > 0 else 0.0,
                    "progress.elapsed_s": elapsed,
                    "running.snps": n_snps,
                    "running.indels": n_indels,
                    "running.ts_tv": ts / tv if tv > 0 else 0.0,
                    "running.pass_rate": n_pass / n_records,
                    "running.multiallelic_rate": n_multiallelic / n_records,
                })

        elapsed = time.perf_counter() - t0
        summary = {
            "summary.records": n_records,
            "summary.snps": n_snps,
            "summary.mnps": n_mnps,
            "summary.indels": n_indels,
            "summary.multiallelic": n_multiallelic,
            "summary.pass": n_pass,
            "summary.filtered": n_filtered,
            "summary.pass_rate": n_pass / n_records if n_records else 0.0,
            "summary.ts": ts,
            "summary.tv": tv,
            "summary.ts_tv": ts / tv if tv > 0 else 0.0,
            "summary.unique_chroms": len(chrom_counter),
            "summary.elapsed_s": elapsed,
            "summary.records_per_sec": n_records / elapsed if elapsed > 0 else 0.0,
        }

        if af_values:
            summary["distribution.af"] = wandb.Histogram(af_values, num_bins=64)
        if qual_values:
            summary["distribution.qual"] = wandb.Histogram(qual_values, num_bins=64)
        if indel_lengths:
            summary["distribution.indel_length"] = wandb.Histogram(indel_lengths, num_bins=64)

        chrom_table = wandb.Table(columns=["chrom", "records"])
        for chrom, n in sorted(chrom_counter.items(), key=lambda x: -x[1]):
            chrom_table.add_data(chrom, n)
        summary["table.records_per_chrom"] = chrom_table

        if filter_counter:
            f_table = wandb.Table(columns=["filter", "count"])
            for f, n in sorted(filter_counter.items(), key=lambda x: -x[1]):
                f_table.add_data(f, n)
            summary["table.filters"] = f_table

        if samples:
            sample_table = wandb.Table(columns=["sample", "called", "het", "hom_alt"])
            for s in samples:
                sample_table.add_data(
                    s,
                    per_sample_called[s],
                    per_sample_het[s],
                    per_sample_hom_alt[s],
                )
            summary["table.per_sample"] = sample_table

        r.log(summary)
    finally:
        r.finish()


if __name__ == "__main__":
    main()
