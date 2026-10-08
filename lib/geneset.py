"""
Geneset store: parsing, registry, validation.

Layout (mirrors h5adstore study folders):

    <store>/
      <Study>/
        index.md                 # OPTIONAL — citation only
        <Experiment>/
          index.md               # REQUIRED — organism, method, parent_dataset
          <name>.grp             # gene set, no header, # comments allowed
          <name>.rnk             # ranked list (gene, rank), no header
          <name>.md              # OPTIONAL — biological notes per geneset

A geneset's identifier is "<Study>/<Experiment>/<name>" where <name> is the
file stem of the .grp / .rnk file.

See genesetstore/CONVENTIONS.md for the format spec.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

try:
    import mus
    from celltype_groups import UNMAPPED, group_of
    from metadata import parse_md_file
except ImportError:  # when imported as `lib.geneset` from packages that set up paths
    from lib import mus  # type: ignore[no-redef]
    from lib.celltype_groups import UNMAPPED, group_of  # type: ignore[no-redef]
    from lib.metadata import parse_md_file  # type: ignore[no-redef]


METHOD_VALUES = {'WGCNA', 'cNMF', 'DGE', 'GSEA', 'manual', 'other'}
GENE_ID_TYPES = {'symbol', 'ensembl', 'entrez'}
DIRECTION_VALUES = {'up', 'down', 'bidirectional'}
GENESET_EXTENSIONS = {'.grp', '.rnk'}

# `celltypes: [all]` marks a catalog meant for datasets with every cell type mixed.
CELLTYPES_ALL = 'all'
# `role: markers` marks a QC marker panel: scored on all cells, never facet-tested.
ROLE_MARKERS = 'markers'
ROLE_VALUES = {ROLE_MARKERS}

# Separator used by ant-score when writing module-score columns:
#     <study>::<experiment>::<geneset_name>
# e.g. "Sierksma2025::WGCNA::Brown_Lysosome"
SCORE_COLUMN_SEPARATOR = '::'
SCORE_COLUMN_TEMPLATE  = '{study}::{experiment}::{name}'


def parse_score_column(column: str) -> Optional[str]:
    """Convert a module-score column name back into a geneset_id.

    "Sierksma2025::WGCNA::Brown_Lysosome" -> "Sierksma2025/WGCNA/Brown_Lysosome"

    Returns None if the column isn't a 3-part score column.
    """
    parts = column.split(SCORE_COLUMN_SEPARATOR)
    if len(parts) != 3:
        return None
    return '/'.join(parts)


def is_score_column(column: str) -> bool:
    """True if a column name follows the <study>::<experiment>::<name> shape."""
    return parse_score_column(column) is not None


class GenesetError(Exception):
    """Raised on parse or validation failures inside the geneset store."""


# ---------- low-level parsers ----------

def _iter_data_lines(path: Path) -> Iterator[Tuple[int, str]]:
    """
    Yield (1-based line number, stripped line) for each non-comment, non-blank
    line in the file. Lines starting with '#' (after leading whitespace) are
    treated as comments. Strict — no header tolerance.
    """
    with open(path, 'r', encoding='utf-8') as handle:
        for line_number, raw in enumerate(handle, start=1):
            stripped = raw.strip()
            if not stripped:
                continue
            if stripped.startswith('#'):
                continue
            yield line_number, stripped


def parse_grp(path: Path) -> List[str]:
    """
    Parse a .grp file into a list of gene symbols.

    - One token per line (whitespace-tolerant; a line with >1 token is rejected).
    - '#' comments and blank lines are skipped.
    - No header row allowed.
    - Duplicate genes are rejected.
    """
    genes: List[str] = []
    seen: Dict[str, int] = {}
    for line_number, line in _iter_data_lines(path):
        tokens = line.split()
        if len(tokens) != 1:
            raise GenesetError(
                f"{path}: line {line_number}: .grp expects exactly one token, "
                f"got {len(tokens)}: {line!r}"
            )
        gene = tokens[0]
        if gene in seen:
            raise GenesetError(
                f"{path}: line {line_number}: duplicate gene {gene!r} "
                f"(first seen on line {seen[gene]})"
            )
        seen[gene] = line_number
        genes.append(gene)
    return genes


def parse_rnk(path: Path) -> List[Tuple[str, float]]:
    """
    Parse a .rnk file into a list of (gene, rank) tuples.

    - Two tokens per line: gene, rank-as-float.
    - Whitespace separated (tab or space).
    - '#' comments and blank lines are skipped.
    - No header row allowed.
    - Duplicate genes are rejected.
    """
    rows: List[Tuple[str, float]] = []
    seen: Dict[str, int] = {}
    for line_number, line in _iter_data_lines(path):
        tokens = line.split()
        if len(tokens) != 2:
            raise GenesetError(
                f"{path}: line {line_number}: .rnk expects 2 tokens "
                f"(gene, rank), got {len(tokens)}: {line!r}"
            )
        gene, rank_text = tokens
        try:
            rank = float(rank_text)
        except ValueError:
            raise GenesetError(
                f"{path}: line {line_number}: rank value {rank_text!r} "
                f"is not numeric"
            )
        if gene in seen:
            raise GenesetError(
                f"{path}: line {line_number}: duplicate gene {gene!r} "
                f"(first seen on line {seen[gene]})"
            )
        seen[gene] = line_number
        rows.append((gene, rank))
    return rows


def _as_label_list(raw: Any) -> List[str]:
    """A ``celltypes`` value (list, or comma separated string) as a list of stripped labels."""
    if raw is None:
        return []
    if isinstance(raw, str):
        return [part.strip() for part in raw.split(',') if part.strip()]
    return [str(item).strip() for item in raw if str(item).strip()]


def celltypes_problems(labels: List[str]) -> List[str]:
    """Why a ``celltypes`` list is not acceptable (empty list when it is).

    A list is acceptable when it is exactly ``['all']`` or holds only labels of
    the controlled ``cell_type`` vocabulary (lib/_mus_vocab.yaml) that map to a
    cell type group of lib/celltype_groups.yaml (otherwise a module could never
    be scored or tested anywhere).
    """
    if not labels:
        return ["'celltypes' is empty: list Cell Ontology labels or the single value 'all'"]
    lowered = [label.lower() for label in labels]
    if CELLTYPES_ALL in lowered:
        if len(labels) > 1:
            return [f"'celltypes' mixes 'all' with {[l for l in labels if l.lower() != CELLTYPES_ALL]}"]
        return [] if labels == [CELLTYPES_ALL] else ["'celltypes' value must be spelled 'all' (lower case)"]
    problems: List[str] = []
    for label in labels:
        if not mus.validate_value('cell_type', label):
            problems.append(f"celltypes label {label!r} is not in the cell_type vocabulary (lib/_mus_vocab.yaml)")
        elif group_of(label) == UNMAPPED:
            problems.append(f"celltypes label {label!r} maps to no cell type group (lib/celltype_groups.yaml)")
    return problems


def role_problems(value: Any) -> List[str]:
    """Why a ``role`` value is not acceptable (empty list when absent or valid)."""
    if value is None or value == '':
        return []
    if str(value).strip() not in ROLE_VALUES:
        return [f"role {value!r} is not one of {sorted(ROLE_VALUES)}"]
    return []


# ---------- dataclasses ----------

@dataclass
class Experiment:
    """A geneset-store experiment (one method run on one parent dataset)."""
    study: str
    name: str                        # experiment folder name
    path: Path                       # absolute path to the experiment folder
    metadata: Dict[str, Any]         # parsed from index.md frontmatter
    body: str                        # markdown body of index.md

    @property
    def experiment_id(self) -> str:
        return f"{self.study}/{self.name}"

    @property
    def organism(self) -> str:
        return self.metadata.get('organism', '')

    @property
    def method(self) -> str:
        return self.metadata.get('method', '')

    @property
    def parent_dataset(self) -> Optional[str]:
        return self.metadata.get('parent_dataset')

    @property
    def gene_id_type(self) -> str:
        return self.metadata.get('gene_id_type', 'symbol')

    @property
    def celltypes(self) -> List[str]:
        """Cell type(s) this experiment's genesets are relevant to.

        REQUIRED in every experiment ``index.md`` (the registry scan rejects an
        experiment without it): Cell Ontology labels of the same vocabulary as
        `.h5ad.mus` `cell_type=` (lib/_mus_vocab.yaml, cell_type), or the single
        value ``all``. The property returns ``[]`` only for an experiment that
        failed that check. Consumers (ant-score, ant-score-ext, ant-ucell,
        ant-facet-test-modules) read the resolved scope through
        ``lib/celltype_scope.py``.
        """
        return _as_label_list(self.metadata.get('celltypes') or self.metadata.get('cell_type'))

    @property
    def role(self) -> Optional[str]:
        """``markers`` for a QC marker panel (scored on all cells, never facet-tested), else None."""
        value = self.metadata.get('role')
        return str(value).strip() if value else None


@dataclass
class Geneset:
    """A single .grp or .rnk file (with optional sibling .md)."""
    experiment: Experiment
    name: str                        # file stem
    path: Path                       # absolute path to .grp / .rnk
    format: str                      # 'grp' | 'rnk'
    md_path: Optional[Path] = None   # optional sibling .md
    metadata: Dict[str, Any] = field(default_factory=dict)
    body: str = ''
    _genes: Optional[List[str]] = field(default=None, repr=False)
    _ranks: Optional[List[Tuple[str, float]]] = field(default=None, repr=False)

    @property
    def geneset_id(self) -> str:
        return f"{self.experiment.experiment_id}/{self.name}"

    @property
    def organism(self) -> str:
        return self.experiment.organism

    @property
    def celltypes(self) -> List[str]:
        """Cell types of this geneset: its own ``.md`` ``celltypes`` when set, else the experiment's."""
        own = self.metadata.get('celltypes')
        return _as_label_list(own) if own else self.experiment.celltypes

    @property
    def role(self) -> Optional[str]:
        """Role of this geneset: its own ``.md`` ``role`` when set, else the experiment's."""
        own = self.metadata.get('role')
        return str(own).strip() if own else self.experiment.role

    def load(self) -> None:
        """Parse the .grp or .rnk file. Idempotent."""
        if self.format == 'grp':
            if self._genes is None:
                self._genes = parse_grp(self.path)
        else:
            if self._ranks is None:
                self._ranks = parse_rnk(self.path)

    @property
    def genes(self) -> List[str]:
        self.load()
        if self.format == 'grp':
            return list(self._genes)  # type: ignore[arg-type]
        return [gene for gene, _ in self._ranks]  # type: ignore[union-attr]

    @property
    def ranks(self) -> Optional[List[Tuple[str, float]]]:
        if self.format != 'rnk':
            return None
        self.load()
        return list(self._ranks)  # type: ignore[arg-type]

    @property
    def n_genes(self) -> int:
        return len(self.genes)

    def to_summary(self) -> Dict[str, Any]:
        """Lightweight dict for API listing (no gene list)."""
        display_name = self.metadata.get('name', self.name)
        description = self.metadata.get('description', '')
        return {
            'geneset_id': self.geneset_id,
            'study': self.experiment.study,
            'experiment': self.experiment.name,
            'name': self.name,
            'display_name': display_name,
            'description': description,
            'format': self.format,
            'n_genes': self.n_genes,
            'organism': self.organism,
            'method': self.experiment.method,
            'parent_dataset': self.experiment.parent_dataset,
            'celltypes': self.celltypes,
            'role': self.role,
            'direction': self.metadata.get('direction'),
        }

    def to_detail(self) -> Dict[str, Any]:
        """Full dict including genes and merged metadata."""
        summary = self.to_summary()
        summary.update({
            'genes': self.genes,
            'ranks': self.ranks,
            'experiment_metadata': self.experiment.metadata,
            'experiment_body': self.experiment.body,
            'geneset_metadata': self.metadata,
            'geneset_body': self.body,
        })
        return summary


