"""DE-ingestion presets.

A preset is a small declarative record describing how a particular DE
tool's output file maps onto the anthive ``__de`` schema. Presets only
cover the **column mapping**; the semantic decisions (study_id, term,
method, target) stay user-supplied.

Adding a new preset = adding a ``Preset`` instance to ``PRESETS`` below.
See ``doc/DESIGN_var_and_de.md`` for the design rationale.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass(frozen=True)
class Preset:
    """A DE-ingestion preset.

    Attributes:
        name:
            Slug used on the CLI (``--preset deseq2``).
        description:
            One-line summary surfaced by ``--list-presets``.
        shape:
            Either ``'one-file-one-contrast'`` (DESeq2, edgeR) — caller
            must supply ``--term`` and ``--contrast`` because the file
            has no such column — or ``'one-file-many-contrasts'``
            (scanpy `rank_genes_groups_df`, long-format CSV), where the
            contrast (and possibly the term) come from a column.
        column_map:
            ``source_name → anthive_name`` for the columns this preset
            understands.  Anthive names are the columns of ``__de``:
            ``gene``, ``log2fc``, ``zeff``, ``pvalue``, ``padj``,
            ``estimate``, ``SE``, ``ci_low``, ``ci_high``, optionally
            ``term``, ``contrast``.
        required_in_file:
            Anthive names that MUST resolve to a present column in the
            input file.  Missing columns produce a friendly error.
        required_cli_flags:
            CLI flags the user MUST pass for this preset.  Examples:
            DESeq2 needs ``--term`` and ``--contrast`` because one file
            is one contrast; scanpy doesn't (the file has a ``group``
            column).
        default_method:
            Default value for ``--method``; used only as the hint in
            the error message when ``--method`` is omitted.
        default_reference_type:
            Default ``reference_type`` for new studies created via this
            preset; can be overridden with ``--reference-type``.
    """

    name: str
    description: str
    shape: str   # 'one-file-one-contrast' | 'one-file-many-contrasts'
    column_map: Dict[str, str]
    required_in_file: List[str] = field(default_factory=list)
    required_cli_flags: List[str] = field(default_factory=list)
    default_method: Optional[str] = None
    default_reference_type: Optional[str] = None


# ── presets ───────────────────────────────────────────────────────────────────

PRESETS: Dict[str, Preset] = {

    'deseq2': Preset(
        name='deseq2',
        description=(
            'DESeq2 / pydeseq2 results() output (CSV per contrast). One file '
            '= one contrast; --term and --contrast are required.'
        ),
        shape='one-file-one-contrast',
        column_map={
            # DESeq2 column → anthive column
            'gene':            'gene',
            'log2FoldChange':  'log2fc',
            'pvalue':          'pvalue',
            'padj':            'padj',
            'baseMean':        'base_mean',   # goes into stats_extra
            'lfcSE':           'SE',
            'stat':            'score',       # goes into stats_extra
        },
        required_in_file=['gene', 'log2fc', 'pvalue', 'padj'],
        required_cli_flags=['--term', '--contrast', '--method'],
        default_method='deseq2',
        default_reference_type='vsreference',
    ),

    'edger': Preset(
        name='edger',
        description=(
            "edgeR topTags()$table output (CSV per contrast). One file "
            '= one contrast; --term and --contrast are required.'
        ),
        shape='one-file-one-contrast',
        column_map={
            'gene':     'gene',
            'logFC':    'log2fc',
            'PValue':   'pvalue',
            'FDR':      'padj',
            'logCPM':   'base_mean',   # nearest analog; stored in stats_extra
            'F':        'score',
            'LR':       'score',
        },
        required_in_file=['gene', 'log2fc', 'pvalue', 'padj'],
        required_cli_flags=['--term', '--contrast', '--method'],
        default_method='edger',
        default_reference_type='vsreference',
    ),

    'scanpy': Preset(
        name='scanpy',
        description=(
            "scanpy.get.rank_genes_groups_df() output. One file = many "
            'contrasts (the file has a "group" column); --term is required, '
            '--contrast comes from the file.'
        ),
        shape='one-file-many-contrasts',
        column_map={
            'names':         'gene',
            'group':         'contrast',
            'logfoldchanges':'log2fc',
            'pvals':         'pvalue',
            'pvals_adj':     'padj',
            'scores':        'score',
        },
        required_in_file=['gene', 'contrast', 'log2fc', 'pvalue', 'padj'],
        required_cli_flags=['--term', '--method'],
        default_method='wilcoxon',
        default_reference_type='vsreference',
    ),

    'long': Preset(
        name='long',
        description=(
            'Arbitrary long-format CSV with explicit gene/term/contrast '
            'columns. All column names supplied via --map; this preset has '
            'no built-in defaults beyond identity (gene→gene, log2fc→log2fc, '
            'etc).'
        ),
        shape='one-file-many-contrasts',
        column_map={
            'gene':     'gene',
            'term':     'term',
            'contrast': 'contrast',
            'log2fc':   'log2fc',
            'zeff':     'zeff',
            'pvalue':   'pvalue',
            'padj':     'padj',
            'estimate': 'estimate',
            'SE':       'SE',
            'ci_low':   'ci_low',
            'ci_high':  'ci_high',
        },
        required_in_file=['gene', 'term', 'contrast'],
        required_cli_flags=['--method'],
        default_method=None,
        default_reference_type=None,
    ),
}


# Canonical anthive columns for __de (the names you map TO).  Anything
# outside this set goes into stats_extra (JSON) at ingest.
ANTHIVE_DE_COLUMNS = {
    'gene', 'term', 'contrast',
    'log2fc', 'zeff', 'pvalue', 'padj',
    'estimate', 'SE', 'ci_low', 'ci_high',
}

# Columns that travel into __de's stats_extra JSON if a preset maps to them.
STATS_EXTRA_COLUMNS = {'base_mean', 'score', 'pct_in', 'pct_out'}


def format_preset_help(preset: Preset) -> str:
    """Detailed help string for `--help-preset <name>`."""
    lines = [
        f"Preset: {preset.name}",
        f"  {preset.description}",
        "",
        f"  Shape:     {preset.shape}",
    ]
    if preset.default_method:
        lines.append(f"  Default --method:    {preset.default_method!r}")
    if preset.default_reference_type:
        lines.append(f"  Default --reference-type: {preset.default_reference_type!r}")
    lines.append("")
    lines.append("  Column mapping (source-file → anthive __de column):")
    for src, dst in preset.column_map.items():
        lines.append(f"    {src:20s} →  {dst}")
    lines.append("")
    lines.append("  Required columns in the input file:")
    lines.append("    " + ", ".join(preset.required_in_file))
    if preset.required_cli_flags:
        lines.append("")
        lines.append("  Required CLI flags for this preset:")
        lines.append("    " + ", ".join(preset.required_cli_flags))
    return "\n".join(lines)


def format_preset_list() -> str:
    """Short list of all presets for ``--list-presets``."""
    lines = ["Available DE-ingestion presets:", ""]
    for preset in PRESETS.values():
        lines.append(f"  {preset.name:8s}  {preset.description}")
    lines.append("")
    lines.append("Run `ant-de-ingest --help-preset <name>` for details.")
    return "\n".join(lines)


def suggest_preset_for_columns(columns: List[str]) -> List[str]:
    """Given a list of source columns, return the presets whose
    ``required_in_file`` set can be satisfied (after applying the
    preset's source→anthive map).  Used in the "you forgot --preset"
    error to give the user concrete options.
    """
    columns_set = {c for c in columns}
    suggestions = []
    for preset in PRESETS.values():
        # Reverse the column_map to look up what source-file column would feed
        # each anthive column for this preset.
        inv = {v: k for k, v in preset.column_map.items()}
        ok = all(
            inv.get(anthive, anthive) in columns_set
            for anthive in preset.required_in_file
        )
        if ok:
            suggestions.append(preset.name)
    return suggestions
