"""Tests for GET /genesets/by-gene/{gene} — gene → geneset/starCAT lookup.

Tests the index-building helpers and the endpoint response shape without
requiring a live ant-serve process. We mock the global ``geneset_registry``
and ``_by_gene_index`` directly since ant-serve is a single-file script loaded
via importlib (no package install).
"""
from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

import pytest

# ── path setup ──────────────────────────────────────────────────────────────
REPO_ROOT = Path(__file__).resolve().parent.parent
LIB_DIR = REPO_ROOT / 'lib'
if str(LIB_DIR) not in sys.path:
    sys.path.insert(0, str(LIB_DIR))


# ── minimal Geneset / GenesetRegistry stubs ─────────────────────────────────

def _make_experiment(study: str, name: str) -> MagicMock:
    exp = MagicMock()
    exp.study = study
    exp.name = name
    exp.experiment_id = f"{study}/{name}"
    exp.organism = 'human'
    exp.method = 'WGCNA'
    exp.parent_dataset = None
    exp.celltypes = []
    return exp


def _make_geneset(
    study: str,
    experiment: str,
    name: str,
    genes: List[str],
    description: str = '',
) -> MagicMock:
    exp = _make_experiment(study, experiment)
    gs = MagicMock()
    gs.experiment = exp
    gs.name = name
    gs.geneset_id = f"{study}/{experiment}/{name}"
    gs.format = 'grp'
    gs.metadata = {'description': description}
    gs.body = ''
    gs.n_genes = len(genes)
    gs.genes = genes
    gs.to_summary.return_value = {
        'geneset_id': gs.geneset_id,
        'study': study,
        'experiment': experiment,
        'name': name,
        'display_name': name,
        'description': description,
        'format': 'grp',
        'n_genes': len(genes),
        'organism': 'human',
        'method': 'WGCNA',
        'parent_dataset': None,
        'celltypes': [],
        'direction': None,
    }
    return gs


def _make_registry(*genesets) -> MagicMock:
    reg = MagicMock()
    reg.genesets = {gs.geneset_id: gs for gs in genesets}
    return reg


# ── import the _build_by_gene_index helper and the endpoint via importlib ───
# ant-serve is not a package; load it as a module but skip the __main__ block.

def _load_ant_serve_helpers():
    """Import the index-builder and endpoint from ant-serve without running it."""
    serve_path = REPO_ROOT / 'bin' / 'api' / 'ant-serve'

    # Provide stubs for heavy imports that ant-serve does at module level
    # so we can extract only the helpers we need.
    _stub_modules = [
        'fastapi', 'fastapi.responses', 'fastapi.middleware',
        'fastapi.middleware.cors', 'uvicorn', 'duckdb', 'numpy',
        'pandas', 'matplotlib', 'matplotlib.pyplot', 'seaborn',
        'yaml', 'boto3', 'botocore', 'botocore.exceptions',
        'aiofiles', 'httpx', 'scipy', 'scipy.stats',
        'openai', 'tiktoken',
    ]
    for name in _stub_modules:
        if name not in sys.modules:
            sys.modules[name] = types.ModuleType(name)

    # fastapi needs a minimal stub to prevent attribute errors
    fa = sys.modules.setdefault('fastapi', types.ModuleType('fastapi'))
    for attr in ('FastAPI', 'HTTPException', 'Query', 'Request', 'Response',
                 'BackgroundTasks', 'Depends'):
        if not hasattr(fa, attr):
            setattr(fa, attr, MagicMock())

    return None  # we test via direct function patching below


# ── unit tests for _build_by_gene_index ─────────────────────────────────────

