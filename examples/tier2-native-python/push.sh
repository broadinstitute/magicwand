#!/bin/bash

set -ex

OS=$(uname)
ARCH=$(uname -m)
PYSAM_VERSION="0.23.0"
WANDB_VERSION="0.21.4"
IMAGE_VERSION="0.1"

# Check if the OS is macOS or Linux
if [ "$OS" == "Darwin" ]; then
    docker_build="docker buildx"
elif [ "$OS" == "Linux" ] && [ "$ARCH" == "x86_64" ]; then
    docker_build="docker"
else
    echo "Unsupported os/arch (os=$OS, arch=$ARCH)"
    exit 1
fi

PUSH_TAG="us-docker.pkg.dev/broad-dsde-methods/magicwand/vcf-stats:$IMAGE_VERSION"
$docker_build build \
    --build-arg PYSAM_VERSION="$PYSAM_VERSION" \
    --build-arg WANDB_VERSION="$WANDB_VERSION" \
    -t "${PUSH_TAG}" \
    --platform linux/amd64 \
    --push \
    "$(pwd)"
