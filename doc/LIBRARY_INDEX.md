# Literature index (`library_chunks.parquet`)

The literature search behind the MCP tools runs on one precomputed file,
`library_chunks.parquet`. It is built once, centrally, and shared with every
deployment. Sites never parse or embed PDFs themselves: they load the file
at startup, which takes a few seconds.

Code: `lib/library_index.py` (build and query side), `bin/meta/ant-lib-index`
(the build tool), tests in `tests/test_library_index.py`.

## Building it

```sh
# all papers in the h5adstore (<collection>/static/*.pdf), 16 parallel parsers
ant lib-index --store data/h5adstore --workers 16

# the library repo (Library/<citekey>.md + .pdf), once it exists
ant lib-index --library data/library

# any folder with PDFs, forced full rebuild with another model
ant lib-index --pdfs ~/papers --model BAAI/bge-small-en-v1.5 --full
```

The default output is `<library_index>/library_chunks.parquet` when the
config key `library_index` is set, otherwise `data/library_index/`.

Builds are incremental. Each PDF is identified by its sha256; unchanged PDFs
keep their chunks and vectors, changed or new PDFs are parsed and embedded,
and chunks of PDFs that disappeared are dropped. Changing the embedding
model or the chunker version forces a full rebuild, because vectors from two
models must never mix.

## What is in the file

One row per chunk:

| Column | Meaning |
| --- | --- |
| `chunk_id` | `<first 16 hex of source sha256>:<chunk number>` |
| `citekey` | library citekey when a sidecar matches the PDF (by sha256, else DOI); null until the library exists |
| `collection` | h5adstore collection the PDF came from (null for library-only papers) |
| `doi`, `title`, `year` | from the library sidecar, else from the collection `index.md` |
| `source_path`, `source_sha256` | the PDF, relative to the indexed root, and its checksum |
| `page_start`, `page_end` | 1-based pages the chunk spans |
| `section` | abstract, introduction, methods, results, discussion, figure_legend, references or other |
| `heading` | nearest subheading, kept as context |
| `text`, `n_tokens` | the chunk (about 300 to 500 tokens, 50 tokens overlap within a section) |
| `cited_dois` | DOIs found in reference-list chunks |
| `embedding` | fixed-size list of float32, L2-normalised |

The parquet key-value metadata `anthive_library_index` holds a JSON object
with `schema_version`, `chunker_version`, `embedding_model`,
`embedding_dim`, `embedding_normalised`, `query_prefix`, `document_prefix`,
`built_at`, `n_chunks` and `n_sources`.

Reference lists are stored (tagged `references`) but left out of search
unless asked for. Text pulled out of plots (axis ticks, p-value grids) is
detected and dropped. Figure legends are separate chunks, so a legend never
gets glued into running text.

## Querying it

```python
from library_index import LibraryIndex, Embedder
index = LibraryIndex("library_chunks.parquet", embedder=Embedder("BAAI/bge-small-en-v1.5"))
hits = index.search("how were microglia isolated from fresh tissue", k=10,
                    mode="hybrid", sections=["methods"], year_min=2020)
```

- `keyword`: BM25 over chunk text and heading (sparse matrix in scipy; no
  DuckDB extension download, which locked-down containers cannot do).
- `semantic`: cosine similarity against the stored vectors (numpy, brute
  force; milliseconds at this size, no vector database needed).
- `hybrid` (default): reciprocal rank fusion of both rankings.
- Filters: `citekeys`, `collections`, `sections`, `year_min`, `year_max`,
  `include_references`.

The query embedder must be the model recorded in the file. If it is not, or
no embedder is available, the index logs a warning, reports it in the
response (`warnings`) and answers with keyword search instead of returning
meaningless similarities.

## Parser choice (evaluated 2026-10-02)

Five real papers (Fancy2024, Lee2026, Mathys2023, Sierksma2026, Zhou2020;
33 to 63 pages each), parsed with both tools on the same machine:

