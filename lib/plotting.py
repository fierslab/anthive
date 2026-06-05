"""
Plotting functions for Anthive.

Each function accepts pandas DataFrames and renders with matplotlib/seaborn,
mirroring the Streamlit visualisation pages. Common parameters width, height,
dpi, and output_format control output size and format (png | svg | pdf).

New unified API: plot_scatter, plot_distribution, plot_grouped, plot_heatmap.
Legacy functions (plot_umap, plot_gene, plot_cat, plot_num) remain unchanged.
"""

import io
from typing import Optional, Tuple

import numpy as np
import matplotlib
matplotlib.use('Agg')  # non-interactive backend — must precede pyplot import
import matplotlib.pyplot as plt
from matplotlib.figure import Figure as MplFigure
import seaborn as sns
import pandas as pd


# Three palette families.  The right one is picked from the data kind:
#
#   categorical  — discrete labels (cell types, clusters, samples)
#   sequential   — strictly ordered, single-direction continuous
#                  (counts, gene expression, mt-fraction, percent-X, …)
#   divergent    — continuous values that straddle a meaningful zero
#                  (z-scores, log fold change, signed module scores)
#
# Defaults — used when the caller doesn't override:
_DEFAULT_CATEGORICAL = 'tab10'
_DEFAULT_SEQUENTIAL  = 'inferno_r'
_DEFAULT_DIVERGENT   = 'RdBu_r'

# Legacy alias kept for older callers — points at the categorical default.
_DEFAULT_PALETTE = _DEFAULT_CATEGORICAL

_VALID_FORMATS = {'png', 'svg', 'pdf'}

# Qualitative palettes: never use these as a continuous cmap (matplotlib
# accepts them but cycles them into an unreadable scale).  Discrete colours
# only.
_QUALITATIVE_PALETTES = {
    'tab10', 'tab20', 'tab20b', 'tab20c',
    'Set1', 'Set2', 'Set3',
    'Paired', 'Accent', 'Dark2',
    'Pastel1', 'Pastel2',
}

# Divergent palettes: red↔blue, brown↔green, etc. — only meaningful when
# data spans zero (or some other neutral midpoint).  Don't use these for
# sequential data — the midpoint colour becomes uninformative.
_DIVERGENT_PALETTES = {
    'RdBu', 'RdBu_r',
    'coolwarm',
    'RdYlBu', 'RdYlBu_r',
    'PiYG', 'PiYG_r',
    'PuOr', 'PuOr_r',
    'BrBG', 'BrBG_r',
    'RdGy', 'RdGy_r',
    'bwr', 'seismic', 'Spectral', 'Spectral_r',
}


# Back-compat shim — used to be the only "is continuous?" helper.
_CONTINUOUS_DEFAULT = _DEFAULT_SEQUENTIAL


def _fmt_n(n: int) -> str:
    """Pretty cell-count: 234567 → '235k', 1_300_000 → '1.3M'."""
    if n < 1_000:
        return str(n)
    if n < 100_000:
        return f"{n/1_000:.1f}k"
    if n < 1_000_000:
        return f"{int(round(n/1_000))}k"
    return f"{n/1_000_000:.1f}M"


def _compose_title(bits: list, extra: Optional[str] = None) -> str:
    """Compose a tight title: "<main> · <bit> · <bit> · <extra>"."""
    pieces = [str(b) for b in bits if b]
    if extra:
        pieces.append(str(extra))
    if not pieces:
        return ''
    return pieces[0] + (' · ' + ' · '.join(pieces[1:]) if len(pieces) > 1 else '')


def _looks_divergent(values) -> bool:
    """Heuristic: data straddles zero with comparable magnitudes on each side
    → it's meaningfully divergent (z-scores, log fold change, signed module
    scores).  A field where 99% of values are positive isn't divergent even
    if a few negatives slipped in."""
    arr = np.asarray(values, dtype=float)
    finite = arr[np.isfinite(arr)]
    if finite.size < 10:
        return False
    vmin = float(finite.min())
    vmax = float(finite.max())
    if not (vmin < 0 < vmax):
        return False
    # require both arms to be at least ~10% of the dominant arm
    return min(abs(vmin), abs(vmax)) / max(abs(vmin), abs(vmax)) > 0.1


def pick_cmap(
    kind: str,
    palette_cat: str = _DEFAULT_CATEGORICAL,
    palette_cont: str = _DEFAULT_SEQUENTIAL,
    palette_div: str = _DEFAULT_DIVERGENT,
    values=None,
) -> Tuple[str, str]:
    """Return ``(cmap_name, used_kind)`` for the given data kind.

    Args:
        kind:  ``'categorical'`` | ``'continuous'`` | ``'gene'``.
               ``'gene'`` is treated like ``'continuous'`` for cmap purposes.
        palette_cat / palette_cont / palette_div: per-family palettes.
        values: numeric array used to auto-decide sequential vs divergent
                inside the continuous branch.  Pass ``None`` for "force
                sequential".

    Returns:
        ``(cmap_name, 'categorical' | 'sequential' | 'divergent')``.

    Defensive fallbacks:
        - a qualitative palette name passed for a continuous family is
          silently replaced with the default sequential cmap.
        - a divergent palette passed when the data is one-sided is replaced
          with the sequential default.
        - a sequential palette passed when the data is divergent is replaced
          with the divergent default (so the user always sees a coherent
          axis).
    """
    if kind == 'categorical':
        if palette_cat in _QUALITATIVE_PALETTES:
            return palette_cat, 'categorical'
        return _DEFAULT_CATEGORICAL, 'categorical'

    # 'continuous' or 'gene'
    if values is not None and _looks_divergent(values):
        if palette_div in _DIVERGENT_PALETTES:
            return palette_div, 'divergent'
        return _DEFAULT_DIVERGENT, 'divergent'

    if palette_cont in _QUALITATIVE_PALETTES:
        return _DEFAULT_SEQUENTIAL, 'sequential'
    if palette_cont in _DIVERGENT_PALETTES:
        # one-sided data → divergent cmap wastes range; fall back
        return _DEFAULT_SEQUENTIAL, 'sequential'
    return palette_cont, 'sequential'