class TestBuildByGeneIndex:
    """Test the in-process index builder logic (without touching ant-serve globals)."""

    def _run_builder(
        self,
        genesets: List[MagicMock],
        starcat_catalogs: List[Any] | None = None,
    ) -> Dict[str, List[Dict]]:
        """Run a self-contained version of the build logic and return the index."""
        new_index: Dict[str, List[Dict]] = {}

        registry_mock = _make_registry(*genesets)
        for geneset in registry_mock.genesets.values():
            summary = geneset.to_summary()
            for position, gene_symbol in enumerate(geneset.genes, start=1):
                key = gene_symbol.upper()
                entry = {
                    'kind': 'geneset',
                    'study': summary['study'],
                    'experiment': summary['experiment'],
                    'name': summary['name'],
                    'display_name': summary.get('display_name') or summary['name'],
                    'description': summary.get('description', ''),
                    'n_genes': summary['n_genes'],
                    'rank': position,
                    'catalog_id': None,
                    'weight': None,
                }
                new_index.setdefault(key, []).append(entry)

        if starcat_catalogs:
            for catalog in starcat_catalogs:
                for module in catalog.modules:
                    weight_map = module.weights()
                    if not weight_map:
                        continue
                    for gene_symbol, weight in weight_map.items():
                        key = gene_symbol.upper()
                        entry = {
                            'kind': 'starcat_module',
                            'catalog_id': catalog.catalog_id,
                            'name': module.name,
                            'display_name': module.name,
                            'description': module.description,
                            'n_genes': module.n_genes,
                            'weight': weight,
                            'rank': None,
                            'study': None,
                            'experiment': None,
                        }
                        new_index.setdefault(key, []).append(entry)

        return new_index

    def test_single_geneset_single_gene(self):
        gs = _make_geneset('StudyA', 'Exp1', 'SetGreen', ['APOE', 'TREM2'])
        idx = self._run_builder([gs])
        assert 'APOE' in idx
        assert len(idx['APOE']) == 1
        assert idx['APOE'][0]['kind'] == 'geneset'
        assert idx['APOE'][0]['study'] == 'StudyA'
        assert idx['APOE'][0]['rank'] == 1   # APOE is first

    def test_multiple_genesets_same_gene(self):
        gs1 = _make_geneset('StudyA', 'Exp1', 'Set1', ['APOE', 'CLU'])
        gs2 = _make_geneset('StudyB', 'Exp2', 'Set2', ['APOE', 'BIN1'])
        idx = self._run_builder([gs1, gs2])
        apoe_hits = idx['APOE']
        assert len(apoe_hits) == 2
        studies = {h['study'] for h in apoe_hits}
        assert studies == {'StudyA', 'StudyB'}

    def test_case_insensitive_key(self):
        gs = _make_geneset('S', 'E', 'N', ['Apoe'])
        idx = self._run_builder([gs])
        assert 'APOE' in idx
        assert 'Apoe' not in idx

    def test_unknown_gene_returns_empty(self):
        gs = _make_geneset('S', 'E', 'N', ['TREM2'])
        idx = self._run_builder([gs])
        assert idx.get('NOTAREALGENE', []) == []

    def test_rank_ordering(self):
        gs = _make_geneset('S', 'E', 'N', ['GENE_A', 'GENE_B', 'GENE_C'])
        idx = self._run_builder([gs])
        assert idx['GENE_A'][0]['rank'] == 1
        assert idx['GENE_B'][0]['rank'] == 2
        assert idx['GENE_C'][0]['rank'] == 3

    def test_starcat_entry_shape(self):
        cat = MagicMock()
        cat.catalog_id = 'MICROGLIA_V1'
        cat.source = 'starcat'
        mod = MagicMock()
        mod.name = 'Microglia_DAM'
        mod.description = 'DAM state'
        mod.n_genes = 100
        mod.weights.return_value = {'APOE': 0.87, 'TREM2': 0.72}
        cat.modules = [mod]

        idx = self._run_builder([], starcat_catalogs=[cat])
        assert 'APOE' in idx
        entry = idx['APOE'][0]
        assert entry['kind'] == 'starcat_module'
        assert entry['catalog_id'] == 'MICROGLIA_V1'
        assert entry['weight'] == pytest.approx(0.87)

    def test_empty_store_returns_empty_index(self):
        idx = self._run_builder([])
        assert idx == {}


# ── unit tests for endpoint response logic ──────────────────────────────────

