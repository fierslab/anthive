"""Checks of DE tables deposited as ``de_table`` artefacts (doc/DESIGN_artefacts.md section 6.2, phase A3).

A ``de_table`` artefact holds one or more DE result tables (``.tsv``, ``.csv``, gzip of those), each with its
``<file>.mus`` sidecar (the ``de_*`` keys of the DE-only deposit route: ``de_format``, ``de_term``,
``de_contrast``, ``de_method``, ``de_study_id``, ``de_map_<column>``, ...). The watcher reads each table in
full (work-dir copy) and checks it against the server's DE presets (``lib/de_presets.py``) and the scope
dataset before the all-or-nothing import (``ingest_pipeline.import_de_all_or_nothing``):

* the sidecar: known ``de_format``, the keys its format needs (term, contrast, method; ``auto`` is missing),
  and that it names the scope dataset (``de_dataset_id`` or ``de_dataset_h5ad``);
* the columns: the preset's required columns resolve after the ``de_map_`` overrides (the import's rule);
  when they do not, the presets the columns would fit are named;
* rows: at least one; p-values and adjusted p-values in [0, 1]; log fold changes finite; adjusted p smaller
  than raw p in more than 1% of the rows (a warning);
* genes: at least 50% of the gene identifiers are genes of the dataset (error), below 90% a warning;
  duplicated genes within one contrast (warning);
* sign convention: a contrast must say its direction (``A_vs_B``); the convention recorded is "a positive
  log2 fold change is higher in A than in B". The table itself cannot prove the direction, so a contrast
  without ``vs`` is a warning, never a pass.

Input: the work-dir paths, the sidecar keys, the gene names of the dataset. Output: check entries
(``{id, status, summary, fix}``) and per-table facts for the manifest's ``de`` block.
"""
from __future__ import annotations

import csv
import gzip
import io
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

DE_TABLE_EXTS = ('.tsv', '.csv', '.tsv.gz', '.csv.gz')
MIN_ROWS = 1
GENE_MATCH_ERROR = 0.5
GENE_MATCH_WARN = 0.9
PADJ_BELOW_P_WARN = 0.01
MISSING = {'', 'na', 'nan', 'null', 'none'}
CONTRAST_DIRECTION_RE = re.compile(r'(?P<numerator>.+?)[ _.-]+vs\.?[ _.-]+(?P<denominator>.+)', re.IGNORECASE)
#: sidecar keys copied into the manifest's ``de`` block (never paths, ELN ids or S3 keys of the uploader)
DE_KEYS = ('de_format', 'de_term', 'de_contrast', 'de_method', 'de_study_id', 'de_description', 'de_covariates',
           'de_reference_type', 'de_software_version', 'de_dataset_id', 'de_dataset_h5ad', 'de_cluster_obs_column')
#: sidecar keys that point at the uploader's machine or systems (a warning: they are served with the sidecar)
LOCAL_KEYS = ('abspath', 'eln_experiment_id', 'eln_project_id', 'eln_study_id', 's3_key', 's3_url', 's3_bucket')


@dataclass
class TableResult:
    """What the checks learnt about one table."""
    file: str
    sidecar: str
    study_id: str
    meta: Dict[str, str]
    rows: int = 0
    genes_matched: Optional[float] = None
    sign_convention: Optional[str] = None
    checks: List[Dict[str, Any]] = field(default_factory=list)

    def manifest_entry(self) -> Dict[str, Any]:
        entry: Dict[str, Any] = {'file': self.file, 'sidecar': self.sidecar, 'study_id': self.study_id,
                                 'rows': self.rows, 'genes_matched': self.genes_matched,
                                 'sign_convention': self.sign_convention}
        for key in DE_KEYS:
            if self.meta.get(key):
                entry[key] = self.meta[key]
        return entry


def is_table(name: str) -> bool:
    return name.lower().endswith(DE_TABLE_EXTS)


def read_table(path: Path) -> Tuple[List[str], List[List[str]]]:
    """Header and rows of a DE table (tab for ``.tsv``, comma for ``.csv``; gzip by ending). An empty header
    cell is named ``column<i>``, as DuckDB's ``read_csv_auto`` (and so ``ant-de-ingest``) names it."""
    name = path.name.lower()
    raw = path.read_bytes()
    if name.endswith('.gz'):
        raw = gzip.decompress(raw)
    text = raw.decode('utf-8-sig')
    delimiter = '\t' if '.tsv' in name else ','
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    header = next(reader, None) or []
    columns = [column.strip() or f'column{index}' for index, column in enumerate(header)]
    rows = [row for row in reader if any(cell.strip() for cell in row)]
    return columns, rows