# Legacy single-palette helper kept for any old callsite I missed.
def _continuous_cmap(palette: str) -> str:
    """Old single-palette helper.  Prefer pick_cmap() for new code."""
    if not palette or palette in _QUALITATIVE_PALETTES:
        return _DEFAULT_SEQUENTIAL
    return palette


def _legend_outside(ax, title: Optional[str] = None, fontsize: float = 8) -> None:
    """Move an existing matplotlib legend to outside-top-right of the axes.

    No-op when the axes has no legend (e.g. no hue mapping).  Used by every
    plot geom so legend placement is uniform across the site.  bbox_inches
    is set to 'tight' on savefig so the outside legend is not clipped.
    """
    legend = ax.get_legend()
    if legend is None:
        return
    new_title = title if title is not None else (
        legend.get_title().get_text() if legend.get_title() else None
    )
    ax.legend(
        bbox_to_anchor=(1.02, 1.0), loc='upper left',
        borderaxespad=0.0,
        fontsize=fontsize,
        title=new_title,
        title_fontsize=fontsize + 1,
        frameon=False,
    )


def _setup_style(
    font_family: Optional[str] = None,
    font_size: Optional[float] = None,
) -> None:
    """Configure matplotlib + seaborn style. Per-call so concurrent renders
    with different font settings don't clobber each other (matplotlib's
    rcParams is process-global; this is best-effort but each render fully
    overrides at the start)."""
    sns.set_theme(style='ticks', font_scale=0.9)
    rc = {}
    if font_family:
        rc['font.family'] = font_family
    if font_size is not None:
        rc['font.size'] = font_size
        rc['axes.titlesize'] = font_size * 1.15
        rc['axes.labelsize'] = font_size
        rc['xtick.labelsize'] = font_size * 0.85
        rc['ytick.labelsize'] = font_size * 0.85
        rc['legend.fontsize'] = font_size * 0.85
        rc['legend.title_fontsize'] = font_size * 0.9
    if rc:
        plt.rcParams.update(rc)


def _render_figure(
    fig: MplFigure,
    dpi: int,
    output_format: str = 'png',
    caption: Optional[str] = None,
    source_url: Optional[str] = None,
    font_family: Optional[str] = None,
    font_size: Optional[float] = None,
) -> bytes:
    """Render figure to bytes in the requested format and close it.

    Provenance embedding:
      - PDF: caption + source_url rendered as small grey footer lines.
      - PNG: caption + source_url written as tEXt chunks via matplotlib's
        `metadata` kwarg (`Title`, `Description`, `Source`). Survives save-as.
      - SVG: same metadata keys → XML `<metadata>` element. Also survives.

    For PNG/SVG, the caption is ALSO returned via the `X-Plot-Caption` /
    `X-Source-URL` HTTP headers so HTTP clients see provenance without
    parsing image metadata.
    """
    if output_format not in _VALID_FORMATS:
        output_format = 'png'

    # PDF: render caption + source URL as visible footer text.
    if output_format == 'pdf':
        footer_lines = []
        if caption:
            footer_lines.append(caption)
        if source_url:
            footer_lines.append(source_url)
        for i, line in enumerate(reversed(footer_lines)):
            fig.text(
                0.5, 0.005 + i * 0.012, line,
                ha='center', va='bottom', fontsize=6,
                color='#555555' if i == len(footer_lines) - 1 else '#888888',
                wrap=True,
            )

    # Structured metadata via matplotlib savefig — different backends accept
    # different key sets, so we build per-format dicts:
    #   PNG: tolerant (accepts any string key → tEXt chunk)
    #   SVG: Dublin Core only (Title, Creator, Subject, Description, Date,
    #        Type, Format, Identifier, Source, Relation, Coverage, Rights,
    #        Language, Keywords, Publisher, Contributor)
    #   PDF: Title, Author, Subject, Keywords, Producer, Creator,
    #        CreationDate, ModDate, Trapped
    # We use the common-denominator keys (Title, Description, Source,
    # Creator) and tag PNG with an extra Software key.
    metadata: dict = {}
    if output_format == 'png':
        if caption:
            metadata['Title'] = caption[:200]
            metadata['Description'] = caption
        if source_url:
            metadata['Source'] = source_url
        metadata['Software'] = 'anthive ant-serve / matplotlib'
    elif output_format == 'svg':
        if caption:
            metadata['Title'] = caption[:200]
            metadata['Description'] = caption
        if source_url:
            metadata['Source'] = source_url
        metadata['Creator'] = 'anthive ant-serve / matplotlib'
    elif output_format == 'pdf':
        if caption:
            metadata['Title'] = caption[:200]
            metadata['Subject'] = caption
        # PDF doesn't have a Source key — fold the URL into Keywords so
        # the URL is still searchable in the file's metadata.
        if source_url:
            metadata['Keywords'] = source_url
        metadata['Creator'] = 'anthive ant-serve / matplotlib'

    buf = io.BytesIO()
    save_kwargs = {'format': output_format, 'dpi': dpi, 'bbox_inches': 'tight'}
    if metadata:
        save_kwargs['metadata'] = metadata
    fig.savefig(buf, **save_kwargs)
    plt.close(fig)
    buf.seek(0)
    return buf.getvalue()


def _render_png(fig: MplFigure, dpi: int) -> bytes:
    """Render figure to PNG bytes and close it immediately (legacy wrapper)."""
    return _render_figure(fig, dpi, 'png')


# ── UMAP ──────────────────────────────────────────────────────────────────────