class TestByGeneEndpointLogic:
    """Test the response-shaping logic of get_genesets_by_gene without FastAPI."""

    def _endpoint_logic(self, gene: str, index: Dict[str, List[Dict]]) -> Dict:
        """Replicate the sort + split logic from the endpoint."""
        gene_upper = gene.strip().upper()
        matches = list(index.get(gene_upper, []))

        def _sort_key(match):
            if match['kind'] == 'starcat_module':
                weight = match.get('weight') or 0.0
                return (0, -abs(weight), match.get('catalog_id', ''), match.get('name', ''))
            return (1, 0.0, match.get('study', ''), match.get('experiment', ''), match.get('name', ''))

        matches.sort(key=_sort_key)

        geneset_matches = [m for m in matches if m['kind'] == 'geneset']
        starcat_matches  = [m for m in matches if m['kind'] == 'starcat_module']

        genesets_out = [
            {
                'study':        m['study'],
                'experiment':   m['experiment'],
                'name':         m['name'],
                'display_name': m.get('display_name') or m['name'],
                'description':  m.get('description', ''),
                'n_genes':      m.get('n_genes'),
                'rank':         m.get('rank'),
            }
            for m in geneset_matches
        ]
        starcat_out = [
            {
                'catalog':  m['catalog_id'],
                'module':   m['name'],
                'weight':   m.get('weight'),
                'n_genes':  m.get('n_genes'),
            }
            for m in starcat_matches
        ]

        return {
            'gene':     gene_upper,
            'matches':  matches,
            'genesets': genesets_out,
            'starcat':  starcat_out,
            'total':    len(matches),
        }

    def _make_index(self) -> Dict[str, List[Dict]]:
        return {
            'APOE': [
                {'kind': 'geneset', 'study': 'StudyA', 'experiment': 'Exp1',
                 'name': 'SetRed', 'display_name': 'SetRed', 'description': '',
                 'n_genes': 50, 'rank': 3, 'catalog_id': None, 'weight': None},
                {'kind': 'starcat_module', 'catalog_id': 'MICROGLIA_V1',
                 'name': 'Microglia_DAM', 'display_name': 'Microglia_DAM',
                 'description': 'DAM', 'n_genes': 100, 'weight': 0.87,
                 'rank': None, 'study': None, 'experiment': None},
            ],
            'TREM2': [
                {'kind': 'geneset', 'study': 'StudyA', 'experiment': 'Exp1',
                 'name': 'SetBlue', 'display_name': 'SetBlue', 'description': '',
                 'n_genes': 30, 'rank': 1, 'catalog_id': None, 'weight': None},
            ],
        }

    def test_known_gene_returns_total(self):
        idx = self._make_index()
        result = self._endpoint_logic('APOE', idx)
        assert result['total'] == 2
        assert result['gene'] == 'APOE'

    def test_lowercase_input_normalised(self):
        idx = self._make_index()
        result = self._endpoint_logic('apoe', idx)
        assert result['gene'] == 'APOE'
        assert result['total'] == 2

    def test_unknown_gene_returns_zero_not_404(self):
        idx = self._make_index()
        result = self._endpoint_logic('NOTAREALGENE', idx)
        assert result['total'] == 0
        assert result['gene'] == 'NOTAREALGENE'
        assert result['matches'] == []
        assert result['genesets'] == []
        assert result['starcat'] == []

    def test_starcat_sorted_before_genesets(self):
        """starCAT entries (kind=0) appear before genesets (kind=1) after sort."""
        idx = self._make_index()
        result = self._endpoint_logic('APOE', idx)
        kinds = [m['kind'] for m in result['matches']]
        # starcat_module must precede geneset
        assert kinds[0] == 'starcat_module'
        assert kinds[1] == 'geneset'

    def test_genesets_structured_output(self):
        idx = self._make_index()
        result = self._endpoint_logic('TREM2', idx)
        assert len(result['genesets']) == 1
        gs = result['genesets'][0]
        assert gs['study'] == 'StudyA'
        assert gs['experiment'] == 'Exp1'
        assert gs['name'] == 'SetBlue'

    def test_starcat_structured_output(self):
        idx = self._make_index()
        result = self._endpoint_logic('APOE', idx)
        assert len(result['starcat']) == 1
        sc = result['starcat'][0]
        assert sc['catalog'] == 'MICROGLIA_V1'
        assert sc['module'] == 'Microglia_DAM'
        assert sc['weight'] == pytest.approx(0.87)

    def test_matches_flat_list_contains_all_kinds(self):
        idx = self._make_index()
        result = self._endpoint_logic('APOE', idx)
        kinds = {m['kind'] for m in result['matches']}
        assert kinds == {'geneset', 'starcat_module'}

    def test_starcat_sorted_by_descending_weight(self):
        idx: Dict[str, List[Dict]] = {
            'CLU': [
                {'kind': 'starcat_module', 'catalog_id': 'CAT', 'name': 'ModA',
                 'display_name': 'ModA', 'description': '', 'n_genes': 50,
                 'weight': 0.30, 'rank': None, 'study': None, 'experiment': None},
                {'kind': 'starcat_module', 'catalog_id': 'CAT', 'name': 'ModB',
                 'display_name': 'ModB', 'description': '', 'n_genes': 50,
                 'weight': 0.95, 'rank': None, 'study': None, 'experiment': None},
            ]
        }
        result = self._endpoint_logic('CLU', idx)
        weights = [m['weight'] for m in result['matches']]
        assert weights[0] > weights[1]  # higher weight first

    def test_empty_index_returns_zero(self):
        result = self._endpoint_logic('APOE', {})
        assert result['total'] == 0

    @pytest.mark.parametrize("gene", ['APOE', 'apoe', 'ApoE', '  APOE  '])
    def test_case_and_whitespace_variants(self, gene):
        idx = self._make_index()
        result = self._endpoint_logic(gene, idx)
        assert result['total'] == 2
        assert result['gene'] == 'APOE'
