# magicwand Tier 2 demo image: pysam VCF stats with live W&B metrics.
#
# python:3.12-slim is debian-bookworm with python + pip + ca-certificates
# already in place; pysam ships manylinux wheels with bundled htslib, so
# no apt step is needed. Single layer for the pip install keeps the image
# tight (~380 MB).
#
# Tier 2 uses wandb directly — no magicwand wrapper. When you own the
# Python process, wandb's own resource tracking + stdio capture do the
# right thing without any shim.

FROM python:3.12-slim

ARG PYSAM_VERSION=0.23.0
ARG WANDB_VERSION=0.21.4

ENV PYSAM_VERSION=$PYSAM_VERSION \
    WANDB_VERSION=$WANDB_VERSION \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

LABEL org.opencontainers.image.source="https://github.com/broadinstitute/magicwand" \
      org.opencontainers.image.description="magicwand Tier 2 demo: pysam VCF stats with live W&B metrics"

RUN pip install \
        "pysam==${PYSAM_VERSION}" \
        "wandb==${WANDB_VERSION}" \
        numpy

COPY run_stats.py /usr/local/bin/run_stats.py
RUN chmod +x /usr/local/bin/run_stats.py

WORKDIR /workdir