def plot_umap(
    embedding_df: pd.DataFrame,
    color_df: Optional[pd.DataFrame],
    color_field: Optional[str],
    color_type: str = 'categorical',
    width: float = 8,
    height: float = 6,
    dpi: int = 150,
    point_size: float = 3,
    alpha: float = 0.8,
    palette: str = _DEFAULT_PALETTE,
    title: Optional[str] = None,
) -> bytes:
    """UMAP scatter plot, optionally colored by gene / category / numerical.

    Args:
        embedding_df: DataFrame with columns [cell_name, dim_0, dim_1].
        color_df:     DataFrame with [cell_name, <color_field>] or None.
        color_field:  Name of the column to use for colouring.
        color_type:   'categorical', 'numerical', or 'gene'.
        width, height: Figure size in inches.
        dpi:          Output resolution.
        point_size:   Scatter point size (s=).
        alpha:        Point transparency (0–1).
        palette:      Seaborn palette name for categorical data.
        title:        Override plot title.
    """
    _setup_style()
    fig, ax = plt.subplots(figsize=(width, height))

    # The embedding columns are always the last two (after cell_name)
    x_col = embedding_df.columns[-2]
    y_col = embedding_df.columns[-1]

    plot_df = embedding_df.copy()

    if color_df is not None and color_field:
        color_values = color_df.set_index('cell_name')[color_field]
        plot_df[color_field] = color_values.reindex(plot_df['cell_name'].values).values

        scatter_kwargs: dict = dict(
            data=plot_df,
            x=x_col,
            y=y_col,
            hue=color_field,
            linewidth=0,
            s=point_size,
            alpha=alpha,
            ax=ax,
        )
        if color_type == 'categorical':
            scatter_kwargs['palette'] = palette

        sns.scatterplot(**scatter_kwargs)

        if ax.get_legend() and color_type == 'categorical':
            n_cats = int(plot_df[color_field].nunique())
            legend_fontsize = 6 if n_cats > 12 else 8
            ax.legend(
                bbox_to_anchor=(1.02, 1.0), loc='upper left',
                borderaxespad=0.0,
                fontsize=legend_fontsize, markerscale=2,
                title=color_field, title_fontsize=legend_fontsize + 1,
                frameon=False,
            )
    else:
        sns.scatterplot(
            data=plot_df,
            x=x_col,
            y=y_col,
            linewidth=0,
            s=point_size,
            alpha=alpha,
            color='steelblue',
            ax=ax,
        )

    ax.set_xlabel(str(x_col).replace('dim_', 'Dim '))
    ax.set_ylabel(str(y_col).replace('dim_', 'Dim '))
    ax.set_title(title or (color_field or 'UMAP'))
    sns.despine(ax=ax)

    return _render_png(fig, dpi)


# ── Gene expression distribution ──────────────────────────────────────────────

def plot_gene(
    data_df: pd.DataFrame,
    gene_field: str,
    plot_type: str = 'ecdf',
    bins: int = 20,
    width: float = 7,
    height: float = 4,
    dpi: int = 150,
    title: Optional[str] = None,
) -> bytes:
    """Expression distribution for one gene: ecdf, kde, or histogram.

    Args:
        data_df:    DataFrame with at least [cell_name, <gene_field>].
        gene_field: Gene (column) name.
        plot_type:  'ecdf' | 'kde' | 'histogram'.
        bins:       Number of histogram bins (ignored for ecdf/kde).
        width, height: Figure size in inches.
        dpi:        Output resolution.
        title:      Override plot title.
    """
    _setup_style()
    fig, ax = plt.subplots(figsize=(width, height))

    if plot_type == 'ecdf':
        sns.ecdfplot(data=data_df, x=gene_field, ax=ax)
    elif plot_type == 'kde':
        sns.kdeplot(data=data_df, x=gene_field, ax=ax)
    else:  # histogram
        sns.histplot(data=data_df, x=gene_field, bins=bins, ax=ax)

    ax.set_xlabel(gene_field)
    ax.set_title(title or f'{gene_field} ({plot_type})')
    sns.despine(ax=ax)

    return _render_png(fig, dpi)


# ── Categorical breakdown ──────────────────────────────────────────────────────

def plot_cat(
    data_df: pd.DataFrame,
    cat_field: str,
    y_field: Optional[str] = None,
    y_type: str = 'count',
    plot_type: str = 'bar',
    width: float = 8,
    height: float = 5,
    dpi: int = 150,
    title: Optional[str] = None,
) -> bytes:
    """Values grouped by a categorical field.

    Args:
        data_df:    DataFrame with [cell_name, cat_field, (y_field)].
        cat_field:  Categorical column to group by.
        y_field:    Gene or numerical column for Y axis (None → cell counts).
        y_type:     'count' | 'gene' | 'numerical'.
        plot_type:  'bar' | 'box' | 'violin'.
        width, height: Figure size in inches.
        dpi:        Output resolution.
        title:      Override plot title.
    """
    _setup_style()
    fig, ax = plt.subplots(figsize=(width, height))

    cat_order = data_df[cat_field].value_counts().index.tolist()

    if y_type == 'count':
        counts = data_df[cat_field].value_counts().reindex(cat_order)
        sns.barplot(x=counts.index, y=counts.values, ax=ax)
        ax.set_ylabel('Cell count')
        auto_title = f'Cells per {cat_field}'
    elif plot_type == 'box':
        sns.boxplot(data=data_df, x=cat_field, y=y_field,
                    order=cat_order, ax=ax, showfliers=False)
        auto_title = f'{y_field} by {cat_field} (box)'
    elif plot_type == 'violin':
        sns.violinplot(data=data_df, x=cat_field, y=y_field,
                       order=cat_order, ax=ax)
        auto_title = f'{y_field} by {cat_field} (violin)'
    else:  # bar
        sns.barplot(data=data_df, x=cat_field, y=y_field,
                    order=cat_order, ax=ax)
        auto_title = f'{y_field} by {cat_field}'

    ax.set_xlabel(cat_field)
    plt.setp(ax.get_xticklabels(), rotation=45, ha='right')
    ax.set_title(title or auto_title)
    sns.despine(ax=ax)
    fig.tight_layout()

    return _render_png(fig, dpi)


