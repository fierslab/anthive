"""
Plotting functions for Anthive.

Each function accepts pandas DataFrames and renders with matplotlib/seaborn,
mirroring the Streamlit visualisation pages. Common parameters width, height,
dpi, and output_format control output size and format (png | svg | pdf).

New unified API: plot_scatter, plot_distribution, plot_grouped, plot_heatmap.
Legacy functions (plot_umap, plot_gene, plot_cat, plot_num) remain unchanged.
"""

import io
from typing import Optional

import numpy as np
import matplotlib
matplotlib.use('Agg')  # non-interactive backend — must precede pyplot import
import matplotlib.pyplot as plt
from matplotlib.figure import Figure as MplFigure
import seaborn as sns
import pandas as pd


_DEFAULT_PALETTE = 'tab20'
_VALID_FORMATS = {'png', 'svg', 'pdf'}


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

    # PNG / SVG: structured metadata via matplotlib savefig kwarg.
    metadata: dict = {}
    if caption:
        metadata['Title'] = caption[:200]
        metadata['Description'] = caption
    if source_url:
        metadata['Source'] = source_url
    metadata['Software'] = 'anthive ant-serve / matplotlib'

    buf = io.BytesIO()
    save_kwargs = {'format': output_format, 'dpi': dpi, 'bbox_inches': 'tight'}
    if output_format in ('png', 'svg', 'pdf') and metadata:
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

        legend = ax.get_legend()
        if legend and color_type == 'categorical':
            n_cats = int(plot_df[color_field].nunique())
            if n_cats > 12:
                ax.legend(
                    bbox_to_anchor=(1.02, 1), loc='upper left',
                    fontsize=6, markerscale=2, title=color_field,
                )
            else:
                ax.legend(fontsize=8, markerscale=2, title=color_field)
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
) -> str:
    """Return a human-readable figure caption for the given plot parameters."""
    if geom == 'scatter':
        base = f'Scatter plot of {db_id}'
        if x and x_kind == 'embedding':
            base = f'UMAP of {db_id} ({x})'
        if color:
            return f'{base}, colored by {color}'
        return base
    if geom in ('violin', 'box'):
        if y:
            return f'{y} expression by {x} in {db_id} ({geom})'
        return f'Cell counts per {x} in {db_id} ({geom})'
    if geom == 'bar':
        if y:
            return f'Mean {y} per {x} in {db_id}'
        return f'Cell counts per {x} in {db_id}'
    if geom in ('histogram', 'ecdf', 'kde'):
        base = f'Distribution of {x} in {db_id} ({geom})'
        if color:
            return f'{base}, grouped by {color}'
        return base
    if geom == 'heatmap':
        return f'Mean gene expression per {y} in {db_id} (layer: {layer})'
    return f'{geom} plot of {db_id}'


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
    palette: str = _DEFAULT_PALETTE,
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

    if color_df is not None and color_field:
        color_values = color_df.set_index('cell_name')[color_field]
        plot_df[color_field] = color_values.reindex(plot_df['cell_name'].values).values

        if color_kind == 'categorical':
            sns.scatterplot(
                data=plot_df, x=x_col, y=y_col,
                hue=color_field, linewidth=0, s=point_size, alpha=alpha,
                palette=palette, ax=ax,
            )
            n_cats = int(plot_df[color_field].nunique())
            if n_cats > 12:
                ax.legend(
                    bbox_to_anchor=(1.02, 1), loc='upper left',
                    fontsize=6, markerscale=2, title=color_field,
                )
            else:
                ax.legend(fontsize=8, markerscale=2, title=color_field)
        else:
            # Numerical / gene: use matplotlib directly so viridis (or any cmap)
            # is applied correctly and a proper colorbar is shown instead of a
            # dot-legend.
            cmap = palette if palette != _DEFAULT_PALETTE else 'viridis'
            color_array = np.asarray(pd.to_numeric(plot_df[color_field], errors='coerce'), dtype=float)
            sc = ax.scatter(
                np.asarray(plot_df[x_col], dtype=float),
                np.asarray(plot_df[y_col], dtype=float),
                c=color_array,
                cmap=cmap,
                s=point_size,
                alpha=alpha,
                linewidths=0,
            )
            plt.colorbar(sc, ax=ax, label=color_field, shrink=0.8)
    else:
        sns.scatterplot(
            data=plot_df, x=x_col, y=y_col,
            linewidth=0, s=point_size, alpha=alpha, color='steelblue', ax=ax,
        )

    ax.set_xlabel(x_label)
    ax.set_ylabel(y_label)
    ax.set_title(color_field or 'UMAP')
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
        sns.kdeplot(data=data_df, x=x_field, hue=hue, ax=ax)
    else:
        sns.histplot(data=data_df, x=x_field, bins=bins, hue=hue, ax=ax)

    ax.set_xlabel(x_field)
    ax.set_title(f'{x_field} ({geom})')
    sns.despine(ax=ax)

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
) -> bytes:
    """Values grouped by a categorical field: bar (with CI), box, or violin.

    Args:
        data_df:   DataFrame with [cell_name, x_field, (y_field)].
        x_field:   Categorical column for grouping (X axis).
        y_field:   Gene or numerical column for Y axis. None → cell counts.
        geom:      'bar' | 'box' | 'violin'.
        ci:        Show 95% confidence interval on bar plots (seaborn bootstrap).
        output_format: 'png' | 'svg' | 'pdf'.
        caption:   Figure caption.
    """
    _setup_style(font_family, font_size)
    fig, ax = plt.subplots(figsize=(width, height))

    cat_order = data_df[x_field].value_counts().index.tolist()

    if y_field is None:
        counts = data_df[x_field].value_counts().reindex(cat_order)
        sns.barplot(x=counts.index, y=counts.values, ax=ax)
        ax.set_ylabel('Cell count')
    elif geom == 'box':
        sns.boxplot(data=data_df, x=x_field, y=y_field,
                    order=cat_order, ax=ax, showfliers=False)
    elif geom == 'violin':
        sns.violinplot(data=data_df, x=x_field, y=y_field,
                       order=cat_order, ax=ax)
    else:  # bar
        errorbar = ('ci', 95) if ci else None
        sns.barplot(data=data_df, x=x_field, y=y_field,
                    order=cat_order, ax=ax, errorbar=errorbar)

    ax.set_xlabel(x_field)
    plt.setp(ax.get_xticklabels(), rotation=45, ha='right')
    sns.despine(ax=ax)
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
