"""
Plotting functions for Anthive — all return PNG bytes.

Each function accepts pandas DataFrames and renders with matplotlib/seaborn,
mirroring the Streamlit visualisation pages. Common parameters width, height,
and dpi control output size and resolution.
"""

import io
from typing import Optional

import matplotlib
matplotlib.use('Agg')  # non-interactive backend — must precede pyplot import
import matplotlib.pyplot as plt
from matplotlib.figure import Figure as MplFigure
import seaborn as sns
import pandas as pd


_DEFAULT_PALETTE = 'tab20'


def _setup_style() -> None:
    sns.set_theme(style='ticks', font_scale=0.9)


def _render_png(fig: MplFigure, dpi: int) -> bytes:
    """Render figure to PNG bytes and close it immediately."""
    buf = io.BytesIO()
    fig.savefig(buf, format='png', dpi=dpi, bbox_inches='tight')
    plt.close(fig)
    buf.seek(0)
    return buf.getvalue()


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
