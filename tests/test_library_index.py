"""Tests for lib/library_index.py: chunking, artifact schema, incremental
builds, model-mismatch handling and hybrid ranking (fake embedder, no
network, no model download)."""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

pytest.importorskip("pyarrow")
pytest.importorskip("scipy")

import library_index as li  # noqa: E402


class FakeEmbedder:
    """Deterministic bag-of-words hashing embedder (stands in for fastembed)."""

    def __init__(self, model: str = "fake/model-1", dim: int = 64):
        self.model, self.dim = model, dim
        self.query_prefix = self.doc_prefix = ""
        self.calls = 0

    def _vec(self, text: str) -> np.ndarray:
        vector = np.zeros(self.dim, np.float32)
        for word in li._WORD_RE.findall(text.lower()):
            vector[int(hashlib.md5(word.encode()).hexdigest(), 16) % self.dim] += 1
        norm = np.linalg.norm(vector)
        return vector / norm if norm else vector

    def embed_documents(self, texts):
        self.calls += 1
        return np.array([self._vec(t) for t in texts], np.float32)

    def embed_query(self, text):
        return self._vec(text)


def sentence(n: int, topic: str = "microglia") -> str:
    return " ".join(f"The {topic} sample number {i} showed a clear response in tissue." for i in range(n))


PAGES = [
    (1, "# Title of the paper\n\n**Abstract**\n\n" + sentence(5, "amyloid")),
    (2, "# Introduction\n\n" + sentence(30, "background")),
    (3, "# Results\n\n## Microglia respond to plaques\n\n" + sentence(60, "microglia")
        + "\n\n**Fig. 1 | Microglia around plaques.** a, UMAP of cells. b, Dot plot of markers.\n\n"
        + "0.12 0.33 0.45 0.98 0.11 0.02 0.77 0.65 0.31 0.08 0.44 0.19"),
    (4, "# Methods\n\n## Single nucleus isolation\n\n" + sentence(40, "isolation")),
    (5, "# References\n\n1. Smith J. Paper. Nature 2020. doi:10.1038/s41586-020-0001-1\n\n"
        "2. Jones K. Other. Cell 2021. https://doi.org/10.1016/j.cell.2021.01.001"),
]


def test_section_tagging_and_legends():
    chunks = li.blocks_to_chunks(li.blocks_from_page_markdown(PAGES))
    sections = {c.section for c in chunks}
    assert {"abstract", "introduction", "results", "figure_legend", "methods", "references"} <= sections
    legend = [c for c in chunks if c.section == "figure_legend"]
    assert legend and legend[0].text.startswith("Fig. 1")
    methods = [c for c in chunks if c.section == "methods"]
    assert all(c.page_start == 4 for c in methods)
    assert "Single nucleus isolation" in methods[0].text      # subheading kept as context
    refs = [c for c in chunks if c.section == "references"]
    assert "10.1038/s41586-020-0001-1" in refs[0].dois
    assert "10.1016/j.cell.2021.01.001" in refs[0].dois


def test_chunk_sizes_and_debris_dropped():
    chunks = li.blocks_to_chunks(li.blocks_from_page_markdown(PAGES), target_tokens=400, max_tokens=500)
    assert all(c.n_tokens <= 520 for c in chunks)
    assert not any("0.12 0.33 0.45" in c.text for c in chunks)
    results = [c for c in chunks if c.section == "results"]
    assert len(results) >= 2                                   # 60 sentences do not fit one chunk


def test_classify_heading_variants():
    assert li.classify_heading("43 **Abstract**") == "abstract"
    assert li.classify_heading("STAR★Methods") is None or li.classify_heading("STAR+Methods") == "methods"
    assert li.classify_heading("Materials and Methods") == "methods"
    assert li.classify_heading("Online Methods") == "methods"
    assert li.classify_heading("Microglia respond to plaques") is None
    assert li.is_figure_legend("Extended Data Fig. 3 | Something")


def _make_pdfs(tmp_path: Path, names_and_texts):
    folder = tmp_path / "pdfs"
    folder.mkdir(exist_ok=True)
    paths = []
    for name, text in names_and_texts:
        path = folder / name
        path.write_text(text)          # fake "PDF": the fake parser reads text
        paths.append(path)
    return folder, paths


def fake_parse(path: Path):
    text = path.read_text()
    return li.blocks_from_page_markdown([(1, "# Abstract\n\n" + text), (2, "# Results\n\n" + text)])


def test_artifact_schema_and_metadata(tmp_path):
    pa = pytest.importorskip("pyarrow")
    import pyarrow.parquet as pq
    folder, paths = _make_pdfs(tmp_path, [("a.pdf", sentence(20, "astrocyte")), ("b.pdf", sentence(20, "neuron"))])
    out = tmp_path / "idx.parquet"
    emb = FakeEmbedder()
    stats = li.build_index(paths, out, embedder=emb, parse=fake_parse, source_root=folder)
    assert stats["parsed"] == 2 and stats["chunks"] > 0
    table = pq.read_table(out)
    field = table.schema.field("embedding")
    assert pa.types.is_fixed_size_list(field.type) and field.type.list_size == 64
    assert field.type.value_type == pa.float32()
    for column in ("chunk_id", "citekey", "source_path", "source_sha256", "page_start",
                   "page_end", "section", "text", "n_tokens"):
        assert column in table.column_names
    meta = li.read_index_metadata(out)
    assert meta["embedding_model"] == "fake/model-1" and meta["embedding_dim"] == 64
    assert meta["chunker_version"] == li.CHUNKER_VERSION and meta["embedding_normalised"] is True
    assert set(table.column("source_path").to_pylist()) == {"a.pdf", "b.pdf"}


