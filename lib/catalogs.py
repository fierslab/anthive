"""Read-side model for MODULE CATALOGS across the anthive stack.

Two catalog families:

- **geneset** — plain gene lists (one .grp file per module). Loaded via
  `lib.geneset.GenesetRegistry`. Modules have no per-gene weights.
- **starcat** — starCAT reference catalogs. Each module is a **weighted
  spectrum** across ~2 700 genes. Weights come from the reference TSV
  (rows = modules, columns = genes). Score-derivation recipes come from
  the catalog's YAML.

Public API
----------
* :class:`Module` — one program / geneset. Uniformly `.name`, `.group`,
  `.description`, `.top_genes(n)`, `.weights()` (or ``None`` for genesets).
* :class:`Catalog` — collection of modules + optional score recipes.
* :func:`registry` — cached loader that returns all catalogs, keyed by
  `(source, catalog_id)`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import yaml


# Where the starCAT reference lives on disk. Override via env / config in
# the future; for now this matches the deployed layout.
DEFAULT_STARCATSTORE_ROOT = Path('/data/1/project/starcatstore')


# ── Module / Catalog dataclasses ────────────────────────────────────────

@dataclass
class ScoreRecipe:
    """One derived score (composite or binary call) built on top of a
    catalog's programs.

    Composite (`kind='continuous'`): `value = sum(w_i * usage_i)`.
    Binary (`kind='discrete'`): `value = (weighted_sum > threshold)`.
    """
    name: str
    kind: str                          # 'continuous' | 'discrete' | 'multinomial'
    columns: List[str] = field(default_factory=list)
    weights: List[float] = field(default_factory=list)
    threshold: Optional[float] = None  # only for discrete
    normalization: str = 'normalized'
    file: Optional[str] = None         # for multinomial: helper script

    def to_dict(self) -> dict:
        return {
            'name': self.name,
            'kind': self.kind,
            'columns': list(self.columns),
            'weights': list(self.weights),
            'threshold': self.threshold,
            'normalization': self.normalization,
            'file': self.file,
        }


@dataclass
class Module:
    """One program in a catalog — weighted (starcat) or plain (geneset)."""
    name: str
    catalog_id: str                     # e.g. 'Sierksma2025/WGCNA' or 'MICROGLIA_V1'
    source: str                         # 'geneset' | 'starcat'
    group: str = ''                     # e.g. 'identity', 'states', ...
    description: str = ''
    n_genes: int = 0
    # Weights: dict[gene → weight] for starcat; None for geneset.
    # Loaded lazily via ``.weights()``.
    _weights: Optional[Dict[str, float]] = field(default=None, repr=False)
    _weights_loader: Optional[object] = field(default=None, repr=False)
    # Plain gene list (any catalog). For starcat this is the full gene
    # set; for geneset it's the .grp contents.
    _genes: Optional[List[str]] = field(default=None, repr=False)
    _genes_loader: Optional[object] = field(default=None, repr=False)

    def weights(self) -> Optional[Dict[str, float]]:
        """Full {gene: weight} map. None if this catalog has no weights."""
        if self._weights is not None:
            return self._weights
        if self._weights_loader is not None:
            self._weights = self._weights_loader()
            return self._weights
        return None

    def genes(self) -> List[str]:
        if self._genes is not None:
            return self._genes
        if self._genes_loader is not None:
            self._genes = self._genes_loader()
            return self._genes
        w = self.weights()
        return list(w.keys()) if w else []

    def top_genes(self, n: int = 30) -> List[Tuple[str, Optional[float]]]:
        """Top-N genes by absolute weight (starcat) or first-N (geneset)."""
        w = self.weights()
        if w is not None:
            ranked = sorted(w.items(), key=lambda kv: -abs(kv[1]))
            return [(g, wgt) for g, wgt in ranked[:n]]
        return [(g, None) for g in self.genes()[:n]]

    def to_summary(self) -> dict:
        return {
            'name': self.name,
            'catalog_id': self.catalog_id,
            'source': self.source,
            'group': self.group,
            'description': self.description,
            'n_genes': self.n_genes,
            'has_weights': self._weights_loader is not None,
        }


@dataclass
class Catalog:
    """A named collection of modules ± score recipes."""
    catalog_id: str
    source: str
    name: str                           # display name
    description: str = ''
    modules: List[Module] = field(default_factory=list)
    scores: List[ScoreRecipe] = field(default_factory=list)
    n_genes: int = 0                    # universe size across all modules
    metadata: dict = field(default_factory=dict)

    def get_module(self, name: str) -> Optional[Module]:
        return next((m for m in self.modules if m.name == name), None)

    def to_summary(self) -> dict:
        return {
            'catalog_id': self.catalog_id,
            'source': self.source,
            'name': self.name,
            'description': self.description,
            'n_modules': len(self.modules),
            'n_scores': len(self.scores),
            'n_genes': self.n_genes,
            'metadata': self.metadata,
        }


# ── starCAT reader ──────────────────────────────────────────────────────

# starCAT program groupings — hardcoded because the naming.tsv doesn't
# explicitly declare them. Matches doc/ANNOTATE_CELLS.md § 4.
_STARCAT_GROUPS = {
    'Microglia_Homeostatic': 'identity',
    'Microglia_Homeostatic_MERTK': 'identity',
    'Microglia_DAM': 'states',
    'Microglia_Lipid_LAM': 'states',
    'Microglia_IRM': 'states',
    'Microglia_HLA_MHCII': 'states',
    'Microglia_Cycling': 'states',
    'Microglia_UPR_Activated': 'activation',
    'Microglia_Cytokine_Acute': 'activation',
    'Microglia_IEG_Stress': 'activation',
    'Microglia_HIF_EarlyMetabolic': 'activation',
    'Microglia_HeatShock': 'activation',
    'Microglia_Stress_FKBP5': 'activation',
    'Microglia_Translation': 'activation',
    'Microglia_Border_CAM': 'cns_macrophages',
    'Microglia_Monocyte_Derived': 'cns_macrophages',
    'Microglia_DC_cDC1': 'dendritic_cells',
    'Microglia_DC_cDC2': 'dendritic_cells',
    'Microglia_DC_pDC': 'dendritic_cells',
    'Microglia_DC_Mature': 'dendritic_cells',
    'Microglia_WholeCell_Baseline': 'technology_baseline',
}


def _read_starcat_naming(path: Path) -> Dict[str, str]:
    """Return {program_name: rationale_string}."""
    if not path.exists():
        return {}
    out: Dict[str, str] = {}
    with open(path) as fh:
        header = fh.readline().rstrip('\n').split('\t')
        cols = {c: i for i, c in enumerate(header)}
        for line in fh:
            parts = line.rstrip('\n').split('\t')
            if len(parts) < len(header):
                continue
            out[parts[cols['name']]] = parts[cols.get('rationale', 2)]
    return out


def _read_starcat_tsv_header(path: Path) -> Tuple[List[str], List[str]]:
    """Return (program_names, gene_names) from a starCAT reference TSV.

    Only reads headers + first column — cheap. Full weight matrix is
    lazy-loaded per module via ``_load_starcat_weights``.
    """
    with open(path) as fh:
        header = fh.readline().rstrip('\n').split('\t')
        gene_names = header[1:]
        program_names = []
        for line in fh:
            if not line.strip():
                continue
            program_names.append(line.split('\t', 1)[0])
    return program_names, gene_names


def _load_starcat_weights(path: Path, program_name: str) -> Dict[str, float]:
    """Read one row of the starCAT TSV and return {gene: weight}."""
    with open(path) as fh:
        header = fh.readline().rstrip('\n').split('\t')
        gene_names = header[1:]
        for line in fh:
            parts = line.rstrip('\n').split('\t')
            if parts[0] != program_name:
                continue
            weights = [float(w) for w in parts[1:]]
            return {g: w for g, w in zip(gene_names, weights) if w != 0}
    raise KeyError(program_name)


def _load_starcat_scores(scores_yaml: Path) -> List[ScoreRecipe]:
    """Parse the starCAT scores YAML into ScoreRecipe records."""
    if not scores_yaml.exists():
        return []
    with open(scores_yaml) as fh:
        blob = yaml.safe_load(fh) or {}
    scores_block = blob.get('scores') or {}
    out: List[ScoreRecipe] = []
    for entry in scores_block.get('continuous') or []:
        cols = list(entry.get('columns') or [])
        weights = entry.get('weights')
        if weights is None:
            weights = [1.0] * len(cols)
        out.append(ScoreRecipe(
            name=entry['name'], kind='continuous',
            columns=cols, weights=list(weights),
            normalization=entry.get('normalization', 'normalized'),
        ))
    for entry in scores_block.get('discrete') or []:
        kind = 'multinomial' if 'file' in entry else 'discrete'
        out.append(ScoreRecipe(
            name=entry['name'], kind=kind,
            columns=list(entry.get('columns') or []),
            threshold=entry.get('threshold'),
            normalization=entry.get('normalization', 'normalized'),
            file=entry.get('file'),
        ))
    return out


def _load_starcat_catalog(root: Path, folder: str) -> Optional[Catalog]:
    """Load one starCAT catalog folder (e.g. `microglia`)."""
    catalog_dir = root / folder / 'catalog'
    if not catalog_dir.exists():
        return None
    # Find the ref TSV — the .tsv without .naming / .top_genes suffix.
    tsvs = [p for p in catalog_dir.glob('*.tsv')
            if not p.name.endswith('.naming.tsv')
            and not p.name.endswith('.top_genes.tsv')]
    if not tsvs:
        return None
    ref_tsv = sorted(tsvs)[0]
    catalog_stem = ref_tsv.stem            # e.g. 'MICROGLIA.V1.0'
    catalog_id = catalog_stem.replace('.', '_')

    naming_tsv  = catalog_dir / f'{catalog_stem}.naming.tsv'
    scores_yaml = catalog_dir / f'{catalog_stem}.scores.yaml'

    rationales = _read_starcat_naming(naming_tsv)
    program_names, gene_names = _read_starcat_tsv_header(ref_tsv)
    modules: List[Module] = []
    for prog in program_names:
        mod = Module(
            name=prog,
            catalog_id=catalog_id,
            source='starcat',
            group=_STARCAT_GROUPS.get(prog, ''),
            description=rationales.get(prog, ''),
            n_genes=len(gene_names),
        )
        # Lazy loader closure — reads that one row on demand.
        mod._weights_loader = (lambda p=ref_tsv, name=prog:
                               _load_starcat_weights(p, name))
        modules.append(mod)

    return Catalog(
        catalog_id=catalog_id,
        source='starcat',
        name=catalog_stem,
        description=f'starCAT reference catalog from {folder}',
        modules=modules,
        scores=_load_starcat_scores(scores_yaml),
        n_genes=len(gene_names),
        metadata={
            'ref_tsv': str(ref_tsv),
            'organism': 'human',        # MICROGLIA.V1.0 is human-specific
            'folder': folder,
        },
    )


def _load_geneset_catalogs(geneset_registry) -> List[Catalog]:
    """Bridge lib.geneset experiments → Catalog records.

    Each geneset Experiment (e.g. Sierksma2025/WGCNA) is exposed here as
    one Catalog with the study's genesets as its modules.
    """
    catalogs: List[Catalog] = []
    for experiment in geneset_registry.list_experiments():
        modules: List[Module] = []
        experiment_id = experiment.experiment_id
        for gs in geneset_registry.list_genesets(
                study=experiment.study,
                method=experiment.method,
        ):
            if gs.experiment.experiment_id != experiment_id:
                continue
            mod = Module(
                name=gs.name,
                catalog_id=experiment_id,
                source='geneset',
                description=gs.metadata.get('description', ''),
                n_genes=gs.n_genes,
            )
            mod._genes_loader = (lambda g=gs: list(g.genes))
            modules.append(mod)
        if not modules:
            continue
        catalogs.append(Catalog(
            catalog_id=experiment_id,
            source='geneset',
            name=experiment_id.replace('/', ' / '),
            description=(experiment.metadata.get('name', '')
                         or experiment.body.strip().split('\n', 1)[0].strip()),
            modules=modules,
            n_genes=sum(m.n_genes for m in modules),
            metadata={
                'organism': experiment.organism,
                'method': experiment.method,
                'parent_dataset': experiment.parent_dataset,
                'study': experiment.study,
            },
        ))
    return catalogs


# ── Public registry ─────────────────────────────────────────────────────

@lru_cache(maxsize=1)
def _cached_starcat_catalogs(root_str: str) -> List[Catalog]:
    root = Path(root_str)
    out: List[Catalog] = []
    if not root.exists():
        return out
    for entry in sorted(root.iterdir()):
        if not entry.is_dir():
            continue
        cat = _load_starcat_catalog(root, entry.name)
        if cat is not None:
            out.append(cat)
    return out


def build_registry(
    geneset_registry,
    starcatstore_root: Path = DEFAULT_STARCATSTORE_ROOT,
) -> Dict[Tuple[str, str], Catalog]:
    """Return the flat map {(source, catalog_id): Catalog}.

    Cached loaders keep the starCAT parse cheap; geneset side is a
    delegation to lib.geneset which the caller already maintains.
    """
    result: Dict[Tuple[str, str], Catalog] = {}
    for cat in _cached_starcat_catalogs(str(starcatstore_root)):
        result[(cat.source, cat.catalog_id)] = cat
    for cat in _load_geneset_catalogs(geneset_registry):
        result[(cat.source, cat.catalog_id)] = cat
    return result