# ── Numerical field distribution ───────────────────────────────────────────────

def plot_num(
    data_df: pd.DataFrame,
    num_field: str,
    cat_field: Optional[str] = None,
    plot_type: str = 'ecdf',
    bins: int = 20,
    width: float = 7,
    height: float = 4,
    dpi: int = 150,
    title: Optional[str] = None,
) -> bytes:
    """Numerical metadata field distribution, optionally grouped by category.

    Without cat_field: ecdf, kde, or histogram.
    With cat_field:    box, violin, or bar grouped by category.

    Args:
        data_df:    DataFrame with [cell_name, num_field, (cat_field)].
        num_field:  Numerical column name.
        cat_field:  Optional groupby categorical column.
        plot_type:  'ecdf' | 'kde' | 'histogram' | 'box' | 'violin' | 'bar'.
        bins:       Histogram bins (default 20).
        width, height: Figure size in inches.
        dpi:        Output resolution.
        title:      Override plot title.
    """
    _setup_style()
    fig, ax = plt.subplots(figsize=(width, height))

    if cat_field:
        cat_order = data_df[cat_field].value_counts().index.tolist()
        if plot_type == 'box':
            sns.boxplot(data=data_df, x=cat_field, y=num_field,
                        order=cat_order, ax=ax, showfliers=False)
        elif plot_type == 'violin':
            sns.violinplot(data=data_df, x=cat_field, y=num_field,
                           order=cat_order, ax=ax)
        else:  # bar
            sns.barplot(data=data_df, x=cat_field, y=num_field,
                        order=cat_order, ax=ax)
        ax.set_xlabel(cat_field)
        plt.setp(ax.get_xticklabels(), rotation=45, ha='right')
        auto_title = f'{num_field} by {cat_field}'
    else:
        if plot_type == 'ecdf':
            sns.ecdfplot(data=data_df, x=num_field, ax=ax)
        elif plot_type == 'kde':
            sns.kdeplot(data=data_df, x=num_field, ax=ax)
        else:  # histogram
            sns.histplot(data=data_df, x=num_field, bins=bins, ax=ax)
        ax.set_xlabel(num_field)
        auto_title = f'{num_field} ({plot_type})'

    ax.set_title(title or auto_title)
    sns.despine(ax=ax)
    if cat_field:
        fig.tight_layout()

    return _render_png(fig, dpi)


# ── Unified grammar-of-graphics API ───────────────────────────────────────────

def build_caption(
    db_id: str,
    geom: str,
    x: Optional[str],
    x_kind: Optional[str],
    y: Optional[str],
    color: Optional[str],
    layer: str = 'X',
    n_cells: Optional[int] = None,
    n_groups: Optional[int] = None,
    sample: Optional[int] = None,
    subset: Optional[str] = None,
    ci: Optional[bool] = None,
) -> str:
    """Build a single-paragraph figure caption.

    The result is meant to be shown verbatim under the rendered image — it
    should answer: what kind of plot, on what dataset, of what, grouped how,
    over how many cells/groups, drawn from which layer, with which
    statistical decoration.  Optional context fields are appended only when
    given so callers can stay terse for legacy paths.
    """
    plural = lambda n: 's' if n != 1 else ''  # noqa: E731

    # Sentence 1 — what the plot is.
    if geom == 'scatter':
        if x and x_kind == 'embedding':
            sentence = f'UMAP embedding of {db_id} ({x})'
        else:
            sentence = f'Scatter plot of {db_id}'
        if color:
            sentence += f', cells colored by {color}'
    elif geom == 'hexbin':
        if x and x_kind == 'embedding':
            sentence = f'Hexbin embedding of {db_id} ({x})'
        else:
            sentence = f'Hexbin plot of {db_id}'
        if color:
            sentence += f', hexes aggregated on {color}'
        else:
            sentence += '; coloured by cell density'
    elif geom == 'xy_scatter':
        sentence = f'Scatter of {y} vs {x} in {db_id}'
        if color:
            sentence += f', cells coloured by {color}'
    elif geom == 'xy_hexbin':
        sentence = f'Hexbin of {y} vs {x} in {db_id}'
        if color:
            sentence += f', hexes aggregated on {color}'
        else:
            sentence += '; coloured by cell density'
    elif geom in ('violin', 'box'):
        kind = {'violin': 'Violin plot', 'box': 'Box plot'}[geom]
        if y:
            sentence = f'{kind} of {y} expression per {x} in {db_id}'
        else:
            sentence = f'{kind} of cell counts per {x} in {db_id}'
        if color:
            sentence += f', split by {color}'
    elif geom == 'bar':
        if y:
            sentence = f'Bar plot of mean {y} per {x} in {db_id}'
            if ci:
                sentence += ' with 95% confidence interval (bootstrap)'
        else:
            sentence = f'Bar plot of cell counts per {x} in {db_id}'
        if color:
            sentence += f', split by {color}'
    elif geom in ('histogram', 'ecdf', 'kde'):
        label = {'histogram': 'Histogram', 'ecdf': 'Empirical CDF',
                 'kde': 'KDE density'}[geom]
        sentence = f'{label} of {x} in {db_id}'
        if color:
            sentence += f', grouped by {color}'
    elif geom == 'heatmap':
        sentence = (f'Heatmap of mean gene expression per {y} in {db_id}'
                    f' (z-scored per gene)')
    else:
        sentence = f'{geom} plot of {db_id}'

    # Layer — name the data source for any geom touching expression values.
    if layer and layer != 'X' and geom in (
            'scatter', 'violin', 'box', 'bar',
            'histogram', 'ecdf', 'kde', 'heatmap'):
        sentence += f'; layer: {layer}'
    elif geom == 'heatmap' and layer:
        sentence += f'; layer: {layer}'

    # Footer — sample size, sub-grouping cardinality, and any active subset.
    footer = []
    if n_cells:
        footer.append(f'n = {n_cells:,} cell{plural(n_cells)}')
    if n_groups:
        footer.append(f'{n_groups} group{plural(n_groups)}')
    if sample:
        footer.append(f'random sample of {sample:,}')
    if subset:
        footer.append(f'subset: {subset}')
    if footer:
        sentence += '.  ' + '; '.join(footer)
    return sentence + '.'


