version 1.0

# Tier 2 demo: native Python with magicwand pre-baked into the docker
# image. The command block is one line — no apt-get, no curl-pipe-bash
# install.sh, no `magicwand init`. The image already has python, pysam,
# and magicwand. `run_stats.py` opens a `magicwand.run()` context inside
# itself, so all the W&B plumbing (run init, URL printing, run.finish)
# happens in Python — bash just calls the script.
#
# This is the natural fit when you own the docker layer and want to
# stream rich domain metrics live alongside magicwand's per-process
# resource graphs.

task vcf_stats_pysam {
  input {
    File vcf
    String? region
    Int log_every = 10000
    String? wandb_api_key
    String total_memory = "4 GB"
    String docker_image = "us-docker.pkg.dev/broad-dsde-methods/magicwand/vcf-stats:0.1"
  }

  command <<<
    set -e
    ~{"export WANDB_API_KEY=" + wandb_api_key}
    run_stats.py \
        --vcf ~{vcf} \
        --log-every ~{log_every} \
        ~{"--region " + region}
  >>>

  runtime {
    memory: total_memory
    docker: docker_image
    cpu: "2"
    disks: "local-disk 20 SSD"
  }
}

workflow magicwand_tier2_demo {
  input {
    File vcf
    String? region
    Int log_every = 10000
    String? wandb_api_key
    String total_memory = "4 GB"
    String docker_image = "us-docker.pkg.dev/broad-dsde-methods/magicwand/vcf-stats:0.1"
  }

  call vcf_stats_pysam {
    input:
      vcf = vcf,
      region = region,
      log_every = log_every,
      wandb_api_key = wandb_api_key,
      total_memory = total_memory,
      docker_image = docker_image
  }
}
