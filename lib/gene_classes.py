"""Gene-class flags for the facet result views.

Genes that are over-represented among facet hits for technical or locus reasons are flagged so a reader can weigh
them: mitochondrial, ribosomal, lncRNA or clone-named, sex-chromosome, long (span above 500 kb) and APOE-locus genes.
Names are classified by pattern (human and mouse). Sex chromosome and span need a GENCODE gene annotation
(``gene_annotation_gtf`` in ``anthive.yaml``, or ``ANTHIVE_GENE_GTF``; GENCODE v41 hg38 on the dev host); without it only
the name based flags exist. Mouse genes have no span annotation here.

The GTF is read once per process (gene lines only, about a second) and cached in memory by file size and mtime.

Two more flags depend on where a result is shown and are added by ``context_flags``: ``foreign_lineage_marker`` (the
gene is a marker of a neuronal, glial or vascular panel and the cell type group is myeloid) and ``no_mouse_ortholog``
(a xenograft result for a human gene without a mouse ortholog). Both are informational; they never change an r-value.
"""
from __future__ import annotations

import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Dict, FrozenSet, Iterable, List, Optional, Tuple

LONG_GENE_BP = 500_000
APOE_LOCUS = frozenset({'APOE', 'APOC1', 'APOC1P1', 'TOMM40', 'NECTIN2', 'PVRL2'})
#: Dev-host GENCODE v41 file, used only when neither the environment nor the config names one.
DEV_HOST_GTF = Path('/data/1/project/borzoi_daan/hg38/genes/gencode41/gencode41_basic_nort.gtf')

_MT = re.compile(r'^(MT-|mt-)')
_RIBOSOMAL = re.compile(r'^(RP[LS]\d+[A-Z]?(P\d+)?|Rp[ls]\d+[a-z]?(-ps\d+)?|MRP[LS]\d+|Mrp[ls]\d+)$')
_CLONE = re.compile(r'^(A[CLPFJ]\d{6}\.\d+|C[TU]\d{6}\.\d+|Z\d{5}\.\d+|BX\d{6}\.\d+|FP\d{6}\.\d+|LINC\d+|MIR\d+HG|'
                    r'.+-(AS|IT|OT)\d+|Gm\d+|.+Rik\d?|LOC\d+|Mir\d+[a-z]?hg)$')

_lock = threading.Lock()
_cache: Dict[str, Dict[str, Tuple[str, int]]] = {}


_PATH_TTL_SECONDS = 10.0
_path_memo: Optional[Tuple[float, Tuple[Optional[str], Optional[str]], Optional[Path]]] = None


def gtf_path() -> Optional[Path]:
    """The configured GENCODE GTF, else the dev host file when it exists, else None.

    The answer is remembered for a few seconds (per environment setting): the config file is parsed on
    every lookup otherwise, and a gene card asks once per gene.
    """
    global _path_memo
    environment = (os.environ.get('ANTHIVE_GENE_GTF'), os.environ.get('ANTHIVE_CONFIG'))
    memo = _path_memo
    now = time.monotonic()
    if memo is not None and memo[1] == environment and now - memo[0] < _PATH_TTL_SECONDS:
        return memo[2]
    path = _gtf_path_uncached()
    _path_memo = (now, environment, path)
    return path


def _gtf_path_uncached() -> Optional[Path]:
    explicit = os.environ.get('ANTHIVE_GENE_GTF')
    if not explicit:
        try:
            from config import get_config  # noqa: PLC0415
            explicit = (get_config() or {}).get('gene_annotation_gtf')
        except Exception:
            explicit = None
    if explicit:
        path = Path(str(explicit))
        return path if path.exists() else None
    return DEV_HOST_GTF if DEV_HOST_GTF.exists() else None