def test_incremental_update(tmp_path):
    import pyarrow.parquet as pq
    folder, paths = _make_pdfs(tmp_path, [("a.pdf", sentence(20, "astrocyte")), ("b.pdf", sentence(20, "neuron"))])
    out = tmp_path / "idx.parquet"
    li.build_index(paths, out, embedder=FakeEmbedder(), parse=fake_parse, source_root=folder)
    first = pq.read_table(out).to_pylist()

    # unchanged: nothing parsed or embedded again
    emb = FakeEmbedder()
    stats = li.build_index(paths, out, embedder=emb, parse=fake_parse, source_root=folder)
    assert stats["parsed"] == 0 and stats["reused"] == 2 and emb.calls == 0

    # change b, drop a
    paths[1].write_text(sentence(20, "oligodendrocyte"))
    emb = FakeEmbedder()
    stats = li.build_index([paths[1]], out, embedder=emb, parse=fake_parse, source_root=folder)
    assert stats["parsed"] == 1 and emb.calls == 1
    rows = pq.read_table(out).to_pylist()
    assert {r["source_path"] for r in rows} == {"b.pdf"}
    assert any("oligodendrocyte" in r["text"] for r in rows)
    assert {r["source_sha256"] for r in rows}.isdisjoint({r["source_sha256"] for r in first if r["source_path"] == "b.pdf"})

    # different model: full rebuild even without --full
    emb = FakeEmbedder(model="fake/model-2")
    stats = li.build_index([paths[1]], out, embedder=emb, parse=fake_parse, source_root=folder)
    assert stats["parsed"] == 1 and li.read_index_metadata(out)["embedding_model"] == "fake/model-2"


def _toy_index(tmp_path, emb=None):
    folder, paths = _make_pdfs(tmp_path, [
        ("trem2.pdf", sentence(15, "TREM2 microglia plaque")),
        ("astro.pdf", sentence(15, "reactive astrocyte inflammation")),
        ("ms.pdf", sentence(15, "oligodendrocyte multiple sclerosis lesion")),
    ])
    out = tmp_path / "idx.parquet"
    li.build_index(paths, out, embedder=emb or FakeEmbedder(), parse=fake_parse, source_root=folder)
    return out


def test_hybrid_ranking_and_filters(tmp_path):
    out = _toy_index(tmp_path)
    index = li.LibraryIndex(out, embedder=FakeEmbedder())
    for mode in ("keyword", "semantic", "hybrid"):
        res = index.search("TREM2 microglia around plaques", k=3, mode=mode)
        assert res["mode"] == mode and res["results"]
        assert res["results"][0]["source_path"] == "trem2.pdf", mode
    only_results = index.search("astrocyte", k=10, sections=["results"])
    assert only_results["results"] and all(r["section"] == "results" for r in only_results["results"])
    none = index.search("astrocyte", k=10, collections=["NoSuchCollection"])
    assert none["results"] == []


def test_model_mismatch_falls_back_to_keyword(tmp_path):
    out = _toy_index(tmp_path)
    index = li.LibraryIndex(out, embedder=FakeEmbedder(model="other/model"))
    assert not index.semantic_available
    res = index.search("multiple sclerosis lesion", mode="hybrid")
    assert res["mode"] == "keyword"
    assert any("does not match" in w for w in res["warnings"])
    assert res["results"][0]["source_path"] == "ms.pdf"


def test_no_embedder_keyword_only(tmp_path):
    out = _toy_index(tmp_path)
    index = li.LibraryIndex(out)
    res = index.search("reactive astrocyte", mode="semantic")
    assert res["mode"] == "keyword" and res["results"][0]["source_path"] == "astro.pdf"


def test_library_copy_deduplicated_and_citekey_attached(tmp_path):
    import pyarrow.parquet as pq
    store = tmp_path / "store" / "Lee2026" / "static"
    store.mkdir(parents=True)
    (tmp_path / "store" / "Lee2026" / "index.md").write_text(
        "---\ndoi: 10.1/XYZ\npaper_title: Store title\nyear: 2026\n---\n")
    pdf = store / "lee.pdf"
    pdf.write_text(sentence(10, "perivascular macrophage"))
    lib = tmp_path / "library" / "papers"
    lib.mkdir(parents=True)
    (lib / "lee2026plasticity.pdf").write_text(pdf.read_text())          # identical copy
    (lib / "lee2026plasticity.md").write_text(
        "---\ncitekey: lee2026plasticity\ntitle: Library title\ndate: '2026-03-01'\n"
        "doi: 10.1/xyz\nfile: lee2026plasticity.pdf\n---\nnotes\n")
    out = tmp_path / "idx.parquet"
    stats = li.build_index([pdf, lib / "lee2026plasticity.pdf"], out, embedder=FakeEmbedder(),
                           parse=fake_parse, store_root=tmp_path / "store",
                           library_dir=tmp_path / "library",
                           source_root=[tmp_path / "store", tmp_path / "library"])
    assert stats["sources"] == 1 and stats["duplicates"] == 1
    rows = pq.read_table(out).to_pylist()
    assert {r["citekey"] for r in rows} == {"lee2026plasticity"}
    assert {r["collection"] for r in rows} == {"Lee2026"}
    assert {r["source_path"] for r in rows} == {"Lee2026/static/lee.pdf"}
    assert {r["title"] for r in rows} == {"Library title"} and {r["year"] for r in rows} == {2026}