def resolve_columns(preset: Any, meta: Dict[str, str], columns: Sequence[str]) -> Dict[str, str]:
    """``anthive column -> source column`` after the preset's map and the ``de_map_<anthive>=<source>`` keys
    (an empty value drops the mapping), keeping only source columns that exist (``ant-de-ingest``'s rule)."""
    resolved: Dict[str, str] = {}                     # source -> anthive
    for source, anthive in preset.column_map.items():
        resolved[source] = anthive
    for key, source in meta.items():
        if not key.startswith('de_map_') or not key[len('de_map_'):]:
            continue
        anthive = key[len('de_map_'):]
        resolved = {s: a for s, a in resolved.items() if a != anthive}
        if source:
            resolved[source] = anthive
    present = set(columns)
    out: Dict[str, str] = {}
    for source, anthive in resolved.items():
        if source in present and anthive not in out:
            out[anthive] = source
    return out


def fitting_presets(presets: Dict[str, Any], columns: Sequence[str]) -> List[str]:
    """Presets whose required columns all resolve from ``columns`` without overrides."""
    return [name for name, preset in presets.items()
            if all(anthive in resolve_columns(preset, {}, columns) for anthive in preset.required_in_file)]


def _number(value: str) -> Optional[float]:
    text = value.strip()
    if text.lower() in MISSING:
        return None
    try:
        return float(text)
    except ValueError:
        return float('nan')


def sign_convention(contrast: Optional[str]) -> Optional[str]:
    """``positive log2fc = higher in A than in B`` for a contrast ``A_vs_B``; None when it states no direction."""
    match = CONTRAST_DIRECTION_RE.fullmatch((contrast or '').strip())
    if not match:
        return None
    return f'positive log2 fold change = higher in {match.group("numerator")} than in {match.group("denominator")}'


