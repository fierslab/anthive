"""Regression tests for the ant-h5ad-check preserve/regenerate contract.

Historical bug: `preserve_user_customizations` used a fixed 4-field whitelist
(title, description, layer_description, embedding_description). Anything else
in the .md — id, year, api (obs_rename/hidden/derive), caveats, DOI —
got silently dropped every time the h5ad's sha256 changed (which happens
on any h5ad rewrite).

These tests pin the invariant: the FUNCTION preserves everything except the
fields explicitly in `REGENERATED_FIELDS`.

The tests import the module directly from `bin/ingest/ant-h5ad-check`.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
ANT_H5AD_CHECK = REPO_ROOT / "bin" / "ingest" / "ant-h5ad-check"


def _load_module():
    """Import the ant-h5ad-check uv-script as a module.

    The script has no `.py` extension so ``spec_from_file_location`` won't
    auto-pick a loader — we hand it a ``SourceFileLoader`` explicitly. Also
    add ``lib/`` to ``sys.path`` so the script's ``from metadata import ...``
    resolves. Guard the heavy PEP-723 imports (scanpy, anndata, h5py) by
    setting ``ANT_H5AD_CHECK_TESTING`` — the script skips them when set;
    if it doesn't guard, we still catch ImportError so pure-function tests
    can proceed on the module partial-namespace.
    """
    from importlib.machinery import SourceFileLoader

    lib_dir = str(REPO_ROOT / "lib")
    if lib_dir not in sys.path:
        sys.path.insert(0, lib_dir)

    loader = SourceFileLoader("ant_h5ad_check", str(ANT_H5AD_CHECK))
    spec = importlib.util.spec_from_loader("ant_h5ad_check", loader)
    module = importlib.util.module_from_spec(spec)
    try:
        loader.exec_module(module)
    except Exception:
        # Fall back: the top-level `if __name__ == '__main__'` guard means
        # module import shouldn't run the CLI; a heavy dep failure would come
        # from top-of-file imports (scanpy, anndata). Re-raise so the test
        # user knows their env is incomplete.
        raise
    return module


@pytest.fixture(scope="module")
def h5ad_check():
    return _load_module()


class TestRegeneratedFields:
    def test_regenerated_fields_frozen_set(self, h5ad_check):
        assert isinstance(h5ad_check.REGENERATED_FIELDS, frozenset)

    def test_regenerated_fields_expected_members(self, h5ad_check):
        # Fields that MUST come from the current h5ad state — never preserved
        expected = {
            "sha256", "n_obs", "n_vars", "version", "date",
            "layer_stats",
            "layers_include", "obsm_include",
            "obsnum_include", "obscat_include",
            "default_layer",
        }
        assert expected <= h5ad_check.REGENERATED_FIELDS


class TestPreserveUserCustomizations:
    """The critical invariant: preserve everything except REGENERATED_FIELDS."""

    def test_preserves_id_field(self, h5ad_check):
        """`id:` MUST survive a regeneration — historical bug pinned here."""
        old = {"id": "ABC", "sha256": "old", "title": "Old title"}
        new = {"sha256": "new", "n_obs": 100, "n_vars": 5000}
        h5ad_check.preserve_user_customizations(old, new)
        assert new["id"] == "ABC"

    def test_preserves_year_field(self, h5ad_check):
        old = {"year": 2022, "sha256": "old"}
        new = {"sha256": "new"}
        h5ad_check.preserve_user_customizations(old, new)
        assert new["year"] == 2022

    def test_preserves_caveats_block(self, h5ad_check):
        """`caveats:` warning blocks are user content and MUST survive."""
        caveats = [
            {"level": "medium", "kind": "contamination",
             "scope": "microglia_subcluster=4",
             "message": "5,827 cells co-express astro markers",
             "detected_at": "2026-08-16"},
        ]
        old = {"id": "MPF", "caveats": caveats, "sha256": "old"}
        new = {"sha256": "new"}
        h5ad_check.preserve_user_customizations(old, new)
        assert new["caveats"] == caveats

    def test_preserves_api_block(self, h5ad_check):
        """`api.obs_rename` / `obs_hidden` / `obs_derive` MUST survive."""
        api = {
            "obs_rename": {"individualID": "donor_id"},
            "obs_hidden": ["vcf_id"],
            "obs_derive": {
                "astro_contaminated": {
                    "source": "microglia_subcluster",
                    "dtype": "categorical",
                    "map": {"4": "yes"},
                    "default": "no",
                }
            },
        }
        old = {"id": "MPF", "api": api, "sha256": "old"}
        new = {"sha256": "new"}
        h5ad_check.preserve_user_customizations(old, new)
        assert new["api"] == api

    def test_preserves_doi_and_paper_metadata(self, h5ad_check):
        old = {
            "id": "MIC",
            "doi": "10.1038/s41586-024-07871-6",
            "journal": "Nature",
            "paper_title": "ROSMAP DLPFC microglia atlas",
            "pmid": "12345",
            "sha256": "old",
        }
        new = {"sha256": "new"}
        h5ad_check.preserve_user_customizations(old, new)
        for k in ("doi", "journal", "paper_title", "pmid"):
            assert new[k] == old[k], f"lost {k}"

    def test_preserves_unknown_custom_field(self, h5ad_check):
        """Any user-added top-level field survives — forwards-compatible."""
        old = {
            "id": "XYZ", "sha256": "old",
            "wgcna_module_colors": ["black", "blue", "brown"],
            "z_score_layer": "wgcna_z",
            "extra_note": "regenerated 2026-08-16 for donor sweep",
        }
        new = {"sha256": "new"}
        h5ad_check.preserve_user_customizations(old, new)
        for k in ("wgcna_module_colors", "z_score_layer", "extra_note"):
            assert new[k] == old[k], f"lost custom {k}"

    def test_regenerates_sha256_and_size_fields(self, h5ad_check):
        """The regenerated fields OVERRIDE anything in old_metadata."""
        old = {
            "id": "ABC", "sha256": "old_hash",
            "n_obs": 100, "n_vars": 5000,
            "layer_stats": {"X": {"min_value": 0.0, "max_value": 5.0}},
        }
        new = {
            "sha256": "new_hash",
            "n_obs": 200, "n_vars": 6000,
            "layer_stats": {"X": {"min_value": 0.1, "max_value": 6.5}},
        }
        h5ad_check.preserve_user_customizations(old, new)
        assert new["sha256"] == "new_hash"
        assert new["n_obs"] == 200
        assert new["n_vars"] == 6000
        assert new["layer_stats"]["X"]["max_value"] == 6.5

    def test_regenerated_include_lists_not_preserved(self, h5ad_check):
        """Include lists come from the current h5ad — old ones are dropped."""
        old = {
            "id": "ABC", "sha256": "old",
            "layers_include": ["X", "counts", "spliced"],
            "obsm_include": ["X_umap", "X_tsne_old"],
        }
        new = {"sha256": "new",
               "layers_include": ["X", "counts"],
               "obsm_include": ["X_umap"]}
        h5ad_check.preserve_user_customizations(old, new)
        assert new["layers_include"] == ["X", "counts"]
        assert new["obsm_include"] == ["X_umap"]

    def test_prunes_layer_description_for_missing_layers(self, h5ad_check):
        old = {
            "id": "ABC", "sha256": "old",
            "layer_description": {
                "X": "Log-norm",
                "counts": "Raw",
                "spliced": "Velocity spliced",
            },
        }
        new = {"sha256": "new", "layers_include": ["X", "counts"]}
        h5ad_check.preserve_user_customizations(old, new)
        assert new["layer_description"] == {"X": "Log-norm", "counts": "Raw"}
        assert "spliced" not in new["layer_description"]

    def test_prunes_embedding_description_for_missing_obsm(self, h5ad_check):
        old = {
            "id": "ABC", "sha256": "old",
            "embedding_description": {"X_umap": "Umap", "X_pca": "PCA"},
        }
        new = {"sha256": "new", "obsm_include": ["X_umap"]}
        h5ad_check.preserve_user_customizations(old, new)
        assert new["embedding_description"] == {"X_umap": "Umap"}

    def test_copies_exclude_lists(self, h5ad_check):
        old = {
            "id": "ABC", "sha256": "old",
            "layers_exclude": ["ambient"],
            "obs_exclude": ["hash.ID"],
            "var_exclude": ["MIR6859-1"],
            "obsm_exclude": ["X_scvi"],
        }
        new = {"sha256": "new"}
        h5ad_check.preserve_user_customizations(old, new)
        for k in ("layers_exclude", "obs_exclude", "var_exclude", "obsm_exclude"):
            assert new[k] == old[k]

    def test_end_to_end_realistic_regeneration(self, h5ad_check):
        """Simulate the actual bug scenario: h5ad rewritten → sha256 changed
        → regeneration triggered → check everything critical survives."""
        old = {
            # user-owned identifying fields
            "id": "MPF", "year": 2024,
            # publication metadata
            "doi": "10.1038/s41586-024-07606-7",
            "journal": "Nature",
            "paper_title": "Mathys 2024 multi-region microglia",
            # anthive api directives (would silently break the catalog if lost)
            "api": {
                "obs_rename": {"individualID": "donor_id"},
                "obs_hidden": ["hash.ID", "vcf_id"],
                "obs_derive": {
                    "astro_contaminated": {
                        "source": "microglia_subcluster",
                        "dtype": "categorical",
                        "map": {"4": "yes"},
                        "default": "no",
                    }
                },
            },
            # curation caveats block
            "caveats": [
                {"level": "medium", "kind": "contamination",
                 "message": "5827 cells GFAP+"},
            ],
            # user-selected top-N obs
            "top_cat": ["disease", "sex"],
            "top_genes": ["P2RY12", "TMEM119"],
            # to-be-regenerated (should NOT be preserved)
            "sha256": "OLD_HASH",
            "n_obs": 8000,
            "n_vars": 30000,
            "layer_stats": {"X": {"max_value": 5.0}},
        }
        new = {
            "sha256": "NEW_HASH",
            "n_obs": 8673,
            "n_vars": 33694,
            "layer_stats": {"X": {"max_value": 6.7}},
            "layers_include": ["X", "counts"],
            "obsm_include": ["X_umap"],
        }
        h5ad_check.preserve_user_customizations(old, new)
        # User-owned survives
        assert new["id"] == "MPF"
        assert new["year"] == 2024
        assert new["doi"] == "10.1038/s41586-024-07606-7"
        assert new["api"]["obs_rename"] == {"individualID": "donor_id"}
        assert new["api"]["obs_derive"]["astro_contaminated"]["source"] == "microglia_subcluster"
        assert len(new["caveats"]) == 1
        assert new["top_cat"] == ["disease", "sex"]
        # Regenerated overrides
        assert new["sha256"] == "NEW_HASH"
        assert new["n_obs"] == 8673
        assert new["layer_stats"]["X"]["max_value"] == 6.7
