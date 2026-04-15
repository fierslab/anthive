# Uploading Data to Anthive — Step-by-Step Guide

This guide explains how to upload `.h5ad` files to the shared Anthive S3 storage so
they become available to the team via the data portal.

---

## Overview

Files live in **group folders** on S3 (one folder per study or dataset collection,
e.g. `Simpson2025`).  The workflow is always:

```
1. Install the tool   (once)
2. Configure credentials  (once)
3. Create your group  (once per study)
4. Upload your files  (repeat as needed)
```

---

## Requirements

- **Python 3.10+** and **[uv](https://docs.astral.sh/uv/getting-started/installation/)**

  Install uv with:
  ```bash
  curl -LsSf https://astral.sh/uv/install.sh | sh
  ```
  That's it — `ant_s3` is a self-contained script that installs its own dependencies
  the first time it runs.

---

## Step 1 — Get the script

Download `ant_s3` to somewhere on your `PATH` (e.g. `~/bin/`):

```bash
mkdir -p ~/bin
curl -o ~/bin/ant_s3 https://raw.githubusercontent.com/YOUR_ORG/anthive4/main/bin/ant_s3
chmod +x ~/bin/ant_s3
```

> **Or**, if you have already cloned the anthive4 repository, the script is at
> `bin/ant_s3` inside the repo — you can run it directly from there or symlink it.

---

## Step 2 — Configure credentials

You only need to do this once.  Run the following four commands, replacing the
placeholders with the credentials you received from the data team:

```bash
ant_s3 set endpoint  fsn1.your-objectstorage.com
ant_s3 set region    eu-central
ant_s3 set bucket    anthive4a
ant_s3 set access-key  <your-access-key>
ant_s3 set secret-key  <your-secret-key>
```

Credentials are saved to `~/.anthive/credentials.yaml` on your machine — **never
commit this file to git**.

### Verify the connection

```bash
ant_s3 check
```

Expected output:
```
Checking S3 connection...
  Endpoint : fsn1.your-objectstorage.com
  Bucket   : anthive4a

✓ Connection successful — bucket is accessible.
```

If you see an error, double-check the credentials with the data team.

### Review your configuration at any time

```bash
ant_s3 show
```

This prints all settings with the secret key masked.

---

## Step 3 — Create a group

Every dataset belongs to a **group folder** named after the study (convention:
`AuthorYear`, e.g. `Simpson2025`).  Create yours before uploading:

```bash
ant_s3 group create Simpson2025
```

Expected output:
```
✓ Created group 'Simpson2025'  →  s3://anthive4a/Simpson2025/index.md
  Edit the index.md to add authors, experiment notes, etc.
```

This creates a small `index.md` file inside the group that describes the study.
You can download it, fill in the metadata (authors, year, experiment description),
and re-upload it if you like — but it is not required to proceed.

### Group naming rules

- Letters, digits and underscores (`_`) only — **no hyphens**
- Must start with a letter
- Must end with a **4-digit year**
- No spaces or slashes
- Good examples: `Simpson2025`, `Van_Der_Berg2024`, `MouseBrainAtlas2026`

### See what groups already exist

```bash
ant_s3 group list
```

```
Groups in s3://anthive4a  (prefix: /)

  AlbertiniZielonka2025                       4 file(s)      7.9 GB
  Blackburn2026                               6 file(s)    123.0 GB
  Simpson2025                                 6 file(s)     94.6 GB
  ...

  15 group(s) total
```

---

## Step 4 — Upload files

```bash
ant_s3 upload Simpson2025 /path/to/my_dataset.h5ad
```

You can upload **multiple files at once**:

```bash
ant_s3 upload Simpson2025 sample_A.h5ad sample_B.h5ad sample_C.h5ad
```

Or with a shell glob:

```bash
ant_s3 upload Simpson2025 /data/processed/*.h5ad
```

Upload progress is shown in real time:

```
[1/2] Uploading: sample_A.h5ad
  → s3://anthive4a/Simpson2025/sample_A.h5ad  (2.3 GB)
  [██████████████████████░░░░░░░░]  73.2%  1.7 GB / 2.3 GB  45.1 MB/s
  ✓ Done

[2/2] Uploading: sample_B.h5ad
  → s3://anthive4a/Simpson2025/sample_B.h5ad  (1.8 GB)
  ...
```

Large files are uploaded in parallel chunks automatically (multipart upload,
256 MB chunks) — no special flags needed.

---

## Checking what is in the bucket

### List all files

```bash
ant_s3 list
```

### List files in your group only

```bash
ant_s3 list 'Simpson2025/*'
```

### List only h5ad files across the whole bucket

```bash
ant_s3 list '*.h5ad'
```

---

## Downloading a file

```bash
ant_s3 download Simpson2025/sample_A.h5ad
```

Save to a specific location:

```bash
ant_s3 download Simpson2025/sample_A.h5ad /data/local/sample_A.h5ad
```

---

## Generating a shareable download link

Presigned URLs let anyone download a file for a limited time — no credentials needed
on their end.

```bash
ant_s3 open Simpson2025/sample_A.h5ad
```

```
Simpson2025/sample_A.h5ad
  https://fsn1.your-objectstorage.com/anthive4a/Simpson2025/sample_A.h5ad?X-Amz-...

```

The link is valid for **24 hours** by default.  For a longer window:

```bash
ant_s3 open Simpson2025/sample_A.h5ad --expiry 604800   # 7 days
```

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `ERROR: No S3 bucket configured` | Credentials not set | Run `ant_s3 set bucket anthive4a` |
| `✗ Connection failed  (HTTP 403)` | Wrong access or secret key | Re-run `ant_s3 set access-key` / `set secret-key` |
| `✗ Connection failed  (HTTP 404)` | Wrong bucket name | Re-run `ant_s3 set bucket` |
| `uv: command not found` | uv not installed | Install from https://docs.astral.sh/uv/ |
| Upload stops midway | Network interruption | Re-run the same command — S3 multipart will resume |
| `WARNING: Group 'X' has no index.md` | Group not created yet | Run `ant_s3 group create X` first |

---

## Quick reference

```bash
# Setup (once)
ant_s3 set endpoint  fsn1.your-objectstorage.com
ant_s3 set region    eu-central
ant_s3 set bucket    anthive4a
ant_s3 set access-key  <key>
ant_s3 set secret-key  <secret>
ant_s3 check

# Groups
ant_s3 group list
ant_s3 group create MyStudy2025

# Upload
ant_s3 upload MyStudy2025 file1.h5ad file2.h5ad

# Inspect
ant_s3 list
ant_s3 list 'MyStudy2025/*'
ant_s3 show

# Download / share
ant_s3 download MyStudy2025/file1.h5ad
ant_s3 open MyStudy2025/file1.h5ad
ant_s3 open MyStudy2025/file1.h5ad --expiry 604800
```

---

## Credentials reference

> Share this table with new users (fill in the actual keys privately).

| Setting | Value |
|---|---|
| Endpoint | `fsn1.your-objectstorage.com` |
| Region | `eu-central` |
| Bucket | `anthive4a` |
| Access Key | *(provided separately)* |
| Secret Key | *(provided separately)* |

Credentials are stored in `~/.anthive/credentials.yaml` on your local machine.
This file is never uploaded anywhere — keep it private.