def _load(path: Path) -> Dict[str, Tuple[str, int]]:
    """``SYMBOL -> (chromosome, span in bp)`` of every gene line of a GTF (the longest span wins on duplicates)."""
    key = f'{path}:{path.stat().st_size}:{path.stat().st_mtime_ns}'
    with _lock:
        if key in _cache:
            return _cache[key]
    genes: Dict[str, Tuple[str, int]] = {}
    pattern = re.compile(r'gene_name "([^"]+)"')
    with open(path, 'rb') as handle:
        for raw in handle:
            if b'\tgene\t' not in raw:
                continue
            fields = raw.decode('ascii', 'replace').split('\t')
            if len(fields) < 9 or fields[2] != 'gene':
                continue
            match = pattern.search(fields[8])
            if match:
                symbol = match.group(1).upper()
                span = int(fields[4]) - int(fields[3]) + 1
                if symbol not in genes or span > genes[symbol][1]:
                    genes[symbol] = (fields[0], span)
    with _lock:
        _cache.clear()
        _cache[key] = genes
    return genes


def flags(symbol: str, annotation: Optional[Dict[str, Tuple[str, int]]] = None) -> List[str]:
    """Class flags of one gene symbol: ``mitochondrial``, ``ribosomal``, ``lncrna_or_clone``, ``sex_chromosome``,
    ``long_gene`` (span above 500 kb), ``apoe_locus``.

    Args:
        symbol: Gene symbol (human or mouse).
        annotation: ``SYMBOL -> (chromosome, span)``; default the configured GENCODE file when available.
    """
    out: List[str] = []
    if _MT.match(symbol):
        out.append('mitochondrial')
    if _RIBOSOMAL.match(symbol):
        out.append('ribosomal')
    if _CLONE.match(symbol):
        out.append('lncrna_or_clone')
    if annotation is None:
        path = gtf_path()
        try:
            annotation = _load(path) if path else {}
        except OSError:
            annotation = {}
    entry = annotation.get(symbol.upper()) if annotation else None
    if entry is not None:
        if entry[0] in ('chrX', 'chrY'):
            out.append('sex_chromosome')
        if entry[1] > LONG_GENE_BP:
            out.append('long_gene')
    if symbol.upper() in APOE_LOCUS:
        out.append('apoe_locus')
    return out


# ── context flags: depend on the cell type group and the facet scope (informational, never change an r-value) ──

FLAG_FOREIGN_LINEAGE = 'foreign_lineage_marker'
FLAG_NO_MOUSE_ORTHOLOG = 'no_mouse_ortholog'
#: Cell type groups in which a neuronal, glial or vascular marker is out of place (the myeloid lineage).
MYELOID_GROUPS = frozenset({'microglia', 'perivascular macrophage', 'macrophage (other)', 'myeloid (mixed)'})
#: Facet scope whose human cells sit in a mouse brain (a human gene without a mouse ortholog is not protected by a
#: combined human and mouse reference: reads of that region cannot be assigned to the mouse copy).
XENOGRAFT_SCOPE = 'xenograft_human_in_mouse'
ORTHOLOG_FILE = Path('_orthologs') / 'mouse_human_orthologs.tsv'
_MYELOID_PANEL = re.compile(r'micro|macroph|monocyt|myeloid', re.I)
_ortholog_cache: Dict[str, Tuple[Tuple[float, int], FrozenSet[str]]] = {}


def foreign_lineage_symbols(genesets: Iterable[Any]) -> FrozenSet[str]:
    """Upper-case symbols of the marker panels of lineages other than microglia, for the foreign-lineage flag.

    Args:
        genesets: Geneset-store entries (``role``, ``name``, ``genes``); the ``role: markers`` ones are the panels
            (PanglaoDB Neuron, Glia and Vasculature markers, the LuChen NBB cell type markers). Panels of the myeloid
            lineage (name matches micro, macroph, monocyt, myeloid) are not foreign, and a gene that is also in one of
            them is not flagged.
    """
    foreign: set = set()
    own: set = set()
    for geneset in genesets:
        if getattr(geneset, 'role', None) != 'markers':
            continue
        symbols = {str(gene).upper() for gene in geneset.genes}
        (own if _MYELOID_PANEL.search(str(geneset.name)) else foreign).update(symbols)
    return frozenset(foreign - own)