def check_table(path: Path, file_name: str, sidecar_name: str, meta: Dict[str, str], *, file_id: str,
                dataset_basename: str, db_id: str, genes: Optional[Set[str]], study_id: str) -> TableResult:
    """Every section 6.2 check of one table. Errors are ``fail`` entries; nothing raises for bad content."""
    from de_presets import PRESETS  # noqa: PLC0415
    from ingest_pipeline import de_sidecar_names, de_sidecar_problems  # noqa: PLC0415

    result = TableResult(file=file_name, sidecar=sidecar_name, study_id=study_id, meta=dict(meta))

    def add(check_id: str, status: str, summary: str, fix: str = '') -> None:
        result.checks.append({'id': check_id, 'status': status, 'summary': f'{file_name}: {summary}', 'fix': fix})
    problems = de_sidecar_problems(meta)
    if problems:
        add('de.sidecar', 'fail', '; '.join(problems), 'fill the de_* keys of the .mus (mus de check)')
    else:
        add('de.sidecar', 'pass', f'de_format {meta.get("de_format")} with the keys it needs')
    if not de_sidecar_names(meta, file_id, dataset_basename, db_id):
        add('de.dataset', 'fail', f'the sidecar names neither de_dataset_id={file_id} (or {db_id}) nor '
                                  f'de_dataset_h5ad={dataset_basename}.h5ad', 'name the scope dataset in the .mus')
    else:
        add('de.dataset', 'pass', f'names {db_id}')
    local = [key for key in LOCAL_KEYS if meta.get(key)]
    if local:
        add('de.sidecar_local_keys', 'warn', f'the sidecar holds {", ".join(local)} (paths or ids of your systems); '
                                             f'it is served with the table', 'remove these keys before depositing')
    try:
        columns, rows = read_table(path)
    except (OSError, UnicodeDecodeError, EOFError, csv.Error) as exc:
        add('de.read', 'fail', f'cannot be read as a table ({exc})')
        return result
    result.rows = len(rows)
    preset = PRESETS.get(str(meta.get('de_format') or ''))
    if preset is None:
        return result
    mapped = resolve_columns(preset, meta, columns)
    missing = [anthive for anthive in preset.required_in_file if anthive not in mapped]
    if missing:
        fits = fitting_presets(PRESETS, columns)
        add('de.columns', 'fail', f'columns for {", ".join(missing)} are missing for de_format '
                                  f'{preset.name} (the file has {", ".join(columns[:12])})',
            (f'the columns fit de_format {", ".join(fits)}' if fits else
             f'map them with de_map_<column>=<your column>, e.g. de_map_{missing[0]}=<column>'))
        return result
    add('de.columns', 'pass', f'required columns of {preset.name} present')
    if len(rows) < MIN_ROWS:
        add('de.rows', 'fail', 'the table has no rows')
        return result
    add('de.rows', 'pass', f'{len(rows)} rows')
    index = {column: position for position, column in enumerate(columns)}

    def column(anthive: str) -> List[str]:
        source = mapped.get(anthive)
        if source is None:
            return []
        position = index[source]
        return [row[position] if position < len(row) else '' for row in rows]
    out_of_range: Dict[str, int] = {}
    pvalues = [_number(value) for value in column('pvalue')]
    padjs = [_number(value) for value in column('padj')]
    for name, values in (('pvalue', pvalues), ('padj', padjs)):
        bad = sum(1 for value in values if value is not None and (math.isnan(value) or not 0 <= value <= 1))
        if bad:
            out_of_range[name] = bad
    if out_of_range:
        add('de.pvalues', 'fail', 'values outside [0, 1] (or not numbers): ' + ', '.join(
            f'{name} {count} row(s)' for name, count in out_of_range.items()), 'export raw p-values and FDR, not -log10')
    elif pvalues or padjs:
        add('de.pvalues', 'pass', 'p-values and adjusted p-values within [0, 1]')
    pairs = [(p, q) for p, q in zip(pvalues, padjs) if p is not None and q is not None]
    if pairs:
        below = sum(1 for p, q in pairs if q < p - 1e-12)
        if below / len(pairs) > PADJ_BELOW_P_WARN:
            add('de.padj_order', 'warn', f'the adjusted p-value is smaller than the raw p-value in {below} of '
                                         f'{len(pairs)} rows', 'check that padj and pvalue are not swapped')
    effect_name = 'log2fc' if 'log2fc' in mapped else ('estimate' if 'estimate' in mapped else None)
    if effect_name:
        effects = [_number(value) for value in column(effect_name)]
        bad = sum(1 for value in effects if value is not None and not math.isfinite(value))
        if bad:
            add('de.effects', 'fail', f'{effect_name} is not a finite number in {bad} row(s)')
        else:
            add('de.effects', 'pass', f'{effect_name} finite')
    gene_values = [value.strip() for value in column('gene')]
    if genes is not None and gene_values:
        distinct = {value for value in gene_values if value}
        matched = len(distinct & genes)
        fraction = matched / len(distinct) if distinct else 0.0
        if fraction < GENE_MATCH_ERROR:
            folded = {gene.lower() for gene in genes}
            fraction_folded = sum(1 for value in distinct if value.lower() in folded) / len(distinct) if distinct else 0
            add('de.genes', 'fail', f'{fraction:.0%} of {len(distinct)} gene identifiers are genes of {db_id}'
                + (f' ({fraction_folded:.0%} ignoring case)' if fraction_folded > fraction else ''),
                'use the gene names of the dataset (symbols, not Ensembl ids, or the other way round)')
        elif fraction < GENE_MATCH_WARN:
            add('de.genes', 'warn', f'{fraction:.0%} of {len(distinct)} gene identifiers are genes of {db_id}')
        else:
            add('de.genes', 'pass', f'{fraction:.0%} of the gene identifiers are genes of {db_id}')
        result.genes_matched = round(fraction, 4)
    elif genes is None:
        add('de.genes', 'warn', f'the genes of {db_id} could not be read; not compared')
    contrasts = column('contrast') or [str(meta.get('de_contrast') or '')] * len(gene_values)
    terms = column('term') or [''] * len(gene_values)
    seen: Set[Tuple[str, str, str]] = set()
    duplicates = 0
    for term, contrast, gene in zip(terms, contrasts, gene_values):
        key = (term, contrast, gene)
        if key in seen:
            duplicates += 1
        seen.add(key)
    if duplicates:
        add('de.duplicates', 'warn', f'{duplicates} duplicated gene(s) within one contrast')
    stated = [value for value in {contrast.strip() for contrast in contrasts} if value]
    conventions = {value: sign_convention(value) for value in stated}
    unclear = sorted(value for value, convention in conventions.items() if convention is None)
    if unclear:
        add('de.sign_convention', 'warn', f'the contrast {", ".join(unclear[:5])} does not say its direction',
            'name contrasts A_vs_B: a positive log2 fold change then reads "higher in A"')
    if len(stated) == 1 and conventions.get(stated[0]):
        result.sign_convention = conventions[stated[0]]
    elif stated and not unclear:
        result.sign_convention = 'positive log2 fold change = higher in the first group of each contrast (A_vs_B)'
    return result