| | pymupdf4llm | Docling 2 (layout model, no OCR) |
| --- | --- | --- |
| time per paper | 9 to 48 s | 26 to 149 s |
| reading order in two-column Nature layouts | correct | correct |
| section headings found | yes, plus bold run-in subheadings | yes, misses some run-in subheadings |
| running headers ("NATURE MEDICINE") | leak through, filtered by the chunker | partly removed, some still detected as headings |
| figure captions | recognised by pattern | labelled as captions (cleaner) |
| install size | 244 MB venv | 1.7 GB venv (CPU torch) plus layout model download |

Docling is not clearly better on these papers, and costs 2 to 5 times the
time and a torch install, so pymupdf4llm is the default. Docling stays
available (`--parser docling`) and is the route for scanned PDFs, because it
can run OCR. One current PDF needs that: Gabitto2024's file has no text
layer on its first 37 of 46 pages, so only its extended-data legends are
indexed (the build reports such files as "needs OCR").

## Model choice (evaluated 2026-10-02)

30 hand-written questions about the 56 current papers (paraphrased so they
do not repeat title words), each with the paper that answers it. Metrics at
paper level over the top results; the same 4,360 chunks for every model.

| Model (size, dim) | mode | recall@5 | MRR@10 | recall@1 |
| --- | --- | --- | --- | --- |
| any (BM25 only) | keyword | 0.90 | 0.83 | 0.73 |
| **BAAI/bge-small-en-v1.5** (65 MB quantised ONNX, 384) | semantic | 1.00 | 0.92 | 0.87 |
| | hybrid | 1.00 | 0.90 | 0.83 |
| snowflake-arctic-embed-s (130 MB, 384) | semantic | 1.00 | 0.92 | 0.87 |
| | hybrid | 0.97 | 0.92 | 0.87 |
| BAAI/bge-base-en-v1.5 (210 MB, 768) | semantic | 0.93 | 0.91 | 0.87 |
| | hybrid | 0.97 | 0.90 | 0.83 |
| PubMedBERT embeddings (436 MB ONNX, own export, 768) | semantic | 0.97 | 0.90 | 0.83 |
| | hybrid | 0.97 | 0.92 | 0.87 |

All four models are within one question of each other, which is noise on
30 questions; the biomedical model is no better here. bge-small is the
smallest and fastest, so it is the default. Semantic search beats plain
BM25 by about 10 points; hybrid is not better than semantic on this set,
but stays the default because it protects exact-term queries (gene names,
dataset ids, cohort names) that these questions barely test. Finding the
right section, not only the right paper, is clearly weaker (about 0.5 of
answers have a chunk from the expected section in the top 5); a larger
evaluation set should drive any further tuning. The questions and scripts
are kept with the build notes, not in the repo.

## Size and speed (current corpus)

| | |
| --- | --- |
| PDFs / chunks | 56 / 4,360 (methods 1,193, results 796, figure legends 634, references 546, other 544, discussion 322, abstract 236, introduction 89) |
| file size | 9.2 MB |
| full build | 12.7 min wall clock with 16 parallel parsers (2.7 GB peak RAM); an unchanged re-run takes 6 s |
| load at startup | 0.9 s (parquet plus BM25 matrix), 0.5 s for the query model |
| query latency | keyword 1 ms; semantic 13 ms median; hybrid 24 ms median (p95 30 ms) |

## Where the dependencies go

| Side | Needs | Image |
| --- | --- | --- |
| build (parse + embed) | pymupdf4llm (244 MB venv), fastembed + model; optionally docling (1.7 GB with CPU torch) | ingest image, or a workstation with uv |
| query (ant-serve) | fastembed with onnxruntime and tokenizers (+121 MB on top of numpy/scipy/pyarrow, which the app image already has) and the bge-small model files (65 MB) | app image |

fastembed downloads models into `/tmp/fastembed_cache` by default. In the
images, bake the model in at build time and point `FASTEMBED_CACHE_PATH` (or
the `cache_dir` argument) at that folder, so a site never downloads it at
startup and works offline.

## Sharing with deployments

The parquet is a plain file. It is published next to the library and synced
through S3 with `ant sync`, like the other fleet-wide summary files. A site
downloads it with the library and points the server at it (config key
`library_index`). Nothing is recomputed on the site.