def plot_scatter(
    embedding_df: pd.DataFrame,
    color_df: Optional[pd.DataFrame],
    color_field: Optional[str],
    color_kind: str = 'categorical',
    x_label: str = 'Dim 1',
    y_label: str = 'Dim 2',
    width: float = 8,
    height: float = 6,
    dpi: int = 150,
    output_format: str = 'png',
    point_size: float = 3,
    alpha: float = 0.8,
    palette: Optional[str] = None,          # legacy alias (used as palette_cat)
    palette_cat: str = _DEFAULT_CATEGORICAL,
    palette_cont: str = _DEFAULT_SEQUENTIAL,
    palette_div: str = _DEFAULT_DIVERGENT,
    vmin: Optional[float] = None,           # colorbar clip — None = auto
    vmax: Optional[float] = None,
    title_extra: Optional[str] = None,
    caption: Optional[str] = None,
    source_url: Optional[str] = None,
    font_family: Optional[str] = None,
    font_size: Optional[float] = None,
) -> bytes:
    """Scatter plot on two embedding dimensions, optionally colored by any field.

    Args:
        embedding_df: DataFrame with columns [cell_name, dim_0, dim_1].
        color_df:     DataFrame with [cell_name, <color_field>] or None.
        color_field:  Column name to use for colouring.
        color_kind:   'categorical', 'numerical', or 'gene'.
        x_label:      X-axis label override.
        y_label:      Y-axis label override.
        output_format: 'png' | 'svg' | 'pdf'.
        caption:      Figure caption embedded in PDF or passed via header.
    """
    _setup_style(font_family, font_size)
    fig, ax = plt.subplots(figsize=(width, height))

    x_col = embedding_df.columns[-2]
    y_col = embedding_df.columns[-1]
    plot_df = embedding_df.copy()

    # Back-compat: legacy callers passing a single `palette=` override the
    # categorical default.
    if palette:
        palette_cat = palette

    n_cells = int(len(plot_df))

    if color_df is not None and color_field:
        color_values = color_df.set_index('cell_name')[color_field]
        plot_df[color_field] = color_values.reindex(plot_df['cell_name'].values).values

        if color_kind == 'categorical':
            cmap_name, _ = pick_cmap('categorical', palette_cat=palette_cat,
                                     palette_cont=palette_cont,
                                     palette_div=palette_div)
            sns.scatterplot(
                data=plot_df, x=x_col, y=y_col,
                hue=color_field, linewidth=0, s=point_size, alpha=alpha,
                palette=cmap_name, ax=ax,
            )
            n_cats = int(plot_df[color_field].nunique())
            legend_fontsize = 6 if n_cats > 12 else 8
            ax.legend(
                bbox_to_anchor=(1.02, 1.0), loc='upper left',
                borderaxespad=0.0,
                fontsize=legend_fontsize, markerscale=2,
                title=color_field, title_fontsize=legend_fontsize + 1,
                frameon=False,
            )
            title_bits = [color_field, f'{n_cats} grps', f'n={_fmt_n(n_cells)}']
        else:
            color_array = np.asarray(pd.to_numeric(plot_df[color_field], errors='coerce'), dtype=float)
            cmap_name, used_kind = pick_cmap(
                'continuous',
                palette_cat=palette_cat,
                palette_cont=palette_cont,
                palette_div=palette_div,
                values=color_array,
            )
            sc_kwargs: dict = {
                'c': color_array,
                'cmap': cmap_name,
                's': point_size,
                'alpha': alpha,
                'linewidths': 0,
            }
            if used_kind == 'divergent':
                finite = color_array[np.isfinite(color_array)]
                vlim = float(np.nanmax(np.abs(finite))) if finite.size else 1.0
                sc_kwargs['vmin'] = -vlim
                sc_kwargs['vmax'] = +vlim
            # User overrides always win.
            if vmin is not None: sc_kwargs['vmin'] = vmin
            if vmax is not None: sc_kwargs['vmax'] = vmax
            sc = ax.scatter(
                np.asarray(plot_df[x_col], dtype=float),
                np.asarray(plot_df[y_col], dtype=float),
                **sc_kwargs,
            )
            label_suffix = ' (z-scored / divergent)' if used_kind == 'divergent' else ''
            plt.colorbar(sc, ax=ax, label=f'{color_field}{label_suffix}', shrink=0.8)
            title_bits = [color_field, f'n={_fmt_n(n_cells)}']
    else:
        sns.scatterplot(
            data=plot_df, x=x_col, y=y_col,
            linewidth=0, s=point_size, alpha=alpha, color='steelblue', ax=ax,
        )
        title_bits = ['UMAP', f'n={_fmt_n(n_cells)}']

    ax.set_xlabel(x_label)
    ax.set_ylabel(y_label)
    ax.set_title(_compose_title(title_bits, extra=title_extra))
    sns.despine(ax=ax)

    return _render_figure(fig, dpi, output_format, caption, source_url)


