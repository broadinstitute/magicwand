version 1.0

# Tier 1 demo: same VCF pipeline as Tier 0, plus a few inline lines that pull
# tool-specific metrics out of `bcftools stats` and push them into the same
# W&B run via `magicwand log`.
#
# The shape is intentionally minimal:
#   1. `magicwand init`   — one line, instruments the whole task.
#   2. Run your tools.
#   3. `magicwand log k=v` — one line per metric you care about.
#
# No parser plugins, no extra inputs to wire through the WDL — just shell.
#
# Run with miniwdl:
#     miniwdl run examples/tier1-with-parsers.wdl vcf=path/to/your.vcf.gz

task vcf_quickstats_with_log {
    input {
        File vcf
        Float min_af = 0.001
        # Optional W&B API key for live online tracking; offline if absent.
        String? wandb_api_key
    }

    command <<<
        set -euo pipefail

        echo "Installing bcftools..."
        apt-get update -qq >/dev/null 2>&1
        DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends \
            bcftools >/dev/null 2>&1

        ~{"export WANDB_API_KEY=" + wandb_api_key}

        # The magicwand one-liner — instruments everything below.
        source <(curl -fsSL https://raw.githubusercontent.com/broadinstitute/magicwand/main/install.sh)
        magicwand init

        echo "Running bcftools stats..."
        bcftools stats ~{vcf} | tee stats.txt

        # Inline: pull a few specific fields out of bcftools stats output and
        # log them to the W&B run that magicwand init started. No parser
        # plugin, no Python — just awk + one CLI call.
        records=$(awk '/^SN.*number of records:/ {print $NF}' stats.txt)
        snps=$(awk    '/^SN.*number of SNPs:/    {print $NF}' stats.txt)
        indels=$(awk  '/^SN.*number of indels:/  {print $NF}' stats.txt)
        tstv=$(awk    '/^SN.*ts\/tv:/            {print $NF}' stats.txt)
        magicwand log \
            bcftools.records="$records" \
            bcftools.snps="$snps" \
            bcftools.indels="$indels" \
            bcftools.tstv="$tstv"

        echo "Running bcftools view (filter on AF)..."
        bcftools view -e "INFO/AF<~{min_af}" -O z -o filtered.vcf.gz ~{vcf}
        echo "Counting records..."
        bcftools view -H ~{vcf}          | wc -l | tee total_count.txt
        bcftools view -H filtered.vcf.gz | wc -l | tee filtered_count.txt
        echo "Done."
    >>>

    output {
        File stats             = "stats.txt"
        File filtered_vcf      = "filtered.vcf.gz"
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

workflow magicwand_tier1_demo {
    input {
        File vcf
        Float min_af = 0.001
        String? wandb_api_key
    }

    call vcf_quickstats_with_log {
        input:
            vcf           = vcf,
            min_af        = min_af,
            wandb_api_key = wandb_api_key,
    }

    output {
        File stats            = vcf_quickstats_with_log.stats
        Int  total_records    = vcf_quickstats_with_log.total_records
        Int  filtered_records = vcf_quickstats_with_log.filtered_records
    }
}
