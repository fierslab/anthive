# Ingest on HPC

On a cluster you only need the command-line tools: convert `.h5ad` to
`.duckdb`, check, score, then push the result to the S3 catalog that the
serving sites pull from. No API, web UI or daemons run on compute nodes.

There are two ways to get the tools, and the first is simpler.

## Route A: a repo checkout and uv (no container)

`ant-convert` and the other Python ingest tools are self-contained uv
scripts: each one declares its dependencies in its header and uv builds a
cached environment on first use. uv is a single static binary that installs
into your home directory without root.

```sh
curl -LsSf https://astral.sh/uv/install.sh | sh          # once, on a login node
git clone git@github.com:badslab/anthive4.git ~/anthive4 && git -C ~/anthive4 checkout v2.37.1
cp ~/anthive4/docker/hpc-anthive.yaml ~/anthive4/anthive.yaml   # scratch-friendly defaults
~/anthive4/bin/ant convert /data/project/h5adstore/Study2026/cells.h5ad
```

Run each tool once on a login node first, so uv downloads its packages
there; compute nodes often have no internet access, and uv reuses the cache
(`~/.cache/uv`) afterwards.

Use this route when the Python tools are all you need. It is the easiest to
update (`git pull`) and adds no image to store or move.

## Route B: the Apptainer image

Use the image when you need the R/Seurat environment (converting `.rds`
objects to `.h5ad`), when compute nodes cannot reach the internet at all, or
when you want exactly the tool versions a serving site runs.

```sh
# build it from the published docker image (run in the anthive4 checkout;
# no root needed, ~1.3 GB result, a few minutes):
apptainer build --build-arg BOOTSTRAP=docker \
    --build-arg IMAGE=codeberg.org/mfiers/anthive-ingest:2.37.1 \
    anthive-ingest.sif docker/anthive-ingest.def
# once a .sif is published as an oras artifact this becomes:
#   apptainer pull anthive-ingest.sif oras://<registry>/anthive-ingest-sif:2.37.1

apptainer run  anthive-ingest.sif convert /data/project/h5adstore/Study2026/cells.h5ad
apptainer exec anthive-ingest.sif ant check /data/project/h5adstore/Study2026/cells.h5ad
apptainer exec anthive-ingest.sif rdata Rscript my_rds_to_h5ad.R
```

The build needs no root and no `--fakeroot` (the definition has no `%post`).
The image is read-only; its default config (`docker/hpc-anthive.yaml`, set
through `ANTHIVE_CONFIG`) sends every write to the job's `$TMPDIR`. Bind the
directories the job reads and writes with `--bind`.

To use your own settings, write a job config (start from
`docker/hpc-anthive.yaml`) and pass it with
`--env ANTHIVE_CONFIG=/path/job.yaml`. Values like `${TMPDIR:-/tmp}` stay
unexpanded in the file and are resolved when the tool runs, so they follow
each job's scratch directory, also under `--cleanenv`.

## Scratch and memory

Two settings matter on every cluster:

1. **Build on node-local disk.** DuckDB writes many small pages and relies on
   file locks. Parallel filesystems (GPFS, Lustre, BeeGFS) are slow at the
   first and unreliable at the second. With `convert_staging_dir` set (the
   HPC default is `$TMPDIR`), ant-convert builds the `.duckdb` on local
   scratch, verifies it, and only then moves it next to the h5ad; across
   filesystems that is a copy to a temporary name plus an atomic rename, so
   nobody ever reads a half-written file. Put DuckDB's spill directory
   (`duckdb_temp_directory`) on local scratch too.
   The layer parquet sidecars are still written next to the h5ad: the layer
   views record their absolute paths, so they have to be created in place.
   Those are large sequential writes, which parallel filesystems handle well.

2. **Cap DuckDB's memory at the job allocation.** DuckDB's default (80% of
   the node's RAM) ignores the scheduler's limit, so a big conversion gets
   the job killed instead of spilling to disk. Set `duckdb_memory_limit` to
   about 85% of the requested memory and `duckdb_threads` to the requested
   CPUs. The example job below derives both from the Slurm variables.

## An example Slurm job

`docker/slurm-ingest.example.sh` converts one file with the image:

```sh
sbatch docker/slurm-ingest.example.sh /data/project/h5adstore/Study2026/cells.h5ad
```

It writes a per-job config into `$TMPDIR` (memory limit from
`SLURM_MEM_PER_NODE`, threads from `SLURM_CPUS_PER_TASK`, staging and spill on
scratch, `coordinate: never`), runs `ant convert` with `--cleanenv`, and
removes the config afterwards. Adjust the `#SBATCH` lines and the image path
(`ANTHIVE_SIF`) for your site.

## Coordination with a server

Writers that touch a `.duckdb` a running ant-serve holds open first ask the
server to release it. On a compute node there is no server. The `coordinate`
setting controls this: `auto` (the default) talks to a server only if one
answers and otherwise carries on, `never` skips the handshake, and `always`
treats an unreachable server as an error. The HPC default config uses
`auto`; the example job uses `never`.

## Publishing the result

Push the new `.duckdb` to the S3 catalog with `ant sync push` from a node
with network access and an `anthive.yaml` that holds the S3 credentials.
Keep that file in your home directory (mode 0600) and pass it with
`--env ANTHIVE_CONFIG=...`; never put keys into the image or a job script.
Serving sites then run `ant sync pull` (or `ant-stack ingest -- ant sync pull`)
and rescan.