def plot_hexbin(
    embedding_df: pd.DataFrame,
    color_df: Optional[pd.DataFrame],
    color_field: Optional[str],
    color_kind: str = 'numerical',
    x_label: str = 'Dim 1',
    y_label: str = 'Dim 2',
    width: float = 8,
    height: float = 6,
    dpi: int = 150,
    output_format: str = 'png',
    palette: Optional[str] = None,                # legacy alias for palette_cat
    palette_cat: str = _DEFAULT_CATEGORICAL,
    palette_cont: str = _DEFAULT_SEQUENTIAL,
    palette_div: str = _DEFAULT_DIVERGENT,
    gridsize: int = 60,
    mincnt: int = 1,
    alpha: float = 1.0,
    vmin: Optional[float] = None,                 # colorbar clip — None = auto
    vmax: Optional[float] = None,
    title_extra: Optional[str] = None,
    caption: Optional[str] = None,
    source_url: Optional[str] = None,
    font_family: Optional[str] = None,
    font_size: Optional[float] = None,
) -> bytes:
    """Hexagonal-binned embedding plot — alternative to scatter for showing
    *regional* enrichment when overlapping points obscure density.

    Three colour modes, switched by ``color_kind``:

    * ``'gene'`` / ``'numerical'`` — each hex shows the **mean** value of
      the colour field across the cells that fall in it.  Color = mean,
      colourbar attached.
    * ``'categorical'`` — each hex is coloured by the **modal category**
      (the most-abundant cell type / cluster in that hex).  Hex alpha is
      driven by a **dominance score** = ``count_top1 / count_top2``,
      mapped through a sigmoid so:
          ratio 1.0 (perfectly mixed)  → alpha ≈ 0.25 (transparent)
          ratio 2.0 (top is 2× runner-up) → alpha ≈ 0.5
          ratio ≥ 5.0 (essentially pure) → alpha ≈ 1.0
      A small legend lists the categories that win at least one hex.
    * Color field missing — each hex shows the **cell density** (a basic
      density plot).

    Args:
        embedding_df: DataFrame with [cell_name, dim_0, dim_1].
        color_df:     DataFrame with [cell_name, <color_field>] or None.
        color_field:  column name to colour by.
        color_kind:   'numerical' | 'gene' | 'categorical'.
        gridsize:     number of hex bins along the x-axis.  Higher = finer.
        mincnt:       minimum cells per hex to render (lower = more sparse hexes).
        alpha:        per-hex alpha (numerical/gene mode).  Ignored in
                      categorical mode (dominance drives alpha there).
    """
    _setup_style(font_family, font_size)
    fig, ax = plt.subplots(figsize=(width, height))

    x_col = embedding_df.columns[-2]
    y_col = embedding_df.columns[-1]
    xs = np.asarray(embedding_df[x_col], dtype=float)
    ys = np.asarray(embedding_df[y_col], dtype=float)

    # Back-compat: legacy single-`palette` callers point at the categorical
    # default (it was the only knob before this revision).
    if palette:
        palette_cat = palette

    n_cells = int(len(embedding_df))

    if color_df is None or not color_field:
        # Density only — sequential, count of cells per hex.
        cmap_name, _ = pick_cmap('continuous',
                                 palette_cat=palette_cat,
                                 palette_cont=palette_cont,
                                 palette_div=palette_div)
        hb_kwargs: dict = dict(gridsize=gridsize, mincnt=mincnt,
                               cmap=cmap_name, linewidths=0)
        if vmin is not None: hb_kwargs['vmin'] = vmin
        if vmax is not None: hb_kwargs['vmax'] = vmax
        hb = ax.hexbin(xs, ys, **hb_kwargs)
        plt.colorbar(hb, ax=ax, label='cell count', shrink=0.8)
        title_bits = ['density', f'n={_fmt_n(n_cells)}', f'gridsize={gridsize}']
        ax.set_title(_compose_title(title_bits, extra=title_extra))

    elif color_kind in ('numerical', 'gene'):
        # Per-hex mean of a continuous value.
        values = color_df.set_index('cell_name')[color_field].reindex(
            embedding_df['cell_name'].values
        )
        cvals = np.asarray(values, dtype=float)
        cmap_name, used_kind = pick_cmap(
            'continuous',
            palette_cat=palette_cat,
            palette_cont=palette_cont,
            palette_div=palette_div,
            values=cvals,
        )
        hb_kwargs: dict = {
            'C': cvals,
            'reduce_C_function': np.mean,
            'gridsize': gridsize,
            'mincnt': mincnt,
            'cmap': cmap_name,
            'alpha': alpha,
            'linewidths': 0,
        }
        if used_kind == 'divergent':
            finite = cvals[np.isfinite(cvals)]
            vlim = float(np.nanmax(np.abs(finite))) if finite.size else 1.0
            hb_kwargs['vmin'] = -vlim
            hb_kwargs['vmax'] = +vlim
        # Explicit user overrides always win — useful to clip outliers
        # (e.g. one hex with a mean expression of 12 washes out the rest).
        if vmin is not None: hb_kwargs['vmin'] = vmin
        if vmax is not None: hb_kwargs['vmax'] = vmax
        hb = ax.hexbin(xs, ys, **hb_kwargs)
        label_kind = 'expression' if color_kind == 'gene' else 'value'
        label_suffix = ' (divergent)' if used_kind == 'divergent' else ''
        plt.colorbar(hb, ax=ax,
                     label=f'mean {color_field} ({label_kind}){label_suffix}',
                     shrink=0.8)
        title_bits = [
            color_field,
            f'⬣ mean ({"div" if used_kind == "divergent" else "seq"})',
            f'n={_fmt_n(n_cells)}',
        ]
        ax.set_title(_compose_title(title_bits, extra=title_extra))

    else:
        # Categorical: modal label + dominance.  Done by hand because
        # matplotlib's hexbin doesn't natively aggregate categoricals.
        cats = color_df.set_index('cell_name')[color_field].reindex(
            embedding_df['cell_name'].values
        ).astype(str)
        # Encode categories to integer codes so we can use hexbin's C param
        # to discover hex membership; the actual reduce is done after.
        unique_cats = pd.Series(cats).dropna().unique()
        unique_cats = sorted(unique_cats.tolist())
        cat_to_code = {c: i for i, c in enumerate(unique_cats)}
        codes = np.array([cat_to_code.get(c, -1) for c in cats], dtype=float)

        # Step 1 — find hex centers + which cells map to which hex.
        # We use a transparent hexbin call just to get the hex polygon mesh,
        # then assign each cell to its hex by nearest center.
        scratch = ax.hexbin(xs, ys, gridsize=gridsize, mincnt=0,
                            visible=False, linewidths=0)
        offsets = scratch.get_offsets()  # (n_hex, 2) — centres
        # Map cells → hex_index via nearest centre.  For a typical hex grid
        # this is exact because the bins are convex.
        from scipy.spatial import cKDTree  # noqa: WPS433 — only when needed
        tree = cKDTree(offsets)
        _, hex_idx = tree.query(np.column_stack([xs, ys]))

        # Step 2 — per hex: counts per category, then modal + dominance.
        n_hex = offsets.shape[0]
        n_cats = len(unique_cats)
        counts = np.zeros((n_hex, n_cats), dtype=np.int64)
        valid = codes >= 0
        np.add.at(counts, (hex_idx[valid], codes[valid].astype(int)), 1)

        hex_totals = counts.sum(axis=1)
        used = hex_totals >= mincnt
        if not used.any():
            ax.text(0.5, 0.5, 'no hex bins reached mincnt; lower --mincnt',
                    transform=ax.transAxes, ha='center', va='center', color='#888')
        else:
            # Modal category per hex; second-most-abundant for the ratio.
            sorted_counts = np.sort(counts, axis=1)
            top1 = sorted_counts[:, -1]
            top2 = sorted_counts[:, -2] if n_cats >= 2 else np.zeros(n_hex)
            # dominance = top1 / max(top2, 1) ∈ [1, +∞); ties = 1.
            dominance = top1.astype(float) / np.clip(top2, 1, None)
            # Map dominance → alpha via a sigmoid centred at ratio 2.0.
            # alpha(1)=0.25, alpha(2)=0.50, alpha(5)≈0.95.
            alphas_hex = 1.0 / (1.0 + np.exp(-(dominance - 2.0)))
            alphas_hex = np.clip(alphas_hex, 0.15, 1.0)
            modal_code = counts.argmax(axis=1)

            # Categorical: always use a qualitative palette.  pick_cmap()
            # handles fallback if the user accidentally passed a divergent
            # or sequential cmap for categorical colour.
            cat_palette, _ = pick_cmap(
                'categorical',
                palette_cat=palette_cat,
                palette_cont=palette_cont,
                palette_div=palette_div,
            )
            cmap_obj = matplotlib.cm.get_cmap(cat_palette)
            n_palette = getattr(cmap_obj, 'N', 256)
            colors = cmap_obj(np.array([m % n_palette for m in modal_code]) / max(n_palette - 1, 1))

            # Draw hexes as a PolyCollection so we can vary per-hex alpha.
            # matplotlib's hexbin builds the polygon already sized in data
            # units (sx = (xmax-xmin)/gridsize, sy = (ymax-ymin)/(gridsize*√3/2)),
            # centred at origin — we just translate it to each hex centre.
            # The earlier version re-scaled by an additional 1/gridsize,
            # which made the hexes tiny.
            hex_polygon = scratch.get_paths()[0].vertices  # data units, centred at (0,0)
            from matplotlib.collections import PolyCollection
            polys = []
            for cx, cy in zip(offsets[used, 0], offsets[used, 1]):
                polys.append(np.column_stack([
                    hex_polygon[:, 0] + cx,
                    hex_polygon[:, 1] + cy,
                ]))
            face_colours = colors[used]
            face_colours[:, 3] = alphas_hex[used]  # set per-hex alpha
            coll = PolyCollection(
                polys, facecolors=face_colours,
                edgecolors='none', linewidths=0,
            )
            ax.add_collection(coll)
            ax.set_xlim(xs.min(), xs.max())
            ax.set_ylim(ys.min(), ys.max())

            # Legend: one entry per category that wins at least one used hex.
            won = np.unique(modal_code[used])
            handles = [
                plt.matplotlib.patches.Patch(
                    color=cmap_obj((c % n_palette) / max(n_palette - 1, 1)),
                    label=unique_cats[c],
                )
                for c in won
            ]
            ax.legend(
                handles=handles, bbox_to_anchor=(1.02, 1.0),
                loc='upper left', borderaxespad=0.0,
                fontsize=8, title=color_field,
                title_fontsize=9, frameon=False,
            )
        # Title carries the categorical context: n_cats, n_used hexes,
        # and the median dominance so the reader knows "how mixed" the
        # plot looks before reading the alpha.
        try:
            n_used_hex = int(used.sum())
            median_dom = float(np.median(dominance[used])) if n_used_hex else 0.0
        except Exception:
            n_used_hex, median_dom = 0, 0.0
        title_bits = [
            color_field,
            f'⬣ modal · α=dom',
            f'{len(unique_cats)} grps',
            f'n={_fmt_n(n_cells)}',
            f'median dom={median_dom:.1f}',
        ]
        ax.set_title(_compose_title(title_bits, extra=title_extra))

    ax.set_xlabel(x_label)
    ax.set_ylabel(y_label)
    ax.set_aspect('equal', adjustable='datalim')
    sns.despine(ax=ax)

    return _render_figure(fig, dpi, output_format, caption, source_url)