# ---------- registry ----------

class GenesetRegistry:
    """
    Walks a geneset store directory and indexes every experiment + geneset.

    Usage:
        registry = GenesetRegistry(Path('data/genesetstore'))
        registry.scan()
        for geneset in registry.genesets:
            print(geneset.geneset_id)
    """

    def __init__(self, root: Path):
        self.root: Path = root
        self.experiments: Dict[str, Experiment] = {}      # experiment_id -> Experiment
        self.genesets: Dict[str, Geneset] = {}            # geneset_id -> Geneset
        self.studies: Dict[str, Dict[str, Any]] = {}      # study -> {metadata, body}
        self.errors: List[str] = []
        self.warnings: List[str] = []

    # -------- scanning --------

    def scan(self) -> None:
        """Walk the store and populate experiments/genesets."""
        self.experiments.clear()
        self.genesets.clear()
        self.studies.clear()
        self.errors.clear()
        self.warnings.clear()

        if not self.root.exists():
            return

        for study_dir in sorted(self.root.iterdir()):
            if not study_dir.is_dir():
                continue
            if study_dir.name.startswith(('.', '_')):
                continue
            self._scan_study(study_dir)

    def _scan_study(self, study_dir: Path) -> None:
        study = study_dir.name
        study_index = study_dir / 'index.md'
        if study_index.exists():
            try:
                metadata, body = parse_md_file(study_index)
            except Exception as error:
                self.errors.append(f"{study_index}: failed to parse: {error}")
                metadata, body = {}, ''
            self.studies[study] = {'metadata': metadata, 'body': body}

        for experiment_dir in sorted(study_dir.iterdir()):
            if not experiment_dir.is_dir():
                continue
            if experiment_dir.name.startswith(('.', '_')):
                continue
            self._scan_experiment(study, experiment_dir)

    def _scan_experiment(self, study: str, experiment_dir: Path) -> None:
        experiment_name = experiment_dir.name
        index_path = experiment_dir / 'index.md'
        if not index_path.exists():
            self.errors.append(
                f"{experiment_dir}: missing index.md (required at experiment level)"
            )
            return

        try:
            metadata, body = parse_md_file(index_path)
        except Exception as error:
            self.errors.append(f"{index_path}: failed to parse: {error}")
            return

        if not metadata.get('organism'):
            self.errors.append(f"{index_path}: missing required field 'organism'")
            return
        if not metadata.get('method'):
            self.errors.append(f"{index_path}: missing required field 'method'")
            return
        for problem in self._experiment_problems(metadata):
            self.errors.append(f"{index_path}: {problem}")
            return

        experiment = Experiment(
            study=study,
            name=experiment_name,
            path=experiment_dir,
            metadata=metadata,
            body=body,
        )
        self.experiments[experiment.experiment_id] = experiment

        # Enforce flat structure: an experiment folder must not contain subdirs
        for child in sorted(experiment_dir.iterdir()):
            if child.is_dir() and not child.name.startswith(('.', '_')):
                self.errors.append(
                    f"{child}: experiment folder must be flat — no nested "
                    f"directories allowed (Study/Experiment/<file> only)"
                )

        # Enforce single-format experiments: all .grp OR all .rnk
        formats_present = {
            child.suffix.lstrip('.')
            for child in experiment_dir.iterdir()
            if child.is_file() and child.suffix in GENESET_EXTENSIONS
        }
        if len(formats_present) > 1:
            self.errors.append(
                f"{experiment_dir}: mixed geneset formats not allowed in one "
                f"experiment (found: {sorted(formats_present)}). Split into "
                f"separate experiments."
            )
            return

        for child in sorted(experiment_dir.iterdir()):
            if not child.is_file():
                continue
            if child.suffix not in GENESET_EXTENSIONS:
                continue
            self._scan_geneset(experiment, child)

    @staticmethod
    def _experiment_problems(metadata: Dict[str, Any]) -> List[str]:
        """Problems of the cell type scope (``celltypes`` required, ``role`` optional) of an experiment index."""
        raw = metadata.get('celltypes', metadata.get('cell_type'))
        if raw is None:
            return ["missing required field 'celltypes' (Cell Ontology labels, or 'all')"]
        return celltypes_problems(_as_label_list(raw)) + role_problems(metadata.get('role'))

    @staticmethod
    def _geneset_problems(metadata: Dict[str, Any]) -> List[str]:
        """Problems of the optional ``celltypes`` / ``role`` override of a geneset ``.md``."""
        problems = role_problems(metadata.get('role'))
        if metadata.get('celltypes'):
            problems += celltypes_problems(_as_label_list(metadata['celltypes']))
        return problems

    def _scan_geneset(self, experiment: Experiment, file_path: Path) -> None:
        format_name = file_path.suffix.lstrip('.')
        name = file_path.stem
        md_path = file_path.with_suffix('.md')

        metadata: Dict[str, Any] = {}
        body = ''
        if md_path.exists():
            try:
                metadata, body = parse_md_file(md_path)
            except Exception as error:
                self.errors.append(f"{md_path}: failed to parse: {error}")
                return
            for problem in self._geneset_problems(metadata):
                self.errors.append(f"{md_path}: {problem}")
                return
            md_format = metadata.get('format')
            if md_format and md_format != format_name:
                self.errors.append(
                    f"{md_path}: format field {md_format!r} does not match "
                    f"file extension .{format_name}"
                )
                return
        else:
            md_path = None

        geneset = Geneset(
            experiment=experiment,
            name=name,
            path=file_path,
            format=format_name,
            md_path=md_path,
            metadata=metadata,
            body=body,
        )

        try:
            geneset.load()
        except GenesetError as error:
            self.errors.append(str(error))
            return

        self.genesets[geneset.geneset_id] = geneset

    # -------- lookups --------

    def get(self, geneset_id: str) -> Optional[Geneset]:
        return self.genesets.get(geneset_id)

    def get_experiment(self, experiment_id: str) -> Optional[Experiment]:
        return self.experiments.get(experiment_id)

    def list_genesets(
        self,
        organism: Optional[str] = None,
        method: Optional[str] = None,
        study: Optional[str] = None,
    ) -> List[Geneset]:
        result: List[Geneset] = []
        for geneset in self.genesets.values():
            if organism and geneset.organism != organism:
                continue
            if method and geneset.experiment.method != method:
                continue
            if study and geneset.experiment.study != study:
                continue
            result.append(geneset)
        return sorted(result, key=lambda g: g.geneset_id)

    def list_experiments(self, study: Optional[str] = None) -> List[Experiment]:
        result = list(self.experiments.values())
        if study:
            result = [e for e in result if e.study == study]
        return sorted(result, key=lambda e: e.experiment_id)


