"""Precomputed literature index: parse, chunk, embed and search library PDFs.

Build side (central, run by ``bin/meta/ant-lib-index``): every PDF is parsed
into section-aware blocks, cut into ~300-500 token chunks with page numbers
and a section tag, embedded once, and written to one portable
``library_chunks.parquet``. Deployments only download that file.

Query side (ant-serve): ``LibraryIndex`` loads the parquet in seconds and
answers keyword (BM25), semantic (cosine) and hybrid (reciprocal rank
fusion) searches with filters. The query embedder must be the model recorded
in the parquet metadata; on a mismatch the index warns and answers with
keyword search only.

Heavy dependencies are split by side: parsing (``docling`` or ``pymupdf4llm``)
and embedding at build time; at query time only ``fastembed`` (ONNX, no torch)
for the query vector, plus numpy/scipy/pyarrow.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import logging
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np

log = logging.getLogger(__name__)

CHUNKER_VERSION = "1"
SCHEMA_VERSION = "1"
DEFAULT_MODEL = "BAAI/bge-small-en-v1.5"
SECTIONS = ("abstract", "introduction", "methods", "results", "discussion",
            "figure_legend", "references", "other")
# Sections kept out of semantic search by default (still in the parquet).
NON_SEARCHABLE_SECTIONS = ("references",)

# Per-model text prefixes (asymmetric retrieval models want them).
MODEL_PREFIXES: Dict[str, Tuple[str, str]] = {
    "BAAI/bge-small-en-v1.5": ("Represent this sentence for searching relevant passages: ", ""),
    "BAAI/bge-base-en-v1.5": ("Represent this sentence for searching relevant passages: ", ""),
    "snowflake/snowflake-arctic-embed-s": ("Represent this sentence for searching relevant passages: ", ""),
    "nomic-ai/nomic-embed-text-v1.5-Q": ("search_query: ", "search_document: "),
}

_DOI_RE = re.compile(r"\b(10\.\d{4,9}/[^\s\"<>\]\[)(,;]+)", re.I)
_WORD_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9\-α-ωΑ-Ω]*")


# ── section classification ───────────────────────────────────────────────────

_SECTION_PATTERNS: List[Tuple[str, re.Pattern]] = [
    ("abstract", re.compile(r"^(abstract|summary)\b", re.I)),
    ("introduction", re.compile(r"^(introduction|background|main)\b", re.I)),
    ("methods", re.compile(r"^(online\s+)?(methods?|materials?\s+and\s+methods|star\s*\+?\s*methods|"
                           r"experimental\s+(procedures|model|methods)|method\s+details|"
                           r"key\s+resources\s+table|quantification\s+and\s+statistical|"
                           r"data\s+(and\s+code\s+)?availability|code\s+availability|"
                           r"statistics(\s+and\s+reproducibility)?|reporting\s+summary)\b", re.I)),
    ("results", re.compile(r"^results?\b", re.I)),
    ("discussion", re.compile(r"^(discussion|conclusions?|limitations)\b", re.I)),
    ("references", re.compile(r"^(references?|bibliography|literature\s+cited|works\s+cited)\b", re.I)),
    ("other", re.compile(r"^(acknowledg|author\s+contributions?|competing\s+interests?|"
                         r"declaration\s+of\s+interests?|additional\s+information|funding|"
                         r"supplementary\s+information|online\s+content|ethics)", re.I)),
]
_LEGEND_RE = re.compile(r"^\W*(extended\s+data\s+)?(supplementary\s+)?(fig(ure)?s?\.?|table)\s*S?\d+", re.I)


def _clean_heading(text: str) -> str:
    text = re.sub(r"[*_#`<>]|</?(mark|u|b|i|sup|sub)>", " ", text)
    text = re.sub(r"^\s*\d+(\.\d+)*\s+", "", text)  # "3.1 Methods" / preprint line numbers
    return re.sub(r"\s+", " ", text).strip()


def classify_heading(text: str) -> Optional[str]:
    """Return the section a heading opens, or None for an ordinary subheading."""
    cleaned = _clean_heading(text)
    for section, pattern in _SECTION_PATTERNS:
        if pattern.match(cleaned):
            return section
    return None


def is_figure_legend(text: str) -> bool:
    return bool(_LEGEND_RE.match(_clean_heading(text)[:40]))


def extract_dois(text: str) -> List[str]:
    out: List[str] = []
    for match in _DOI_RE.finditer(text):
        doi = match.group(1).rstrip(".").lower()
        if doi not in out:
            out.append(doi)
    return out


def count_tokens(text: str) -> int:
    """Cheap tokenizer-free estimate (~1.3 wordpieces per word for papers)."""
    return int(math.ceil(len(_WORD_RE.findall(text)) * 1.3))


# ── parsing into blocks ──────────────────────────────────────────────────────

@dataclass
class Block:
    page: int
    kind: str          # heading | text | caption | table
    text: str


_NOISE_RE = re.compile(
    r"^(\W*\d{1,4}\W*|nature\s+\w+|cell\s+reports?|neuron|article|articles|"
    r"https?://\S+|www\.\S+|.*\|\s*vol\s+\d+.*|downloaded\s+from.*|.*all rights reserved.*)$", re.I)


def _is_noise(line: str) -> bool:
    plain = _clean_heading(line)
    return not plain or bool(_NOISE_RE.match(plain))


def parse_pdf_pymupdf(path: Path) -> List[Block]:
    """Blocks from pymupdf4llm page markdown (fast, no torch)."""
    import pymupdf4llm  # noqa: PLC0415
    pages = pymupdf4llm.to_markdown(str(path), page_chunks=True, show_progress=False)
    return blocks_from_page_markdown([(c["metadata"]["page_number"], c["text"]) for c in pages])


def blocks_from_page_markdown(pages: Iterable[Tuple[int, str]]) -> List[Block]:
    blocks: List[Block] = []
    for page_no, markdown in pages:
        for para in re.split(r"\n\s*\n", markdown):
            para = para.strip()
            if not para:
                continue
            first = para.splitlines()[0]
            if first.lstrip().startswith("#"):
                heading = _clean_heading(first)
                if heading and not _is_noise(first):
                    blocks.append(Block(page_no, "heading", heading))
                rest = "\n".join(para.splitlines()[1:]).strip()
                if not rest:
                    continue
                para = rest
            if para.startswith("|") and para.count("|") > 4:
                blocks.append(Block(page_no, "table", para))
                continue
            lines = [ln for ln in para.splitlines() if not _is_noise(ln)]
            text = re.sub(r"\s+", " ", " ".join(lines)).strip()
            text = re.sub(r"<br>|</?(mark|u|sup|sub)>", " ", text)
            text = re.sub(r"\*\*|__|(?<!\w)_(?=\w)|(?<=\w)_(?!\w)", "", text)
            if len(text) < 3:
                continue
            kind = "caption" if is_figure_legend(text) else "text"
            # A short bold line on its own is a heading the converter missed.
            if kind == "text" and len(text) < 90 and para.startswith("**") and para.rstrip().endswith("**"):
                kind = "heading"
            blocks.append(Block(page_no, kind, text))
    return blocks


def parse_pdf_docling(path: Path, converter: Any = None) -> List[Block]:
    """Blocks from Docling's layout model (headings, captions, tables, no
    running headers/footers). Needs torch; run at build time only."""
    from docling_core.types.doc import DocItemLabel  # noqa: PLC0415
    if converter is None:
        converter = make_docling_converter()
    doc = converter.convert(str(path)).document
    skip = {DocItemLabel.PAGE_HEADER, DocItemLabel.PAGE_FOOTER, DocItemLabel.PICTURE}
    blocks: List[Block] = []
    for item, _level in doc.iterate_items():
        label = getattr(item, "label", None)
        if label in skip:
            continue
        page = item.prov[0].page_no if getattr(item, "prov", None) else 0
        if label == DocItemLabel.TABLE:
            try:
                text = item.export_to_markdown(doc)
            except Exception:  # noqa: BLE001
                continue
            blocks.append(Block(page, "table", text))
            continue
        text = re.sub(r"\s+", " ", getattr(item, "text", "") or "").strip()
        if not text:
            continue
        if label in (DocItemLabel.SECTION_HEADER, DocItemLabel.TITLE):
            blocks.append(Block(page, "heading", text))
        elif label == DocItemLabel.CAPTION or is_figure_legend(text):
            blocks.append(Block(page, "caption", text))
        else:
            blocks.append(Block(page, "text", text))
    return blocks


def make_docling_converter(ocr: bool = False) -> Any:
    from docling.datamodel.base_models import InputFormat  # noqa: PLC0415
    from docling.datamodel.pipeline_options import PdfPipelineOptions  # noqa: PLC0415
    from docling.document_converter import DocumentConverter, PdfFormatOption  # noqa: PLC0415
    opts = PdfPipelineOptions()
    opts.do_ocr = ocr
    opts.do_table_structure = True
    return DocumentConverter(format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=opts)})


# ── chunking ─────────────────────────────────────────────────────────────────

@dataclass
class Chunk:
    page_start: int
    page_end: int
    section: str
    text: str
    n_tokens: int
    heading: str = ""
    dois: List[str] = field(default_factory=list)


def blocks_to_chunks(blocks: Sequence[Block], target_tokens: int = 400,
                     max_tokens: int = 500, overlap_tokens: int = 50) -> List[Chunk]:
    """Group blocks into section-tagged chunks.

    Text flows in reading order; a new top-level section heading closes the
    current chunk. Figure legends become their own chunks (tagged
    figure_legend) so they never get mixed into running text. Chunks carry
    the last ~overlap_tokens of the previous chunk of the same section.
    Before the first recognised heading, text counts as abstract on the
    first two pages and other after that.
    """
    chunks: List[Chunk] = []
    section = None
    heading = ""
    buf: List[Tuple[int, str]] = []

    def current_section(page: int) -> str:
        if section:
            return section
        return "abstract" if page <= 2 else "other"

    def flush(carry: bool = True) -> None:
        nonlocal buf
        if not buf:
            return
        text = " ".join(t for _, t in buf).strip()
        if text:
            sec = current_section(buf[0][0])
            chunks.append(Chunk(buf[0][0], buf[-1][0], sec, text, count_tokens(text), heading,
                                extract_dois(text) if sec == "references" else []))
        if carry and text and overlap_tokens > 0:
            words = text.split()
            keep = max(1, int(overlap_tokens / 1.3))
            buf = [(buf[-1][0], " ".join(words[-keep:]))] if len(words) > keep else []
        else:
            buf = []

    def buf_tokens() -> int:
        return count_tokens(" ".join(t for _, t in buf))

    for block in blocks:
        if block.kind == "heading":
            opened = classify_heading(block.text)
            if opened:
                flush(carry=False)
                section = opened
                heading = _clean_heading(block.text)
            else:
                # Subheading: keep it inline as context for the next text.
                if buf_tokens() >= target_tokens * 0.6:
                    flush()
                heading = _clean_heading(block.text)
                buf.append((block.page, heading + "."))
            continue
        if block.kind == "caption":
            text = block.text
            for piece in _split_long(text, max_tokens):
                chunks.append(Chunk(block.page, block.page, "figure_legend", piece, count_tokens(piece), heading))
            continue
        if section != "references" and is_low_content(block.text):
            continue
        for piece in _split_long(block.text, max_tokens):
            if buf and buf_tokens() + count_tokens(piece) > max_tokens:
                flush()
            buf.append((block.page, piece))
            if buf_tokens() >= target_tokens:
                flush()
    flush(carry=False)
    # Drop crumbs (lone overlap carries, stray page numbers) and plot debris.
    return [c for c in chunks
            if (c.n_tokens >= 12 or c.section == "figure_legend")
            and (c.section == "references" or not is_low_content(c.text))]


def _split_long(text: str, max_tokens: int) -> List[str]:
    if count_tokens(text) <= max_tokens:
        return [text]
    sentences = re.split(r"(?<=[.!?])\s+(?=[A-Z(])", text)
    pieces, cur = [], ""
    for sentence in sentences:
        candidate = (cur + " " + sentence).strip()
        if cur and count_tokens(candidate) > max_tokens:
            pieces.append(cur)
            cur = sentence
        else:
            cur = candidate
    if cur:
        pieces.append(cur)
    out: List[str] = []
    for piece in pieces:  # a single sentence longer than max_tokens: hard cut
        if count_tokens(piece) <= max_tokens:
            out.append(piece)
            continue
        cur_words: List[str] = []
        cur_tokens = 0
        for word in piece.split():
            word_tokens = count_tokens(word) or 1
            if cur_words and cur_tokens + word_tokens > max_tokens:
                out.append(" ".join(cur_words))
                cur_words, cur_tokens = [], 0
            cur_words.append(word)
            cur_tokens += word_tokens
        if cur_words:
            out.append(" ".join(cur_words))
    return out


def is_low_content(text: str) -> bool:
    """Text pulled out of plots (axis ticks, heatmap labels, p-value grids):
    few real words among many numbers/symbols. Kept out of the index."""
    tokens = text.split()
    if len(tokens) < 8:
        return False
    wordlike = sum(1 for t in tokens if re.fullmatch(r"[A-Za-z][a-z\-]{2,}[.,;:)]?", t))
    return wordlike / len(tokens) < 0.35


# ── embeddings ───────────────────────────────────────────────────────────────

class Embedder:
    """fastembed (ONNX) text embedder; no torch. Vectors are L2-normalised."""

    def __init__(self, model: str = DEFAULT_MODEL, cache_dir: Optional[str] = None, threads: Optional[int] = None):
        from fastembed import TextEmbedding  # noqa: PLC0415
        self.model = model
        self._impl = TextEmbedding(model_name=model, cache_dir=cache_dir, threads=threads)
        self.query_prefix, self.doc_prefix = MODEL_PREFIXES.get(model, ("", ""))
        probe = next(iter(self._impl.embed(["probe"])))
        self.dim = int(len(probe))

    def _embed(self, texts: Sequence[str], batch_size: int = 64) -> np.ndarray:
        vectors = np.array(list(self._impl.embed(list(texts), batch_size=batch_size)), dtype=np.float32)
        norms = np.linalg.norm(vectors, axis=1, keepdims=True)
        return vectors / np.where(norms == 0, 1, norms)

    def embed_documents(self, texts: Sequence[str]) -> np.ndarray:
        return self._embed([self.doc_prefix + t for t in texts])

    def embed_query(self, text: str) -> np.ndarray:
        return self._embed([self.query_prefix + text])[0]


# ── artifact: build + incremental update ─────────────────────────────────────

def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_front_matter(path: Path) -> Dict[str, Any]:
    import yaml  # noqa: PLC0415
    text = path.read_text(errors="replace")
    match = re.match(r"^---\n(.*?)\n---", text, re.S)
    if not match:
        return {}
    try:
        return yaml.safe_load(match.group(1)) or {}
    except Exception:  # noqa: BLE001
        return {}


def library_sidecar_map(library_dir: Optional[Path]) -> Dict[str, Dict[str, Any]]:
    """Map PDF sha256 and lower-cased DOI to sidecar metadata, for every
    library sidecar that names a PDF (``file:``) next to it."""
    out: Dict[str, Dict[str, Any]] = {}
    if not library_dir or not Path(library_dir).is_dir():
        return out
    for md in Path(library_dir).rglob("*.md"):
        meta = _read_front_matter(md)
        if not meta.get("citekey"):
            continue
        entry = {"citekey": str(meta["citekey"]), "doi": str(meta.get("doi") or "").lower() or None,
                 "title": meta.get("title"), "year": _year_of(meta)}
        pdf_name = meta.get("file")
        if pdf_name and (md.parent / str(pdf_name)).is_file():
            out["sha:" + sha256_file(md.parent / str(pdf_name))] = entry
        if entry["doi"]:
            out["doi:" + entry["doi"]] = entry
    return out


def _year_of(meta: Dict[str, Any]) -> Optional[int]:
    for key in ("year", "date"):
        value = str(meta.get(key) or "")
        match = re.match(r"(\d{4})", value)
        if match:
            return int(match.group(1))
    return None


def store_paper_metadata(pdf: Path, store_root: Optional[Path]) -> Dict[str, Any]:
    """Collection-level paper metadata for a PDF in ``<collection>/static/``
    (the h5adstore layout): doi, title, year from the collection index.md."""
    meta: Dict[str, Any] = {"collection": None, "doi": None, "title": None, "year": None}
    if store_root is None:
        return meta
    try:
        rel = pdf.resolve().relative_to(Path(store_root).resolve())
    except ValueError:
        return meta
    meta["collection"] = rel.parts[0]
    index = Path(store_root) / rel.parts[0] / "index.md"
    if index.is_file():
        front = _read_front_matter(index)
        meta["doi"] = str(front.get("doi") or "").lower() or None
        meta["title"] = front.get("paper_title")
        meta["year"] = _year_of(front)
    return meta


ParseFn = Callable[[Path], List[Block]]


def build_index(sources: Sequence[Path], out: Path, *, embedder: Embedder,
                parse: ParseFn, full: bool = False, store_root: Optional[Path] = None,
                library_dir: Optional[Path] = None, source_root: Any = None,
                workers: int = 1,
                progress: Callable[[str], None] = lambda msg: None) -> Dict[str, Any]:
    """Create or update ``out``. Only sources whose sha256 changed (or that
    are new) are parsed and embedded; chunks of vanished sources are dropped.
    A change of embedding model or chunker version forces a full rebuild.
    ``workers`` > 1 parses PDFs in a process pool (``parse`` must then be a
    picklable module-level function, e.g. ``parse_pdf_pymupdf``)."""
    import pyarrow as pa  # noqa: PLC0415
    import pyarrow.parquet as pq  # noqa: PLC0415

    out = Path(out)
    if source_root is None:
        roots: List[Path] = []
    elif isinstance(source_root, (list, tuple)):
        roots = [Path(r) for r in source_root]
    else:
        roots = [Path(source_root)]
    old_rows: Dict[str, List[Dict[str, Any]]] = {}
    if out.exists() and not full:
        meta = read_index_metadata(out)
        if (meta.get("embedding_model") == embedder.model
                and meta.get("chunker_version") == CHUNKER_VERSION
                and meta.get("schema_version") == SCHEMA_VERSION):
            for row in pq.read_table(out).to_pylist():
                old_rows.setdefault(row["source_sha256"], []).append(row)
        else:
            progress("model/chunker/schema changed: full rebuild")

    sidecars = library_sidecar_map(library_dir)
    rows: List[Dict[str, Any]] = []
    stats = {"sources": 0, "reused": 0, "parsed": 0, "failed": [], "needs_ocr": [], "chunks": 0}
    todo: List[Tuple[Path, str, str, Dict[str, Any]]] = []
    seen_sha: set = set()
    stats["duplicates"] = 0
    for pdf in sources:
        pdf = Path(pdf)
        sha = sha256_file(pdf)
        if sha in seen_sha:          # the same PDF reachable twice (store + library copy)
            stats["duplicates"] += 1
            continue
        seen_sha.add(sha)
        stats["sources"] += 1
        rel = _relative_to_any(pdf, roots)
        paper = store_paper_metadata(pdf, store_root)
        side = sidecars.get("sha:" + sha) or (sidecars.get("doi:" + paper["doi"]) if paper["doi"] else None) or {}
        file_meta = {
            "citekey": side.get("citekey"),
            "collection": paper["collection"],
            "doi": side.get("doi") or paper["doi"],
            "title": side.get("title") or paper["title"],
            "year": side.get("year") or paper["year"],
        }
        if sha in old_rows:
            for row in old_rows.pop(sha):
                row.update(file_meta, source_path=rel)
                rows.append(row)
            stats["reused"] += 1
            continue
        todo.append((pdf, sha, rel, file_meta))

    for pdf, sha, rel, file_meta, blocks, error in _parse_many(todo, parse, workers):
        if error is None:
            try:
                chunks = blocks_to_chunks(blocks)
            except Exception as exc:  # noqa: BLE001
                error = str(exc)
        if error is not None:
            stats["failed"].append(f"{rel}: {error}")
            progress(f"FAILED {rel}: {error}")
            continue
        coverage = text_layer_coverage(pdf)
        if coverage is not None and coverage < 0.5:
            stats["needs_ocr"].append(f"{rel}: text layer on {coverage:.0%} of pages")
            progress(f"WARNING {rel}: text layer on only {coverage:.0%} of pages, needs OCR")
        if not chunks:
            stats["failed"].append(f"{rel}: no text (scanned PDF? needs OCR)")
            continue
        vectors = embedder.embed_documents([c.text for c in chunks])
        for i, (chunk, vector) in enumerate(zip(chunks, vectors)):
            rows.append({
                "chunk_id": f"{sha[:16]}:{i:04d}",
                **file_meta,
                "source_path": rel,
                "source_sha256": sha,
                "page_start": chunk.page_start,
                "page_end": chunk.page_end,
                "section": chunk.section,
                "heading": chunk.heading,
                "text": chunk.text,
                "n_tokens": chunk.n_tokens,
                "cited_dois": chunk.dois,
                "embedding": vector.tolist(),
            })
        stats["parsed"] += 1
        progress(f"{rel}: {len(chunks)} chunks")
    stats["chunks"] = len(rows)
    write_index(rows, out, embedder)
    return stats


def text_layer_coverage(pdf: Path) -> Optional[float]:
    """Fraction of pages with an extractable text layer (None if pymupdf is
    unavailable). Image-only pages need OCR (``--parser docling`` with OCR)."""
    try:
        import pymupdf  # noqa: PLC0415
    except ImportError:
        return None
    try:
        with pymupdf.open(str(pdf)) as doc:
            if doc.page_count == 0:
                return 0.0
            with_text = sum(1 for page in doc if len(page.get_text().strip()) > 50)
            return with_text / doc.page_count
    except Exception:  # noqa: BLE001
        return None


def _relative_to_any(pdf: Path, roots: Sequence[Path]) -> str:
    for root in roots:
        try:
            return pdf.resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            continue
    return pdf.as_posix()


def _parse_one(parse: ParseFn, pdf: Path) -> Tuple[Optional[List[Block]], Optional[str]]:
    try:
        return parse(pdf), None
    except Exception as exc:  # noqa: BLE001
        return None, f"{type(exc).__name__}: {exc}"


def _parse_many(todo, parse: ParseFn, workers: int):
    """Yield (pdf, sha, rel, file_meta, blocks, error) in input order."""
    if workers <= 1 or len(todo) <= 1:
        for pdf, sha, rel, meta in todo:
            blocks, error = _parse_one(parse, pdf)
            yield pdf, sha, rel, meta, blocks, error
        return
    from concurrent.futures import ProcessPoolExecutor  # noqa: PLC0415
    import multiprocessing  # noqa: PLC0415
    ctx = multiprocessing.get_context("spawn")
    with ProcessPoolExecutor(max_workers=workers, mp_context=ctx) as pool:
        futures = [pool.submit(_parse_one, parse, pdf) for pdf, *_ in todo]
        for (pdf, sha, rel, meta), future in zip(todo, futures):
            blocks, error = future.result()
            yield pdf, sha, rel, meta, blocks, error


def index_schema(dim: int):
    import pyarrow as pa  # noqa: PLC0415
    return pa.schema([
        ("chunk_id", pa.string()), ("citekey", pa.string()), ("collection", pa.string()),
        ("doi", pa.string()), ("title", pa.string()), ("year", pa.int32()),
        ("source_path", pa.string()), ("source_sha256", pa.string()),
        ("page_start", pa.int32()), ("page_end", pa.int32()), ("section", pa.string()),
        ("heading", pa.string()), ("text", pa.string()), ("n_tokens", pa.int32()),
        ("cited_dois", pa.list_(pa.string())),
        ("embedding", pa.list_(pa.float32(), dim)),
    ])


def write_index(rows: List[Dict[str, Any]], out: Path, embedder: Embedder) -> None:
    import pyarrow as pa  # noqa: PLC0415
    import pyarrow.parquet as pq  # noqa: PLC0415
    schema = index_schema(embedder.dim)
    table = pa.Table.from_pylist(rows, schema=schema)
    metadata = {
        "anthive_library_index": json.dumps({
            "schema_version": SCHEMA_VERSION,
            "chunker_version": CHUNKER_VERSION,
            "embedding_model": embedder.model,
            "embedding_dim": embedder.dim,
            "embedding_normalised": True,
            "query_prefix": embedder.query_prefix,
            "document_prefix": embedder.doc_prefix,
            "built_at": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
            "n_chunks": table.num_rows,
            "n_sources": len({r["source_sha256"] for r in rows}),
        })
    }
    table = table.replace_schema_metadata({**(table.schema.metadata or {}), **metadata})
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(out.suffix + ".tmp")
    pq.write_table(table, tmp, compression="zstd")
    tmp.replace(out)


def read_index_metadata(path: Path) -> Dict[str, Any]:
    import pyarrow.parquet as pq  # noqa: PLC0415
    raw = (pq.read_schema(path).metadata or {}).get(b"anthive_library_index")
    return json.loads(raw) if raw else {}


# ── query side ───────────────────────────────────────────────────────────────

class LibraryIndex:
    """In-memory search over library_chunks.parquet.

    Keyword search is BM25 over chunk text (sparse matrix, no DuckDB
    extension download needed in locked-down containers). Semantic search is
    brute-force cosine over the stored, L2-normalised float32 embeddings,
    which is a few milliseconds for tens of thousands of chunks. Hybrid
    merges both rankings with reciprocal rank fusion.
    """

    def __init__(self, path: Path, embedder: Optional[Embedder] = None):
        import pyarrow.parquet as pq  # noqa: PLC0415
        self.path = Path(path)
        self.meta = read_index_metadata(self.path)
        table = pq.read_table(self.path)
        columns = [c for c in table.column_names if c != "embedding"]
        self.rows: List[Dict[str, Any]] = table.select(columns).to_pylist()
        emb = table.column("embedding").combine_chunks()
        dim = int(self.meta.get("embedding_dim") or 0) or (emb.type.list_size if hasattr(emb.type, "list_size") else 0)
        flat = emb.values.to_numpy(zero_copy_only=False).astype(np.float32, copy=False)
        self.vectors = flat.reshape(-1, dim) if dim and len(flat) else np.zeros((0, dim or 1), np.float32)
        self.embedder = embedder
        self.warnings: List[str] = []
        if embedder is not None and embedder.model != self.meta.get("embedding_model"):
            self.warnings.append(
                f"query model {embedder.model!r} does not match index model "
                f"{self.meta.get('embedding_model')!r}: semantic search disabled, keyword only")
            log.warning(self.warnings[-1])
            self.embedder = None
        self._build_bm25()

    @property
    def semantic_available(self) -> bool:
        return self.embedder is not None and len(self.rows) > 0

    # BM25 ------------------------------------------------------------------
    @staticmethod
    def _tokens(text: str) -> List[str]:
        return [t.lower() for t in _WORD_RE.findall(text) if len(t) > 1 and t.lower() not in _STOPWORDS]

    def _build_bm25(self, k1: float = 1.5, b: float = 0.75) -> None:
        from scipy.sparse import csr_matrix  # noqa: PLC0415
        vocab: Dict[str, int] = {}
        indptr, indices, data, lengths = [0], [], [], []
        for row in self.rows:
            counts = Counter(self._tokens((row.get("heading") or "") + " " + row["text"]))
            for term, n in counts.items():
                indices.append(vocab.setdefault(term, len(vocab)))
                data.append(n)
            indptr.append(len(indices))
            lengths.append(sum(counts.values()))
        n_docs = max(len(self.rows), 1)
        tf = csr_matrix((np.array(data, np.float32), np.array(indices), np.array(indptr)),
                        shape=(len(self.rows), max(len(vocab), 1)))
        df = np.bincount(np.array(indices, dtype=np.int64), minlength=max(len(vocab), 1)).astype(np.float32)
        self._idf = np.log(1 + (n_docs - df + 0.5) / (df + 0.5)).astype(np.float32)
        lengths_arr = np.array(lengths, np.float32)
        avg = float(lengths_arr.mean()) if len(lengths_arr) else 1.0
        tf = tf.tocoo()
        denom = tf.data + k1 * (1 - b + b * lengths_arr[tf.row] / avg)
        weights = tf.data * (k1 + 1) / denom
        self._bm25 = csr_matrix((weights, (tf.row, tf.col)), shape=tf.shape).tocsc()
        self._vocab = vocab

    def _keyword_scores(self, query: str) -> np.ndarray:
        scores = np.zeros(len(self.rows), np.float32)
        for term in set(self._tokens(query)):
            col = self._vocab.get(term)
            if col is not None:
                column = self._bm25.getcol(col)
                scores[column.indices] += column.data * self._idf[col]
        return scores

    def _semantic_scores(self, query: str) -> np.ndarray:
        vector = self.embedder.embed_query(query)
        return self.vectors @ vector

    # search ----------------------------------------------------------------
    def _mask(self, citekeys: Optional[Iterable[str]], collections: Optional[Iterable[str]],
              sections: Optional[Iterable[str]], year_min: Optional[int], year_max: Optional[int],
              include_references: bool) -> np.ndarray:
        keep = np.ones(len(self.rows), bool)
        ck = set(citekeys) if citekeys else None
        co = set(collections) if collections else None
        se = set(sections) if sections else None
        for i, row in enumerate(self.rows):
            if ck is not None and row.get("citekey") not in ck:
                keep[i] = False
            elif co is not None and row.get("collection") not in co:
                keep[i] = False
            elif se is not None and row["section"] not in se:
                keep[i] = False
            elif se is None and not include_references and row["section"] in NON_SEARCHABLE_SECTIONS:
                keep[i] = False
            elif year_min is not None and (row.get("year") or 0) < year_min:
                keep[i] = False
            elif year_max is not None and (row.get("year") or 9999) > year_max:
                keep[i] = False
        return keep

    def search(self, query: str, k: int = 10, mode: str = "hybrid", *,
               citekeys: Optional[Iterable[str]] = None, collections: Optional[Iterable[str]] = None,
               sections: Optional[Iterable[str]] = None, year_min: Optional[int] = None,
               year_max: Optional[int] = None, include_references: bool = False,
               rrf_k: int = 60) -> Dict[str, Any]:
        """Return {"mode", "results": [...], "warnings": [...]}. ``mode`` is
        keyword, semantic or hybrid; semantic/hybrid fall back to keyword
        (with a warning) when no matching query embedder is available."""
        warnings = list(self.warnings)
        if mode in ("semantic", "hybrid") and not self.semantic_available:
            warnings.append("semantic search unavailable: answered with keyword search")
            mode = "keyword"
        keep = self._mask(citekeys, collections, sections, year_min, year_max, include_references)
        candidates = np.flatnonzero(keep)
        if len(candidates) == 0:
            return {"mode": mode, "results": [], "warnings": warnings}
        if mode == "keyword":
            scores = self._keyword_scores(query)[candidates]
            idx = np.argsort(-scores, kind="stable")
            idx = idx[scores[idx] > 0][:k]
            ranked = [(int(candidates[j]), float(scores[j])) for j in idx]
        elif mode == "semantic":
            scores = self._semantic_scores(query)[candidates]
            idx = np.argsort(-scores, kind="stable")[:k]
            ranked = [(int(candidates[j]), float(scores[j])) for j in idx]
        else:
            depth = max(50, k * 5)
            kw = self._keyword_scores(query)[candidates]
            se = self._semantic_scores(query)[candidates]
            fused: Dict[int, float] = {}
            kw_order = [j for j in np.argsort(-kw, kind="stable")[:depth] if kw[j] > 0]
            for rank, j in enumerate(kw_order):
                fused[int(candidates[j])] = fused.get(int(candidates[j]), 0.0) + 1.0 / (rrf_k + rank + 1)
            for rank, j in enumerate(np.argsort(-se, kind="stable")[:depth]):
                fused[int(candidates[j])] = fused.get(int(candidates[j]), 0.0) + 1.0 / (rrf_k + rank + 1)
            ranked = sorted(fused.items(), key=lambda item: -item[1])[:k]
        results = []
        for i, score in ranked:
            row = dict(self.rows[i])
            row["score"] = round(score, 6)
            results.append(row)
        return {"mode": mode, "results": results, "warnings": warnings}


_STOPWORDS = frozenset("""
a an and are as at be been being by can could did do does for from had has have how in into is it its
of on or our such than that the their them then there these they this those to was we were what when
where which while who whom why will with within would you your also using used use between both each
other more most some any all not no nor only same so very via per fig figure et al
""".split())