def plot_distribution(
    data_df: pd.DataFrame,
    x_field: str,
    geom: str = 'ecdf',
    color_field: Optional[str] = None,
    bins: int = 20,
    width: float = 7,
    height: float = 4,
    dpi: int = 150,
    output_format: str = 'png',
    caption: Optional[str] = None,
    source_url: Optional[str] = None,
    font_family: Optional[str] = None,
    font_size: Optional[float] = None,
) -> bytes:
    """Univariate distribution plot: ecdf, kde, or histogram.

    Optionally grouped by a categorical field via the color_field hue.

    Args:
        data_df:     DataFrame with [cell_name, x_field, (color_field)].
        x_field:     Gene or numerical column for the X axis.
        geom:        'ecdf' | 'kde' | 'histogram'.
        color_field: Optional categorical column for hue grouping.
        output_format: 'png' | 'svg' | 'pdf'.
        caption:     Figure caption.
    """
    _setup_style(font_family, font_size)
    fig, ax = plt.subplots(figsize=(width, height))

    hue = color_field if color_field and color_field in data_df.columns else None

    if geom == 'ecdf':
        sns.ecdfplot(data=data_df, x=x_field, hue=hue, ax=ax)
    elif geom == 'kde':
        # `common_norm=False` lets each group sum to 1 independently — usually
        # what people want when comparing distributions of different sizes.
        sns.kdeplot(data=data_df, x=x_field, hue=hue, ax=ax,
                    common_norm=False, fill=True, alpha=0.4)
    else:
        # seaborn's default multiple='layer' overlays groups with transparency,
        # which reads as stacked-but-blurry. 'dodge' puts them side-by-side
        # which is what a "multi-category bar plot" should look like.
        sns.histplot(data=data_df, x=x_field, bins=bins, hue=hue, ax=ax,
                     multiple='dodge' if hue else 'layer')

    ax.set_xlabel(x_field)
    ax.set_title(f'{x_field} ({geom})')
    sns.despine(ax=ax)
    _legend_outside(ax)

    return _render_figure(fig, dpi, output_format, caption, source_url)