# ---------- validation ----------

def validate_registry(
    registry: GenesetRegistry,
    h5ad_db_ids: Optional[set] = None,
) -> Tuple[List[str], List[str]]:
    """
    Run additional validation on a scanned registry.

    Returns (errors, warnings). The registry's own errors/warnings are
    included.

    h5ad_db_ids: optional set of valid db_ids to check parent_dataset against.
    """
    errors = list(registry.errors)
    warnings = list(registry.warnings)

    for experiment in registry.experiments.values():
        method = experiment.method
        if method not in METHOD_VALUES:
            warnings.append(
                f"{experiment.path}/index.md: method {method!r} is not in "
                f"{sorted(METHOD_VALUES)}"
            )

        gene_id_type = experiment.gene_id_type
        if gene_id_type != 'symbol':
            warnings.append(
                f"{experiment.path}/index.md: gene_id_type is {gene_id_type!r} "
                f"(only 'symbol' is fully supported)"
            )
        if gene_id_type not in GENE_ID_TYPES:
            warnings.append(
                f"{experiment.path}/index.md: gene_id_type {gene_id_type!r} "
                f"is not in {sorted(GENE_ID_TYPES)}"
            )

        if h5ad_db_ids is not None and experiment.parent_dataset:
            if experiment.parent_dataset not in h5ad_db_ids:
                warnings.append(
                    f"{experiment.path}/index.md: parent_dataset "
                    f"{experiment.parent_dataset!r} not found in h5adstore"
                )

    for geneset in registry.genesets.values():
        direction = geneset.metadata.get('direction')
        if direction is not None and direction not in DIRECTION_VALUES:
            warnings.append(
                f"{geneset.md_path}: direction {direction!r} is not in "
                f"{sorted(DIRECTION_VALUES)}"
            )

    return errors, warnings


# ---------- writers ----------

def write_grp(path: Path, genes: List[str], header_comment: Optional[str] = None) -> None:
    """Write a .grp file. header_comment is rendered as '# ...' lines."""
    with open(path, 'w', encoding='utf-8') as handle:
        if header_comment:
            for line in header_comment.splitlines():
                handle.write(f"# {line}\n")
        for gene in genes:
            handle.write(f"{gene}\n")


def write_rnk(path: Path, ranks: List[Tuple[str, float]],
              header_comment: Optional[str] = None) -> None:
    """Write a .rnk file. Tab separated."""
    with open(path, 'w', encoding='utf-8') as handle:
        if header_comment:
            for line in header_comment.splitlines():
                handle.write(f"# {line}\n")
        for gene, rank in ranks:
            handle.write(f"{gene}\t{rank}\n")
