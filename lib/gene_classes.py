"""Gene-class flags for the facet result views.

Genes that are over-represented among facet hits for technical or locus reasons are flagged so a reader can weigh
them: mitochondrial, ribosomal, lncRNA or clone-named, sex-chromosome, long (span above 500 kb) and APOE-locus genes.
Names are classified by pattern (human and mouse). Sex chromosome and span need a GENCODE gene annotation
(``gene_annotation_gtf`` in ``anthive.yaml``, or ``ANTHIVE_GENE_GTF``; GENCODE v41 hg38 on the dev host); without it only
the name based flags exist. Mouse genes have no span annotation here.

The GTF is read once per process (gene lines only, about a second) and cached in memory by file size and mtime.
"""
from __future__ import annotations

import os
import re
import threading
from pathlib import Path
from typing import Dict, List, Optional, Tuple

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


def gtf_path() -> Optional[Path]:
    """The configured GENCODE GTF, else the dev host file when it exists, else None."""
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
