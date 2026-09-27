#!/usr/bin/env bash
# Example Slurm job: convert one .h5ad to .duckdb on a compute node with the
# anthive-ingest Apptainer image, then push the result to the S3 catalog.
# Copy, adjust the #SBATCH lines and paths for your cluster. See doc/HPC.md.
#
#   sbatch docker/slurm-ingest.example.sh /data/project/h5adstore/Study2026/cells.h5ad
#
#SBATCH --job-name=anthive-convert
#SBATCH --cpus-per-task=8
#SBATCH --mem=64G
#SBATCH --time=04:00:00
#SBATCH --output=anthive-convert-%j.log

set -euo pipefail

H5AD="$(readlink -f "${1:?usage: sbatch $0 /path/to/file.h5ad}")"
SIF="${ANTHIVE_SIF:-$HOME/containers/anthive-ingest.sif}"
STORE_ROOT="$(dirname "$(dirname "$H5AD")")"      # the h5adstore the file lives in

# Node-local scratch. Most clusters set TMPDIR per job; fall back to /tmp.
export TMPDIR="${TMPDIR:-/tmp}"

# DuckDB must stay inside the job's memory allocation, or the scheduler
# kills the job instead of DuckDB spilling to disk. Leave ~15% headroom for
# the Python side (anndata holds the matrix in memory while converting).
MEM_MB="${SLURM_MEM_PER_NODE:-65536}"

# A minimal anthive.yaml for the job: limits follow the allocation. Written to
# scratch; the image itself is read-only.
CFG="$TMPDIR/anthive-job-${SLURM_JOB_ID:-local}.yaml"
cat > "$CFG" <<EOF
duckdb_memory_limit: "$(( MEM_MB * 85 / 100 ))MB"
duckdb_threads: ${SLURM_CPUS_PER_TASK:-4}
convert_staging_dir: "$TMPDIR"
duckdb_temp_directory: "$TMPDIR/anthive-spill"
tmp_dir: "$TMPDIR/anthive-tmp"
coordinate: never
EOF

# --cleanenv keeps the login environment (conda, modules, PYTHONPATH) out;
# --env passes only what the tools need. Bind the store and scratch.
apptainer exec --cleanenv \
    --env ANTHIVE_CONFIG="$CFG" --env TMPDIR="$TMPDIR" \
    --bind "$STORE_ROOT" --bind "$TMPDIR" \
    "$SIF" ant convert "$H5AD"

rm -f "$CFG"

# Publish with ant-sync from a node that has network access and an
# anthive.yaml holding the S3 credentials (never put keys in the image or in
# this script), e.g. on the login node:
#   apptainer exec --cleanenv --env ANTHIVE_CONFIG=$HOME/.config/anthive/s3.yaml \
#       --bind "$STORE_ROOT" "$SIF" ant sync --data-folder "$STORE_ROOT" push
# The serving site then pulls it (`ant sync pull`) and rescans.