def plot_grouped(
    data_df: pd.DataFrame,
    x_field: str,
    y_field: Optional[str] = None,
    geom: str = 'bar',
    ci: bool = True,
    width: float = 8,
    height: float = 5,
    dpi: int = 150,
    output_format: str = 'png',
    caption: Optional[str] = None,
    source_url: Optional[str] = None,
    font_family: Optional[str] = None,
    font_size: Optional[float] = None,
    hue_field: Optional[str] = None,
) -> bytes:
    """Values grouped by a categorical field: bar (with CI), box, or violin.

    Args:
        data_df:   DataFrame with [cell_name, x_field, (y_field), (hue_field)].
        x_field:   Categorical column for grouping (X axis).
        y_field:   Gene or numerical column for Y axis. None → cell counts.
        hue_field: Optional second categorical column. Bars / boxes / violins
                   for each (x_field × hue_field) combination are dodged
                   side-by-side, NOT stacked.
        geom:      'bar' | 'box' | 'violin'.
        ci:        Show 95% confidence interval on bar plots (seaborn bootstrap).
        output_format: 'png' | 'svg' | 'pdf'.
        caption:   Figure caption.
    """
    _setup_style(font_family, font_size)
    fig, ax = plt.subplots(figsize=(width, height))

    cat_order = data_df[x_field].value_counts().index.tolist()
    hue = hue_field if hue_field and hue_field in data_df.columns else None
    hue_order = (data_df[hue].value_counts().index.tolist()
                 if hue else None)

    if y_field is None:
        if hue:
            # 2-factor count: bar of count(cells) per (x_field × hue_field),
            # dodged side-by-side. Use crosstab → long form.
            ct = pd.crosstab(data_df[x_field], data_df[hue])
            ct = ct.reindex(cat_order)[hue_order]
            long = ct.reset_index().melt(id_vars=x_field, var_name=hue, value_name='count')
            sns.barplot(data=long, x=x_field, y='count', hue=hue,
                        order=cat_order, hue_order=hue_order, ax=ax)
            ax.set_ylabel('Cell count')
        else:
            counts = data_df[x_field].value_counts().reindex(cat_order)
            sns.barplot(x=counts.index, y=counts.values, ax=ax)
            ax.set_ylabel('Cell count')
    elif geom == 'box':
        sns.boxplot(data=data_df, x=x_field, y=y_field, hue=hue,
                    order=cat_order, hue_order=hue_order,
                    ax=ax, showfliers=False)
    elif geom == 'violin':
        sns.violinplot(data=data_df, x=x_field, y=y_field, hue=hue,
                       order=cat_order, hue_order=hue_order, ax=ax)
    else:  # bar
        errorbar = ('ci', 95) if ci else None
        sns.barplot(data=data_df, x=x_field, y=y_field, hue=hue,
                    order=cat_order, hue_order=hue_order,
                    ax=ax, errorbar=errorbar)

    ax.set_xlabel(x_field)
    plt.setp(ax.get_xticklabels(), rotation=45, ha='right')
    sns.despine(ax=ax)
    _legend_outside(ax)
    fig.tight_layout()

    return _render_figure(fig, dpi, output_format, caption, source_url)


def plot_heatmap(
    mean_expr_df: pd.DataFrame,
    width: float = 10,
    height: float = 6,
    dpi: int = 150,
    output_format: str = 'png',
    caption: Optional[str] = None,
    source_url: Optional[str] = None,
    font_family: Optional[str] = None,
    font_size: Optional[float] = None,
) -> bytes:
    """Heatmap of mean gene expression per category group.

    Args:
        mean_expr_df: DataFrame with index=category values, columns=gene names.
                      Values are z-scored per gene column before display.
        output_format: 'png' | 'svg' | 'pdf'.
        caption:      Figure caption.
    """
    _setup_style(font_family, font_size)
    fig, ax = plt.subplots(figsize=(width, height))

    z_df = (mean_expr_df - mean_expr_df.mean()) / (mean_expr_df.std() + 1e-9)
    sns.heatmap(
        z_df, ax=ax,
        cmap='RdBu_r', center=0,
        linewidths=0.3,
        yticklabels=True,
        xticklabels=True,
        cbar_kws={'label': 'z-score'},
    )
    ax.set_xlabel('Gene')
    ax.set_ylabel('')
    plt.setp(ax.get_xticklabels(), rotation=45, ha='right', fontsize=8)
    fig.tight_layout()

    return _render_figure(fig, dpi, output_format, caption, source_url)
