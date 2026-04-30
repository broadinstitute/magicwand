version 1.0

# Tier 0 demo: zero-effort instrumentation of a real bcftools VCF pipeline.
#
# Two added lines (`source` + `magicwand init`) give you a W&B dashboard
# with one row per bash command (stats, filter, counts) on a "Commands"
# summary table, plus system metrics sampled across the whole task.
# No API key required — runs are anonymous by default.
#
# Run with miniwdl:
#     miniwdl run examples/tier0-dropin.wdl vcf=path/to/your.vcf.gz

task vcf_quickstats {
    input {
        File vcf
        # AF threshold for the 'common' filter (illustrative; many VCFs lack
        # AF for some records — those get dropped under -e).
        Float min_af = 0.001
        # Optional W&B API key. Provide it (or set WANDB_API_KEY in the
        # backend env) to get a live online run with a clickable URL. If
        # absent, magicwand runs W&B in offline mode (local files only).
        String? wandb_api_key
    }

    command <<<
        set -euo pipefail

        echo "Installing bcftools..."
        apt-get update -qq >/dev/null 2>&1
        DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends \
            bcftools >/dev/null 2>&1

        # If a W&B API key was provided, expose it before magicwand init —
        # that flips the run from offline to online so wandb prints a live
        # "View run at <url>" line in this task log.
        ~{"export WANDB_API_KEY=" + wandb_api_key}

        # The magicwand one-liner — one line of WDL, instruments everything below.
        source <(curl -fsSL https://raw.githubusercontent.com/broadinstitute/magicwand/main/install.sh)
        magicwand init

        # 1. Summary statistics — SNP/MNP/indel breakdown, ts/tv, etc.
        #    Tee the output so the user sees it AND it's saved to a file.
        echo "Running bcftools stats..."
        bcftools stats ~{vcf} > stats.txt

        # 2. Filter: keep records with AF above threshold (or AF missing).
        #    Output is bgzipped binary, so straight redirect (no tee).
        echo "Running bcftools view (filter on AF)..."
        bcftools view -e "INFO/AF<~{min_af}" -O z -o filtered.vcf.gz ~{vcf}

        # 3. Counts for the workflow output. Each `bcftools view -H` is its
        #    own command so per-command resource attribution shows up in
        #    the W&B summary table. Tee the wc -l output so the count
        #    prints to the user's task log too.
        echo "Counting records..."
        bcftools view -H ~{vcf}          | wc -l | tee total_count.txt
        bcftools view -H filtered.vcf.gz | wc -l | tee filtered_count.txt
        echo "Done."
    >>>

    output {
        File filtered_vcf      = "filtered.vcf.gz"
        File stats             = "stats.txt"
        Int  total_records     = read_int("total_count.txt")
        Int  filtered_records  = read_int("filtered_count.txt")
    }

    runtime {
        docker: "python:3.12"
        cpu: 2
        memory: "4 GB"
        disks: "local-disk 20 SSD"
    }
}

workflow magicwand_tier0_demo {
    input {
        File vcf
        Float min_af = 0.001
        String? wandb_api_key
    }

    call vcf_quickstats {
        input:
            vcf            = vcf,
            min_af         = min_af,
            wandb_api_key  = wandb_api_key,
    }

    output {
        File stats             = vcf_quickstats.stats
        File filtered_vcf      = vcf_quickstats.filtered_vcf
        Int  total_records     = vcf_quickstats.total_records
        Int  filtered_records  = vcf_quickstats.filtered_records
    }
}