def human_orthologs(path: Path) -> Optional[FrozenSet[str]]:
    """Upper-case human symbols that have a mouse ortholog in the MGI homology table (``mouse<TAB>human``).

    The table is ``<genesetstore>/_orthologs/mouse_human_orthologs.tsv`` (MGI HOM_MouseHumanSequence). Matching is by
    symbol, so a gene renamed since the MGI release reads as without an ortholog; the flag is a prompt to look, not a
    verdict. Cached by file mtime and size; None when the file is missing or unreadable (the flag is then not raised).
    """
    try:
        stat = path.stat()
    except OSError:
        return None
    key = str(path)
    stamp = (stat.st_mtime, stat.st_size)
    with _lock:
        cached = _ortholog_cache.get(key)
        if cached is not None and cached[0] == stamp:
            return cached[1]
    humans: set = set()
    try:
        with open(path, encoding='utf-8') as handle:
            next(handle, None)
            for line in handle:
                parts = line.rstrip('\n').split('\t')
                if len(parts) >= 2 and parts[1]:
                    humans.add(parts[1].upper())
    except OSError:
        return None
    result = frozenset(humans)
    with _lock:
        _ortholog_cache[key] = (stamp, result)
    return result


#: A foreign-lineage flag needs the gene to be rare in microglia (median percent of cells expressing, below this) ...
FOREIGN_MAX_MICROGLIA_PCT = 10.0
#: ... and at least this many times more often detected in a non-myeloid cell type group of the same scope.
FOREIGN_MIN_FOLD = 5.0
#: Detection of the microglia is floored here before the fold is taken (a gene at 0 percent needs 2.5 percent elsewhere).
FOREIGN_PCT_FLOOR = 0.5
MICROGLIA_GROUP = 'microglia'


def foreign_by_detection(group_pcts: Optional[Dict[str, float]]) -> bool:
    """Whether a gene is out of place in microglia judged by how often it is detected.

    Args:
        group_pcts: Median percent of cells expressing the gene per cell type group in one facet scope (fleet gene
            statistics, ``gene_detection.group_pcts``); None or without microglia means unknown, never flagged.

    Returns:
        True when the gene is detected in fewer than ``FOREIGN_MAX_MICROGLIA_PCT`` percent of microglia and at least
        ``FOREIGN_MIN_FOLD`` times more often (microglia floored at ``FOREIGN_PCT_FLOOR``) in some non-myeloid group.
        A gene at or above 10 percent in microglia (broadly expressed genes, state genes such as PTPRG) is never flagged.
    """
    if not group_pcts or MICROGLIA_GROUP not in group_pcts:
        return False
    microglia = group_pcts[MICROGLIA_GROUP]
    if microglia >= FOREIGN_MAX_MICROGLIA_PCT:
        return False
    others = [value for name, value in group_pcts.items() if name not in MYELOID_GROUPS and name != 'unmapped']
    return bool(others) and max(others) >= FOREIGN_MIN_FOLD * max(microglia, FOREIGN_PCT_FLOOR)


def context_flags(symbol: str, group: Optional[str], scope: Optional[str],
                  foreign: Optional[FrozenSet[str]] = None, orthologs: Optional[FrozenSet[str]] = None,
                  group_pcts: Optional[Dict[str, float]] = None) -> List[str]:
    """Flags that depend on where a result is shown (informational: they never change an r-value).

    ``foreign_lineage_marker``: the gene is in a neuronal, glial or vascular marker panel, the result is for a myeloid
    cell type group (``MYELOID_GROUPS``) AND the gene is rare in microglia and much more often detected elsewhere
    (``foreign_by_detection`` over ``group_pcts``): in microglia such a gene points at ambient RNA, doublets or, in
    xenografts, cross-species read misassignment. Panel membership alone is not enough. ``no_mouse_ortholog``: a
    xenograft result for a human gene with no mouse ortholog (None for ``foreign`` / ``orthologs`` means the source is
    unavailable: the flag is not raised).
    """
    out: List[str] = []
    upper = symbol.upper()
    if foreign and group in MYELOID_GROUPS and upper in foreign and foreign_by_detection(group_pcts):
        out.append(FLAG_FOREIGN_LINEAGE)
    if orthologs and scope == XENOGRAFT_SCOPE and upper not in orthologs:
        out.append(FLAG_NO_MOUSE_ORTHOLOG)
    return out
