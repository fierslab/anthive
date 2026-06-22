"""
Plotting functions for Anthive.

Each function accepts pandas DataFrames and renders with matplotlib/seaborn,
mirroring the Streamlit visualisation pages. Common parameters width, height,
dpi, and output_format control output size and format (png | svg | pdf).

New unified API: plot_scatter, plot_distribution, plot_grouped, plot_heatmap.
Legacy functions (plot_umap, plot_gene, plot_cat, plot_num) remain unchanged.
"""

import io
from typing import Dict, List, Optional, Tuple

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

_VALID_FORMATS = {'png', 'svg', 'pdf'}

# ── custom sequential colormaps registered into matplotlib ────────────────
# Matplotlib ships title-case PLURAL sequential cmaps (Blues, Reds, Greens
# …) that go white→colour, but their colour stops are perceptually-tuned
# (not pure RGB) and they don't cover magenta, cyan, yellow, or pure black.
# We register CLEAN white→pure-colour gradients under the lowercase
# singular names, leaving matplotlib's stock cmaps untouched. The reversed
# variants (_r) go colour→white.
#
# Use case: figures where the user wants a single dominant colour
# (matching a manuscript palette, brand colour, or a poster theme) and
# doesn't need matplotlib's perceptual uniformity.
#
# Idempotent: registers only if not already present. Safe to re-import.
#
# Stops chosen so that low values fade into a white background. For
# 'green' / 'orange' we dim the pure RGB to avoid the harsh primary —
# (0, 0.6, 0) and (1, 0.5, 0) read better in print and on screen than
# (0, 1, 0) and (1, 0.65, 0). 'black' goes to pure (0,0,0); 'gray' lands
# at (0.5, 0.5, 0.5) so it doesn't visually collapse with 'black' at the
# high end.
_CUSTOM_SEQUENTIAL_ENDPOINTS = {
    'magenta': (1.0, 0.0, 1.0),
    'blue':    (0.0, 0.0, 1.0),
    'red':     (1.0, 0.0, 0.0),
    'green':   (0.0, 0.6, 0.0),
    'orange':  (1.0, 0.5, 0.0),
    'purple':  (0.5, 0.0, 0.5),
    'cyan':    (0.0, 0.8, 0.8),    # dimmed slightly — pure cyan washes out on white
    'yellow':  (0.95, 0.85, 0.0),  # dimmed — pure yellow is invisible at low alpha
    'black':   (0.0, 0.0, 0.0),
    # NOTE on greyscale: matplotlib ships both 'gray' and 'grey' as
    # aliases for its black→white grayscale cmap, so we can't claim
    # either name without overriding it. Use 'black' for white→pure
    # black (registered above) or matplotlib's 'Greys' for the
    # perceptually-tuned white→black-ish variant.
    # ── earth tones + extra hues ──
    'tan':         (0.82, 0.71, 0.55),   # CSS tan #D2B48C
    'greenyellow': (0.55, 0.85, 0.18),   # CSS greenyellow #ADFF2F, dimmed
    'turquoise':   (0.25, 0.88, 0.82),   # CSS turquoise #40E0D0
    # 'pink' would collide with matplotlib's pink cmap (perceptually-
    # tuned black-pink-white). Use 'hotpink' (CSS hotpink #FF69B4) for
    # the simple white→hot-pink variant.
    'hotpink':     (1.0, 0.41, 0.71),
    'brown':       (0.55, 0.27, 0.07),   # CSS saddlebrown #8B4513
}


def _register_custom_cmaps() -> None:
    try:
        import matplotlib as _mpl
        import matplotlib.colors as _mcolors
    except ImportError:
        return

    def _register_one(name: str, stops: list) -> None:
        try:
            _mpl.colormaps[name]
            return  # already registered
        except KeyError:
            pass
        cmap = _mcolors.LinearSegmentedColormap.from_list(name, stops, N=256)
        try:
            _mpl.colormaps.register(cmap, name=name)
        except (ValueError, AttributeError):
            try:
                _mpl.cm.register_cmap(name=name, cmap=cmap)
            except Exception:
                pass

    for name, end_rgb in _CUSTOM_SEQUENTIAL_ENDPOINTS.items():
        # forward: white → colour (low fades into background)
        _register_one(name, [(1.0, 1.0, 1.0), end_rgb])
        # reversed: colour → white (high fades into background)
        _register_one(name + '_r', [end_rgb, (1.0, 1.0, 1.0)])


_register_custom_cmaps()

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
    n_categories: Optional[int] = None,
    color_scale: Optional[str] = None,
) -> Tuple[str, str]:
    """Return ``(cmap_name, used_kind)`` for the given data kind.

    Args:
        kind:  ``'categorical'`` | ``'continuous'`` | ``'gene'``.
               ``'gene'`` is treated like ``'continuous'`` for cmap purposes.
        palette_cat / palette_cont / palette_div: per-family palettes.
        values: numeric array used to auto-decide sequential vs divergent
                inside the continuous branch when ``color_scale`` is
                None / 'auto'.  Pass ``None`` for "force sequential".
        color_scale: explicit override for the sequential-vs-divergent
                decision in the continuous branch. Values:
                ``None`` / ``'auto'`` — use the ``_looks_divergent``
                  heuristic against ``values`` (the prior behaviour).
                ``'sequential'`` — force the sequential family, even when
                  the data straddles zero (useful for hexbin/heatmap on
                  effect-size data where you want a one-sided ramp).
                ``'divergent'`` — force the divergent family, even when
                  the data is one-sided (useful for z-scores that are
                  signed but happen to be all-positive in the sampled
                  subset).
                Ignored for ``kind='categorical'``.

    Returns:
        ``(cmap_name, 'categorical' | 'sequential' | 'divergent')``.

    Defensive fallbacks:
        - a qualitative palette name passed for a continuous family is
          silently replaced with the default sequential cmap.
        - in ``auto`` mode, a divergent palette passed when the data is
          one-sided is replaced with the sequential default; a sequential
          palette passed when the data is divergent is replaced with the
          divergent default (so the user always sees a coherent axis).
        - in explicit ``sequential`` / ``divergent`` mode, fallbacks
          still pick the right default if the passed palette doesn't
          match the family — but the family is honoured.
    """
    if kind == 'categorical':
        chosen = palette_cat if palette_cat in _QUALITATIVE_PALETTES else _DEFAULT_CATEGORICAL
        # tab10 cycles after 10 colours — distinct clusters reuse hues and
        # become ambiguous.  When we know the cardinality and the user is
        # still on the small default, auto-promote.  tab20 has 20 distinct
        # colours; beyond that, even tab20 cycles but at least the labels
        # are distinguishable for legend reading.
        if n_categories is not None and chosen == 'tab10' and n_categories > 10:
            chosen = 'tab20'
        return chosen, 'categorical'

    # 'continuous' or 'gene' — decide divergent vs sequential.
    mode = (color_scale or 'auto').lower()
    if mode == 'divergent':
        is_div = True
    elif mode == 'sequential':
        is_div = False
    else:  # 'auto'
        is_div = bool(values is not None and _looks_divergent(values))

    if is_div:
        if palette_div in _DIVERGENT_PALETTES:
            return palette_div, 'divergent'
        return _DEFAULT_DIVERGENT, 'divergent'

    if palette_cont in _QUALITATIVE_PALETTES:
        return _DEFAULT_SEQUENTIAL, 'sequential'
    if palette_cont in _DIVERGENT_PALETTES:
        # one-sided data → divergent cmap wastes range; fall back
        return _DEFAULT_SEQUENTIAL, 'sequential'
    return palette_cont, 'sequential'


def _legend_outside(ax, title: Optional[str] = None, fontsize: float = 8) -> None:
    """Place a legend outside-top-right of the axes.

    Works in two scenarios:
      1. Seaborn / sns_* already created a legend → just relocate it.
      2. Plot used raw `ax.plot(..., label=...)` with no `ax.legend()` call
         → labelled artists exist but no legend yet; materialise one from
         them and place it outside.

    No-op only when there's neither an existing legend NOR any labelled
    artists on the axes — i.e. nothing meaningful to label.
    """
    legend = ax.get_legend()
    if legend is None:
        # No legend yet — check if there are labelled artists we should
        # materialise into one (plot_rolling and similar ax.plot-based
        # geoms hit this path; without this, every label= passed to
        # ax.plot was silently dropped).
        handles, _labels = ax.get_legend_handles_labels()
        if not handles:
            return
        existing_title = None
    else:
        existing_title = (
            legend.get_title().get_text() if legend.get_title() else None
        )
    new_title = title if title is not None else existing_title
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

    # No visible caption/footer is rendered into the image — the caption
    # travels alongside the bytes (X-Plot-Caption header, PNG tEXt chunk,
    # SVG/PDF <metadata>) so consumers can fetch it but the figure stays
    # unmarked for reuse in manuscripts / slides.

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
    # ── extra render context — caller passes whatever applies ───────────────
    color_kind: Optional[str] = None,
    bar_mode: Optional[str] = None,
    normalize: Optional[bool] = None,
    facet: Optional[str] = None,
    group_by: Optional[str] = None,
    bins: Optional[int] = None,
    window: Optional[int] = None,
    show_band: Optional[bool] = None,
    zscore: Optional[bool] = None,
    gridsize: Optional[int] = None,
    mincnt: Optional[int] = None,
    alpha: Optional[float] = None,
    log_x: Optional[bool] = None,
    log_y: Optional[bool] = None,
    transform_x: Optional[str] = None,
    transform_y: Optional[str] = None,
    asinh_scale: Optional[float] = None,
    vmin: Optional[float] = None,
    vmax: Optional[float] = None,
    vmin_quantile: Optional[float] = None,
    vmax_quantile: Optional[float] = None,
    color_scale: Optional[str] = None,
    palette_cat: Optional[str] = None,
    palette_cont: Optional[str] = None,
    palette_div: Optional[str] = None,
    marginals: Optional[bool] = None,
    regline: Optional[str] = None,
    iso_overlay: Optional[bool] = None,
    point_overlay: Optional[bool] = None,
    kde_n: Optional[int] = None,
    kde_bw: Optional[float] = None,
    n_levels: Optional[int] = None,
) -> str:
    """Build a multi-sentence figure caption — describe the plot, the data,
    and every knob the caller actually exercised.

    The caption travels alongside the rendered bytes (X-Plot-Caption HTTP
    header / PNG tEXt / SVG metadata) but is NOT drawn into the figure.
    The text is meant to be drop-in for a manuscript "Figure X. ..." legend
    or a methods note — so it states what the plot is, what's on each axis,
    which subset / sample / transforms are active, and which visual
    parameters were tuned away from defaults.

    Caller passes only the kwargs that apply to its geom; unused ones stay
    None and are silently skipped.  ASCII-only output (the caption rides
    in an HTTP header).
    """
    plural = lambda n: 's' if n != 1 else ''  # noqa: E731

    parts: List[str] = []   # each part is a complete sentence
    knobs: List[str] = []   # tail sentence — "Visual knobs: A; B; C."

    # Sentence 1 — what the plot is.
    if geom == 'scatter':
        if x and x_kind == 'embedding':
            sent = f'Embedding scatter plot of dataset {db_id} on the {x} projection'
        else:
            sent = f'Scatter plot of dataset {db_id}'
        if color:
            kind_phrase = f' ({color_kind})' if color_kind else ''
            sent += f', with cells coloured by {color}{kind_phrase}'
    elif geom == 'hexbin':
        if x and x_kind == 'embedding':
            sent = f'Hexbin embedding plot of dataset {db_id} on the {x} projection'
        else:
            sent = f'Hexbin plot of dataset {db_id}'
        if color:
            kind_phrase = f' ({color_kind})' if color_kind else ''
            sent += f', with each hex aggregating {color}{kind_phrase} across the cells it contains'
        else:
            sent += ', with hexes coloured by cell density'
    elif geom == 'xy_scatter':
        sent = f'Scatter plot of {y} vs {x} for dataset {db_id}'
        if color:
            sent += f', with points coloured by {color}'
    elif geom == 'xy_hexbin':
        sent = f'Hexbin plot of {y} vs {x} for dataset {db_id}'
        if color:
            sent += f', with each hex aggregating {color}'
        else:
            sent += ', with hexes coloured by cell density'
    elif geom == 'kde2d':
        sent = f'Two-dimensional kernel density estimate of {y} vs {x} for dataset {db_id}'
    elif geom in ('violin', 'box'):
        kind = {'violin': 'Violin plot', 'box': 'Box plot'}[geom]
        if y:
            sent = f'{kind} of {y} per {x} for dataset {db_id}'
        else:
            sent = f'{kind} of cell counts per {x} for dataset {db_id}'
        if color:
            sent += f', further split by {color}'
    elif geom == 'bar':
        if y:
            mode_phrase = {'stack': 'Stacked bar plot', 'dodge': 'Bar plot'}.get(bar_mode or 'dodge', 'Bar plot')
            sent = f'{mode_phrase} of mean {y} per {x} for dataset {db_id}'
            if ci and bar_mode != 'stack':
                sent += ' with bootstrap 95% confidence intervals'
        else:
            mode_phrase = {'stack': 'Stacked bar plot', 'dodge': 'Bar plot'}.get(bar_mode or 'dodge', 'Bar plot')
            sent = f'{mode_phrase} of cell counts per {x} for dataset {db_id}'
        if color:
            sent += f', further split by {color}'
        if normalize:
            sent += '; each stack is normalised to 100% so the chart shows proportions, not absolute counts'
        if facet:
            sent += f'; faceted into one panel per level of {facet}'
        if group_by:
            sent += f'; within-axis bars are grouped by {group_by}'
    elif geom in ('histogram', 'ecdf', 'kde'):
        label = {'histogram': 'Histogram', 'ecdf': 'Empirical cumulative-distribution plot',
                 'kde': 'Kernel-density-estimate plot'}[geom]
        sent = f'{label} of {x} for dataset {db_id}'
        if color:
            sent += f', with curves split by {color}'
        if geom == 'histogram' and bins:
            sent += f' ({bins} bins)'
    elif geom == 'heatmap':
        zsc = 'z-scored per gene' if zscore else 'raw mean'
        sent = f'Heatmap of {zsc} expression per {y} for dataset {db_id} (genes on the y-axis)'
    elif geom == 'rolling':
        sent = f'Rolling-mean trajectory of {y} along ordered {x} for dataset {db_id}'
        if color:
            sent += f', with one trace per level of {color}'
        if window:
            sent += f'; window size = {window} cells'
        if show_band:
            sent += '; IQR shown as a shaded band around each trace'
    else:
        sent = f'{geom} plot of dataset {db_id}'
    if not sent.endswith('.'):
        sent += '.'
    parts.append(sent)

    # Sentence 2 — sample / subset / layer / sampling.
    sample_bits: List[str] = []
    if n_cells:
        sample_bits.append(f'{n_cells:,} cell{plural(n_cells)}')
    if n_groups:
        sample_bits.append(f'{n_groups} group{plural(n_groups)}')
    if sample_bits:
        body = 'Built from ' + ' across '.join(sample_bits)
        if sample:
            body += f' (after random downsampling to {sample:,} cells)'
        if subset:
            body += f', restricted to the subset where {subset}'
        if layer and geom in ('scatter', 'violin', 'box', 'bar',
                               'histogram', 'ecdf', 'kde', 'heatmap', 'rolling',
                               'hexbin', 'xy_scatter', 'xy_hexbin', 'kde2d'):
            body += f'; expression values read from the "{layer}" layer'
        parts.append(body + '.')
    elif subset:
        parts.append(f'Restricted to the subset where {subset}.')

    # Sentence 3 — axis transforms.
    transforms: List[str] = []
    if log_x:
        transforms.append('x-axis on log10 scale')
    if log_y:
        transforms.append('y-axis on log10 scale')
    if transform_x and transform_x not in (None, 'linear'):
        if transform_x == 'asinh' and asinh_scale is not None:
            transforms.append(f'x-axis transformed by asinh (scale = {asinh_scale:g})')
        else:
            transforms.append(f'x-axis transformed by {transform_x}')
    if transform_y and transform_y not in (None, 'linear'):
        if transform_y == 'asinh' and asinh_scale is not None:
            transforms.append(f'y-axis transformed by asinh (scale = {asinh_scale:g})')
        else:
            transforms.append(f'y-axis transformed by {transform_y}')
    if transforms:
        parts.append('Axis scales: ' + '; '.join(transforms) + '.')

    # Trailing knobs — visual params worth surfacing.
    if geom == 'hexbin' or geom == 'xy_hexbin':
        if gridsize:
            knobs.append(f'hex gridsize = {gridsize}')
        if mincnt and mincnt > 1:
            knobs.append(f'hexes with fewer than {mincnt} cells suppressed (mincnt = {mincnt})')
    if vmin is not None or vmax is not None:
        if vmin is not None and vmax is not None:
            knobs.append(f'colour scale clipped to [{vmin:g}, {vmax:g}]')
        elif vmin is not None:
            knobs.append(f'colour scale lower-clipped at {vmin:g}')
        else:
            knobs.append(f'colour scale upper-clipped at {vmax:g}')
    if vmin_quantile is not None or vmax_quantile is not None:
        lo = vmin_quantile if vmin_quantile is not None else 0.0
        hi = vmax_quantile if vmax_quantile is not None else 1.0
        knobs.append(f'colour scale auto-clipped to the {lo:g}-{hi:g} quantile band of per-hex values')
    if color_scale and color_scale != 'auto':
        knobs.append(f'colour scale forced {color_scale}')
    # Only mention palette if the caller deviated from the defaults — listing
    # the default tab10/inferno_r/RdBu_r on every caption is noise.
    pal_bits = []
    if palette_cat and palette_cat != _DEFAULT_CATEGORICAL:
        pal_bits.append(f'categorical = {palette_cat}')
    if palette_cont and palette_cont != _DEFAULT_SEQUENTIAL:
        pal_bits.append(f'continuous = {palette_cont}')
    if palette_div and palette_div != _DEFAULT_DIVERGENT:
        pal_bits.append(f'divergent = {palette_div}')
    if pal_bits:
        knobs.append('palette overrides: ' + ', '.join(pal_bits))
    if marginals:
        knobs.append('marginal KDE strips drawn on top and right axes')
    if regline:
        knobs.append(f'regression line overlay ({regline})')
    if iso_overlay is False:
        knobs.append('iso-density contours suppressed')
    if point_overlay:
        knobs.append('individual cells drawn on top of the density')
    if geom == 'kde2d':
        kde_bits = []
        if kde_n:    kde_bits.append(f'KDE evaluated on a random sample of {kde_n:,} cells')
        if kde_bw is not None: kde_bits.append(f'bandwidth = {kde_bw:g}')
        if n_levels: kde_bits.append(f'{n_levels} iso-density levels')
        if kde_bits: knobs.append('; '.join(kde_bits))
    # Common default is alpha=0.8 (set by CommonPlotParams); only call it out
    # when it visibly deviates from the typical default range.
    if alpha is not None and alpha not in (0.8, 1.0):
        knobs.append(f'point alpha = {alpha:g}')

    if knobs:
        parts.append('Visual parameters: ' + '; '.join(knobs) + '.')

    return ' '.join(parts)


# ── biology-keyed plot descriptors (X-Plot-Title / X-Plot-Filename) ──────────
#
# X-Plot-Caption answers "what render knobs are active"; X-Plot-Title and
# X-Plot-Filename answer "what biology am I looking at". Both are short,
# ASCII-safe, and produced by `build_plot_descriptor()` below.
#
# Both helpers are pure — no DB or filesystem I/O — so they're cheap on
# every render AND on cache hits.

import re as _re

_FILENAME_MAX_LEN = 80

# `_GEOM_COMPACT` lets a few wordy geom names render shorter in the
# filename without sacrificing readability. Anything not listed falls
# back to the geom name unchanged.
_GEOM_COMPACT: Dict[str, str] = {
    'histogram': 'hist',
}


def _filename_sanitise(token: Optional[str]) -> str:
    """Strip to `[A-Za-z0-9_-]`, collapse repeated underscores, trim edges."""
    if token is None:
        return ''
    cleaned = _re.sub(r'[^A-Za-z0-9_\-]+', '_', str(token))
    cleaned = _re.sub(r'_+', '_', cleaned).strip('_')
    return cleaned


def _subset_summary(
    filters: Optional[Dict[str, object]],
    *,
    max_value_len: int = 60,
) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """Summarise a ``filters`` dict for title / filename.

    Returns ``(title_clause, filename_token, first_key)``:

    * ``title_clause`` — short human suffix like ``"subset patient=X"`` or
      ``"subset patient=X (+2 more)"`` (no leading separator).
    * ``filename_token`` — sanitised ``"key_value"`` form for the first
      subset clause (or just the key if the value is a list).
    * ``first_key`` — the first key in the filters dict (or None).
    """
    if not filters:
        return None, None, None
    keys = list(filters.keys())
    first_key = keys[0]
    raw_value = filters[first_key]
    # Render a representative value for the first clause.
    if isinstance(raw_value, (list, tuple, set)):
        items = list(raw_value)
        if len(items) == 1:
            value_text = str(items[0])
        else:
            value_text = f"{items[0]}+{len(items) - 1}"
    else:
        value_text = str(raw_value)
    if len(value_text) > max_value_len:
        value_text = value_text[:max_value_len] + '...'
    title_clause = f"subset {first_key}={value_text}"
    if len(keys) > 1:
        title_clause += f" (+{len(keys) - 1} more)"
    filename_token = _filename_sanitise(f"{first_key}_{value_text}")
    return title_clause, filename_token, first_key


def _first_token(value: Optional[str]) -> Optional[str]:
    """Pick the first comma-separated token of a string param (e.g. for
    heatmap's ``x`` which is a comma-list of genes)."""
    if not value:
        return None
    parts = [p.strip() for p in str(value).split(',') if p.strip()]
    return parts[0] if parts else None


def _split_tokens(value: Optional[str], limit: int = 3) -> List[str]:
    """Return up to ``limit`` non-empty comma-separated tokens."""
    if not value:
        return []
    parts = [p.strip() for p in str(value).split(',') if p.strip()]
    return parts[:limit]


def build_plot_descriptor(
    *,
    geom: str,
    db_id: str,
    x: Optional[str] = None,
    x_kind: Optional[str] = None,
    y: Optional[str] = None,
    y_kind: Optional[str] = None,
    color: Optional[str] = None,
    color_kind: Optional[str] = None,
    filters: Optional[Dict[str, object]] = None,
    # DE-driven geom selectors
    de_study: Optional[str] = None,
    de_term: Optional[str] = None,
    de_contrast: Optional[str] = None,
    de_gene: Optional[str] = None,
    # heatmap-specific: comma-list of genes (in `x`); precomputed y_resolved label.
    y_resolved: Optional[str] = None,
) -> Dict[str, str]:
    """Compose a biology-keyed title and filename for a plot response.

    Inputs are the *biology* knobs of a plot request — what's plotted (geom),
    what dataset (db_id), which fields are on which axis, which fields drive
    colour, what subset filter was applied, and (for DE geoms) which study /
    term / contrast was selected.

    Returns ``{'title': ..., 'filename': ..., 'filename_truncated': '0'|'1'}``.

    Both strings are pure ASCII; filename is `[A-Za-z0-9_-]` only, no
    extension, snake_case, capped at ``_FILENAME_MAX_LEN`` characters.

    See ``doc/API_CHANGELOG.md`` (2.9.0 entry) for the per-geom title
    templates and filename recipes.
    """
    # ── TITLE ──────────────────────────────────────────────────────────────
    # All title strings begin with "{db_id}: " — the dataset is the single
    # most identifying biology fact about a plot.
    prefix = f"{db_id}: "

    # XY-mode (continuous x AND y) wins over embedding-mode for
    # scatter/hexbin — same rule the handler uses.
    is_xy = (
        geom in ('scatter', 'hexbin', 'xy_scatter', 'xy_hexbin')
        and x_kind in ('gene', 'numerical')
        and y is not None
    )

    if geom in ('scatter', 'xy_scatter') and is_xy:
        body = f"{y} vs {x}"
        if color:
            body += f" coloured by {color}"
    elif geom in ('hexbin', 'xy_hexbin') and is_xy:
        body = f"{y} vs {x}"
        if color and color_kind == 'gene':
            body += f", mean {color} per hex"
        elif color:
            body += f" coloured by {color}"
        else:
            body += " (hex density)"
    elif geom == 'scatter':
        embedding = x or 'embedding'
        if not color:
            body = f"cells on {embedding}"
        elif color_kind == 'gene':
            body = f"{color} expression on {embedding}"
        else:
            body = f"cells by {color} on {embedding}"
    elif geom == 'hexbin':
        embedding = x or 'embedding'
        if not color:
            body = f"cell density on {embedding}"
        elif color_kind == 'gene':
            body = f"mean {color} on {embedding}"
        elif color_kind == 'categorical':
            body = f"modal {color} on {embedding}"
        else:
            # numerical obs colour
            body = f"mean {color} on {embedding}"
    elif geom == 'kde2d':
        body = f"density of {y} vs {x}"
    elif geom in ('violin', 'box'):
        if y:
            body = f"{y} per {x}"
        else:
            body = f"cell counts per {x}"
    elif geom == 'bar':
        if y:
            body = f"mean {y} per {x}"
        else:
            body = f"cell counts per {x}"
    elif geom in ('histogram', 'ecdf', 'kde'):
        body = f"{x} distribution"
    elif geom == 'heatmap':
        # `x` is a comma-list of genes; y is the categorical row axis.
        x_tokens = _split_tokens(x, limit=3)
        if x_tokens:
            gene_label = ','.join(x_tokens)
            if x and ',' in x and len(x_tokens) < len([p for p in x.split(',') if p.strip()]):
                gene_label += '+'
        else:
            gene_label = 'top genes'
        row_label = y_resolved or y or 'group'
        body = f"expression heatmap of {gene_label} per {row_label}"
    elif geom == 'rolling':
        body = f"{y} along {x}"
    elif geom == 'volcano':
        term = de_term or '?'
        contrast = de_contrast or '?'
        body = f"{term} / {contrast} (volcano)"
    elif geom == 'ma':
        term = de_term or '?'
        contrast = de_contrast or '?'
        body = f"{term} / {contrast} (MA)"
    elif geom == 'forest':
        gene = de_gene or '?'
        if de_term and not de_contrast and not de_study:
            body = f"{gene} in {de_term}"
        else:
            body = f"{gene} across studies"
    elif geom == 'de_heatmap':
        term = de_term or '?'
        body = f"DE matrix - {term}"
    else:
        body = geom

    # Subset modifier (keep short — one clause).
    subset_clause, subset_filename_token, _first_key = _subset_summary(filters)
    title = prefix + body
    if subset_clause:
        title += f" - {subset_clause}"

    # ── FILENAME ───────────────────────────────────────────────────────────
    db_id_safe = _filename_sanitise(db_id.replace('/', '_'))
    geom_compact = _GEOM_COMPACT.get(geom, geom)
    bio_tokens: List[str] = []

    if geom in ('scatter', 'hexbin', 'xy_scatter', 'xy_hexbin') and is_xy:
        bio_tokens.append(f"{_filename_sanitise(y)}_vs_{_filename_sanitise(x)}")
        if color:
            bio_tokens.append(_filename_sanitise(color))
    elif geom in ('scatter', 'hexbin'):
        if color:
            bio_tokens.append(_filename_sanitise(color))
        else:
            bio_tokens.append('density' if geom == 'hexbin' else 'cells')
    elif geom == 'kde2d':
        bio_tokens.append(f"{_filename_sanitise(y)}_vs_{_filename_sanitise(x)}")
    elif geom in ('violin', 'box', 'bar'):
        if y:
            bio_tokens.append(f"{_filename_sanitise(y)}_per_{_filename_sanitise(x)}")
        else:
            bio_tokens.append(f"counts_per_{_filename_sanitise(x)}")
    elif geom in ('histogram', 'ecdf', 'kde'):
        if x:
            bio_tokens.append(_filename_sanitise(x))
    elif geom == 'heatmap':
        x_tokens = _split_tokens(x, limit=3)
        if x_tokens:
            bio_tokens.append('_'.join(_filename_sanitise(t) for t in x_tokens))
        elif y_resolved or y:
            bio_tokens.append(_filename_sanitise(y_resolved or y))
    elif geom == 'rolling':
        if y and x:
            y_tokens = _split_tokens(y, limit=2)
            y_part = '_'.join(_filename_sanitise(t) for t in y_tokens) if y_tokens else _filename_sanitise(y)
            bio_tokens.append(f"{y_part}_along_{_filename_sanitise(x)}")
    elif geom in ('volcano', 'ma'):
        if de_term:
            bio_tokens.append(_filename_sanitise(de_term))
        if de_contrast:
            bio_tokens.append(_filename_sanitise(de_contrast))
    elif geom == 'forest':
        if de_gene:
            bio_tokens.append(_filename_sanitise(de_gene))
    elif geom == 'de_heatmap':
        if de_term:
            bio_tokens.append(_filename_sanitise(de_term))

    if subset_filename_token:
        bio_tokens.append(subset_filename_token)

    # Drop empties; de-dupe consecutive identical tokens (e.g. heatmap when
    # the gene list happens to equal the y label).
    bio_tokens = [t for t in bio_tokens if t]

    filename_parts: List[str] = [db_id_safe, _filename_sanitise(geom_compact)]
    filename_parts.extend(bio_tokens)
    filename = '_'.join(p for p in filename_parts if p)
    # Final cleanup: any double-underscores produced by joining empty parts.
    filename = _re.sub(r'_+', '_', filename).strip('_')

    truncated = False
    if len(filename) > _FILENAME_MAX_LEN:
        filename = filename[:_FILENAME_MAX_LEN].rstrip('_-')
        truncated = True

    # ASCII-safe by construction (sanitiser strips non-ASCII), but stay
    # defensive for the title — the X-Plot-Title HTTP header is latin-1.
    title_ascii = title.encode('latin-1', errors='replace').decode('latin-1')

    return {
        'title': title_ascii,
        'filename': filename,
        'filename_truncated': '1' if truncated else '0',
    }


# Above this many cells, matplotlib's ax.scatter() becomes the dominant cost
# of a scatter render (it builds a PathCollection of n offsets and renders
# each marker individually).  ax.plot() with marker='.' is 5-10x faster
# because matplotlib batches all markers behind a single draw_markers call.
# The tradeoff: ax.plot() can't do per-point colour, so we only use it for
# no-colour scatters and (per-group) categorical scatters.  Continuous
# colour still needs ax.scatter for the cmap.
_FAST_SCATTER_THRESHOLD = 50_000


# ── axis transforms ───────────────────────────────────────────────────────────
#
# Used by /plot/scatter (XY mode), /plot/hexbin (XY mode), and /plot/kde2d.
# Picking the right transform is what makes a module-vs-module scatter
# (e.g. MEbrown vs MEmagenta on a 235k-cell dataset) readable instead of
# a single dense blob — the densest few % of cells otherwise wash out the
# rest of the distribution.
#
# All transforms are defined on every real (including negatives).
# Composed with the X / Y axes independently — you can rank one and asinh
# the other.

TRANSFORMS = ('linear', 'log', 'log1p', 'signed_log1p', 'asinh',
              'signed_sqrt', 'rank')


def apply_axis_transform(values: 'np.ndarray', kind: str,
                         asinh_scale: float = 1.0) -> 'np.ndarray':
    """Apply a numerical transform to a 1-D array.

    Args:
        values:      a 1-D numpy array of floats.
        kind:        one of ``TRANSFORMS``.
        asinh_scale: rescaling factor for asinh (``y = asinh(x / s)``).
                     Defaults to 1.0; lower s flattens more aggressively.
                     Ignored for non-asinh transforms.

    Returns:
        a 1-D numpy array of the same shape.

    Notes on each kind:

    * ``linear``       — identity, for completeness.
    * ``log``          — natural log; values ≤ 0 become NaN (caller filters).
    * ``log1p``        — ``log(1 + x)``; values < -1 become NaN.
    * ``signed_log1p`` — ``sign(x) * log1p(|x|)``. Symmetric, defined for
                         all reals, fast (just abs + log1p). Has a kink in
                         the second derivative at 0 but visually identical
                         to asinh on the value ranges used in single-cell
                         work. Recommended default for symmetric data.
    * ``asinh``        — ``asinh(x / s)``. Smooth (C∞ at 0) symmetric. The
                         CyTOF / flow-cytometry standard. Slightly slower
                         than signed_log1p, indistinguishable visually.
    * ``signed_sqrt``  — ``sign(x) * sqrt(|x|)``. Gentler compression than
                         the log family — good when you want to preserve
                         relative magnitude.
    * ``rank``         — percentile rank in [0, 1]. Gives every cell equal
                         visual weight; obliterates magnitude information.
                         Useful for spotting modal structure.
    """
    if kind not in TRANSFORMS:
        raise ValueError(f"unknown transform {kind!r}. Valid: {TRANSFORMS}")
    if kind == 'linear':
        return values
    out = np.asarray(values, dtype=float)
    if kind == 'log':
        return np.where(out > 0, np.log(np.maximum(out, np.finfo(float).tiny)),
                        np.nan)
    if kind == 'log1p':
        return np.where(out > -1, np.log1p(np.maximum(out, -1 + 1e-300)),
                        np.nan)
    if kind == 'signed_log1p':
        return np.sign(out) * np.log1p(np.abs(out))
    if kind == 'asinh':
        return np.arcsinh(out / max(asinh_scale, 1e-12))
    if kind == 'signed_sqrt':
        return np.sign(out) * np.sqrt(np.abs(out))
    # rank → percentile in [0, 1].  np.argsort.argsort gives the ranks;
    # divide by (n-1) to scale to [0, 1].  Stable on ties (uses positional).
    n = out.size
    if n == 0:
        return out
    finite = np.isfinite(out)
    ranks = np.empty(n, dtype=float)
    ranks.fill(np.nan)
    if finite.any():
        order = np.argsort(out[finite], kind='stable')
        rk = np.empty(int(finite.sum()), dtype=float)
        rk[order] = np.arange(int(finite.sum()))
        ranks[finite] = rk / max(int(finite.sum()) - 1, 1)
    return ranks


def _axis_label_with_transform(field: str, kind: str) -> str:
    """Compose an axis label that reflects the applied transform."""
    if kind in (None, 'linear'):
        return field
    if kind == 'signed_log1p':
        return f'sign(x) · log1p(|{field}|)'
    if kind == 'log1p':
        return f'log1p({field})'
    if kind == 'log':
        return f'log({field})'
    if kind == 'asinh':
        return f'asinh({field})'
    if kind == 'signed_sqrt':
        return f'sign(x) · √|{field}|'
    if kind == 'rank':
        return f'rank({field}) [0..1]'
    return f'{kind}({field})'


def _fast_scatter(ax, xs, ys, *, point_size: float, alpha: float,
                  color, label: Optional[str] = None) -> None:
    """Single-colour scatter via ax.plot — 5-10x faster than ax.scatter for
    big embeddings.  ``point_size`` is matplotlib's ``s`` (area in pt²);
    converted here to ``markersize`` (diameter in pt) so callers get the
    same visual marker size as the slow path.
    """
    import math as _math
    markersize = _math.sqrt(point_size) if point_size > 0 else 1.0
    ax.plot(
        xs, ys,
        marker='.', linestyle='none',
        markersize=markersize, alpha=alpha,
        color=color, label=label,
        rasterized=True,  # keeps SVG/PDF outputs reasonable on huge embeddings
    )


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
    palette_cat: str = _DEFAULT_CATEGORICAL,
    palette_cont: str = _DEFAULT_SEQUENTIAL,
    palette_div: str = _DEFAULT_DIVERGENT,
    color_scale: Optional[str] = None,
    vmin: Optional[float] = None,           # colorbar clip — None = auto
    vmax: Optional[float] = None,
    title_extra: Optional[str] = None,
    caption: Optional[str] = None,
    source_url: Optional[str] = None,
    marginals: bool = False,
    regline: Optional[str] = None,
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
        marginals:    XY-mode overlay — univariate KDE strips on x and y.
        regline:      XY-mode overlay — ``'global'`` for one regression
                      line across all points, ``'by_group'`` to draw one
                      per categorical-colour group (cap 30).
        output_format: 'png' | 'svg' | 'pdf'.
        caption:      Figure caption embedded in PDF or passed via header.
    """
    _setup_style(font_family, font_size)
    fig, ax = plt.subplots(figsize=(width, height))

    x_col = embedding_df.columns[-2]
    y_col = embedding_df.columns[-1]
    plot_df = embedding_df.copy()

    n_cells = int(len(plot_df))
    use_fastpath = n_cells > _FAST_SCATTER_THRESHOLD

    if color_df is not None and color_field:
        color_values = color_df.set_index('cell_name')[color_field]
        plot_df[color_field] = color_values.reindex(plot_df['cell_name'].values).values

        if color_kind == 'categorical':
            # Defensive normalisation of the colour column. Two problems
            # this fixes for "dirty" obs columns (label strings + NaN):
            #   1. pandas StringArray + NaN: a true `<NA>` survives astype(str)
            #      as a special NA object, so `sorted(values.unique())` fails
            #      with TypeError('< not supported between str and float').
            #   2. seaborn's slow path sorts hue categories internally and
            #      explodes the same way.
            # Solution: fillna('(missing)') before astype(str). Cells with
            # NA values stay in the plot under an explicit "(missing)"
            # category — honest about data quality without dropping cells.
            plot_df[color_field] = plot_df[color_field].fillna('(missing)').astype(str)
            n_cats = int(plot_df[color_field].nunique())
            cmap_name, _ = pick_cmap('categorical', palette_cat=palette_cat,
                                     palette_cont=palette_cont,
                                     palette_div=palette_div,
                                     n_categories=n_cats)
            if use_fastpath:
                # Per-group fastpath — one ax.plot per category, drastically
                # faster than the seaborn/scatter PathCollection for big N.
                xs_all = np.asarray(plot_df[x_col], dtype=float)
                ys_all = np.asarray(plot_df[y_col], dtype=float)
                cats = plot_df[color_field].values
                unique_cats = sorted(np.unique(cats[cats != 'nan']).tolist())
                cmap_obj = matplotlib.cm.get_cmap(cmap_name)
                n_palette = getattr(cmap_obj, 'N', 256)
                for idx, cat in enumerate(unique_cats):
                    mask = cats == cat
                    colour = cmap_obj((idx % n_palette) / max(n_palette - 1, 1))
                    _fast_scatter(
                        ax, xs_all[mask], ys_all[mask],
                        point_size=point_size, alpha=alpha,
                        color=colour, label=cat,
                    )
            else:
                sns.scatterplot(
                    data=plot_df, x=x_col, y=y_col,
                    hue=color_field, linewidth=0, s=point_size, alpha=alpha,
                    palette=cmap_name, ax=ax,
                )
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
                color_scale=color_scale,
            )
            # Continuous color requires ax.scatter — per-point cmap mapping
            # has no equivalent in ax.plot().  We rasterize so SVG/PDF
            # output doesn't explode on big embeddings.
            sc_kwargs: dict = {
                'c': color_array,
                'cmap': cmap_name,
                's': point_size,
                'alpha': alpha,
                'linewidths': 0,
                'rasterized': use_fastpath,
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
        if use_fastpath:
            _fast_scatter(
                ax,
                np.asarray(plot_df[x_col], dtype=float),
                np.asarray(plot_df[y_col], dtype=float),
                point_size=point_size, alpha=alpha,
                color='steelblue',
            )
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

    # XY-mode overlays.  ``regline`` + ``marginals`` apply uniformly to
    # scatter / hexbin / kde2d; the caller (typically _handle_xy) is the
    # one that decides to set them (embedding mode usually skips these).
    if regline or marginals:
        _xs = np.asarray(plot_df[x_col], dtype=float)
        _ys = np.asarray(plot_df[y_col], dtype=float)
        if regline:
            groups = None
            if regline == 'by_group' and color_df is not None and color_kind == 'categorical':
                groups = plot_df[color_field].astype(str).values
            attach_regression(ax, _xs, _ys, groups=groups, kind=regline,
                              palette_cat=palette_cat)
        if marginals:
            attach_marginals(fig, ax, _xs, _ys, color='#666666')

    return _render_figure(fig, dpi, output_format, caption, source_url)


def _cbar_extend(mappable, vmin, vmax) -> str:
    """Colorbar `extend` cue: an arrowhead only on a side that *genuinely* clips.

    Compares the requested vmin/vmax against the mappable's actual data range,
    so a no-op clip — e.g. vmin at the 0th percentile and vmax at the 100th, or
    symmetric divergent limits that bracket the data — shows no arrows. The
    triangle should only signal that values fall outside the colour range.

    Returns one of 'both' | 'min' | 'max' | 'neither'.
    """
    arr = mappable.get_array()
    if arr is None or len(arr) == 0:
        return 'neither'
    finite = np.asarray(arr, dtype=float)
    finite = finite[np.isfinite(finite)]
    if finite.size == 0:
        return 'neither'
    data_min = float(np.min(finite))
    data_max = float(np.max(finite))
    span = data_max - data_min
    tol = span * 1e-9 if span > 0 else 1e-9
    clip_lo = vmin is not None and vmin > data_min + tol
    clip_hi = vmax is not None and vmax < data_max - tol
    if clip_lo and clip_hi:
        return 'both'
    if clip_lo:
        return 'min'
    if clip_hi:
        return 'max'
    return 'neither'


def _draw_hex_silhouette(ax, offsets, hex_polygon, *,
                        color: str = 'black',
                        linewidth: float = 0.6) -> None:
    """Trace ONE outline around the union of all filled hexes.

    Replaces the old per-hex `edgecolors='black'` styling. Each
    connected region of filled hexes gets its own outline; internal
    holes (gaps inside the filled blob) get their own inner outline.

    Args:
        ax:          matplotlib Axes to draw on.
        offsets:     (n_hex, 2) array of hex CENTRES (already filtered
                     to only the hexes that should be drawn — i.e.
                     after `mincnt` / `used`).
        hex_polygon: (k, 2) vertex array of ONE hex polygon in the
                     "marker" frame (centred at the origin, in data
                     units). Same shape for every hex.  Typically the
                     value of ``hb.get_paths()[0].vertices``.
        color:       outline colour.  Default black.
        linewidth:   outline width in points.  0.6 reads well at the
                     150-dpi server default without dominating.

    Degenerate cases (no filled hexes, or a union that resolves to a
    Point / LineString because hexes only touch at corners) are
    silently skipped — the hex fill is still there, just unframed.
    """
    if offsets is None or len(offsets) == 0:
        return
    try:
        from shapely.geometry import Polygon
        from shapely.ops import unary_union
    except ImportError:
        # Pure soft-fail: if shapely isn't available, just skip the
        # silhouette.  The hexes still render — caller already added
        # them to the Axes.
        return

    # Buffer each hex outward by ~2 % of the hex apothem before
    # unioning.  Without it, neighbouring hexes only touch along an
    # edge — `unary_union` then leaves hairline holes wherever two
    # hexes share a vertex with sub-ulp coordinate jitter, and the
    # silhouette traces every one of those false holes as an internal
    # grid line.  A small buffer is visually invisible (much smaller
    # than the 0.6-point stroke width) but reliably fuses neighbours.
    # We then erode by the same amount on the union so the exterior
    # boundary lands back on the true hex edge.
    hex_extent = float(max(np.abs(hex_polygon).max(), 1e-12))
    buffer_amount = hex_extent * 0.02

    polys = [
        Polygon([(cx + dx, cy + dy) for (dx, dy) in hex_polygon])
        .buffer(buffer_amount, join_style=2)
        for cx, cy in offsets
    ]
    if not polys:
        return
    union = unary_union(polys)
    if union.is_empty:
        return
    # Erode back so the exterior outline lands on the true hex edge.
    union = union.buffer(-buffer_amount, join_style=2)
    if union.is_empty:
        return

    def _stroke(geom) -> None:
        if geom.geom_type != 'Polygon':
            return  # skip Point / LineString edge cases
        exterior_x, exterior_y = geom.exterior.xy
        ax.plot(exterior_x, exterior_y,
                color=color, linewidth=linewidth,
                solid_capstyle='round', solid_joinstyle='round',
                zorder=3)
        for interior in geom.interiors:
            interior_x, interior_y = interior.xy
            ax.plot(interior_x, interior_y,
                    color=color, linewidth=linewidth,
                    solid_capstyle='round', solid_joinstyle='round',
                    zorder=3)

    if union.geom_type == 'Polygon':
        _stroke(union)
    elif union.geom_type == 'MultiPolygon':
        for geom in union.geoms:
            _stroke(geom)
    # GeometryCollection / Point / LineString → nothing useful to draw.


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
    palette_cat: str = _DEFAULT_CATEGORICAL,
    palette_cont: str = _DEFAULT_SEQUENTIAL,
    palette_div: str = _DEFAULT_DIVERGENT,
    color_scale: Optional[str] = None,
    gridsize: int = 60,
    mincnt: int = 1,
    alpha: float = 1.0,
    vmin: Optional[float] = None,                 # colorbar clip — None = auto
    vmax: Optional[float] = None,
    vmin_quantile: Optional[float] = None,        # auto-clip per-hex value at quantile
    vmax_quantile: Optional[float] = None,        # ignored when vmin/vmax is set explicitly
    marginals: bool = False,                       # XY-mode overlay
    regline: Optional[str] = None,                 # 'global' | 'by_group'
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

    n_cells = int(len(embedding_df))

    if color_df is None or not color_field:
        # Density only — sequential, count of cells per hex.
        cmap_name, _ = pick_cmap('continuous',
                                 palette_cat=palette_cat,
                                 palette_cont=palette_cont,
                                 palette_div=palette_div)
        # First pass: compute per-hex counts WITHOUT drawing, so we can
        # auto-clip the colour scale at the requested quantiles before the
        # real draw.  This is what makes a 235k-cell density readable —
        # otherwise the densest few hexes wash everything else out.
        clip_used = None
        if (vmin_quantile is not None or vmax_quantile is not None) \
           and vmin is None and vmax is None:
            probe = ax.hexbin(xs, ys, gridsize=gridsize, mincnt=mincnt,
                              cmap=cmap_name, visible=False, linewidths=0)
            counts = probe.get_array()
            if counts is not None and counts.size > 0:
                lo, hi = _quantile_clip(
                    np.asarray(counts, dtype=float),
                    lo=vmin_quantile if vmin_quantile is not None else 0.0,
                    hi=vmax_quantile if vmax_quantile is not None else 1.0,
                )
                if vmin_quantile is not None: vmin = lo
                if vmax_quantile is not None: vmax = hi
                clip_used = (vmin_quantile, vmax_quantile)
        # No per-hex border — a single silhouette is drawn after the
        # fill (see _draw_hex_silhouette) so the eye sees the shape of
        # the data, not a grid.
        # antialiased=False kills the AA seam between adjacent hexes;
        # otherwise even edgecolors='none' shows a ghost grid because
        # matplotlib softens the polygon edges.
        hb_kwargs: dict = dict(gridsize=gridsize, mincnt=mincnt,
                               cmap=cmap_name,
                               edgecolors='none', linewidths=0,
                               antialiaseds=False)
        if vmin is not None: hb_kwargs['vmin'] = vmin
        if vmax is not None: hb_kwargs['vmax'] = vmax
        hb = ax.hexbin(xs, ys, **hb_kwargs)
        # One outline around the union of all filled hexes.
        _hex_paths = hb.get_paths()
        if _hex_paths:
            _draw_hex_silhouette(ax, hb.get_offsets(),
                                 _hex_paths[0].vertices)
        # Arrowhead only on a side that genuinely clips — a clip at the 0th/100th
        # percentile (or vmin/vmax outside the data range) caps nothing, so no
        # triangle there.
        cbar_extend = _cbar_extend(hb, vmin, vmax)
        cbar = plt.colorbar(hb, ax=ax, label='cell count', shrink=0.8,
                            extend=cbar_extend)
        title_bits = ['density', f'n={_fmt_n(n_cells)}', f'gridsize={gridsize}']
        if clip_used and cbar_extend != 'neither':   # don't advertise a no-op clip
            lo_q, hi_q = clip_used
            title_bits.append(
                f'clip {int((lo_q or 0)*100)}–{int((hi_q or 1)*100)}%'
            )
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
            color_scale=color_scale,
        )
        # First pass: probe the per-hex MEAN distribution so we can auto-clip
        # at the requested quantiles before the real draw. Mirrors the
        # density-mode logic — without this, one outlier hex (mean=12 while
        # everything else is 0–2) washes out the colourmap. Ignored when
        # vmin or vmax is set explicitly.
        clip_used = None
        if (vmin_quantile is not None or vmax_quantile is not None) \
           and vmin is None and vmax is None:
            probe = ax.hexbin(xs, ys, C=cvals, reduce_C_function=np.mean,
                              gridsize=gridsize, mincnt=mincnt,
                              cmap=cmap_name, visible=False, linewidths=0)
            per_hex = probe.get_array()
            if per_hex is not None and per_hex.size > 0:
                arr = np.asarray(per_hex, dtype=float)
                finite_means = arr[np.isfinite(arr)]
                if finite_means.size > 0:
                    lo, hi = _quantile_clip(
                        finite_means,
                        lo=vmin_quantile if vmin_quantile is not None else 0.0,
                        hi=vmax_quantile if vmax_quantile is not None else 1.0,
                    )
                    if vmin_quantile is not None: vmin = lo
                    if vmax_quantile is not None: vmax = hi
                    clip_used = (vmin_quantile, vmax_quantile)
        hb_kwargs: dict = {
            'C': cvals,
            'reduce_C_function': np.mean,
            'gridsize': gridsize,
            'mincnt': mincnt,
            'cmap': cmap_name,
            'alpha': alpha,
            # No per-hex border — one silhouette drawn after the fill.
            # antialiased=False suppresses the AA seam between hexes
            # that otherwise leaves a visible grid.
            'edgecolors': 'none',
            'linewidths': 0,
            'antialiaseds': False,
        }
        if used_kind == 'divergent':
            finite = cvals[np.isfinite(cvals)]
            vlim = float(np.nanmax(np.abs(finite))) if finite.size else 1.0
            hb_kwargs['vmin'] = -vlim
            hb_kwargs['vmax'] = +vlim
        # Explicit user overrides (and quantile-derived ones) always win —
        # useful to clip outliers (e.g. one hex with mean expression 12
        # washes out the rest).
        if vmin is not None: hb_kwargs['vmin'] = vmin
        if vmax is not None: hb_kwargs['vmax'] = vmax
        hb = ax.hexbin(xs, ys, **hb_kwargs)
        # One outline around the union of all filled hexes.  hexbin
        # without mincnt-filtering uses ALL hexes including the empty
        # ones; filter to hexes that actually got a value (non-NaN
        # mean and mincnt-satisfied — matplotlib already drops sub-
        # mincnt hexes from the returned offsets/array).
        _hex_paths = hb.get_paths()
        if _hex_paths:
            _arr = hb.get_array()
            _offsets_filled = hb.get_offsets()
            if _arr is not None and len(_arr) == len(_offsets_filled):
                _finite_mask = np.isfinite(np.asarray(_arr, dtype=float))
                _offsets_filled = _offsets_filled[_finite_mask]
            _draw_hex_silhouette(ax, _offsets_filled,
                                 _hex_paths[0].vertices)
        label_kind = 'expression' if color_kind == 'gene' else 'value'
        label_suffix = ' (divergent)' if used_kind == 'divergent' else ''
        cbar_extend = _cbar_extend(hb, vmin, vmax)
        plt.colorbar(hb, ax=ax,
                     label=f'mean {color_field} ({label_kind}){label_suffix}',
                     shrink=0.8,
                     extend=cbar_extend)
        title_bits = [
            color_field,
            f'⬣ mean ({"div" if used_kind == "divergent" else "seq"})',
            f'n={_fmt_n(n_cells)}',
        ]
        if clip_used and cbar_extend != 'neither':   # don't advertise a no-op clip
            lo_q, hi_q = clip_used
            title_bits.append(
                f'clip {int((lo_q or 0)*100)}–{int((hi_q or 1)*100)}%'
            )
        ax.set_title(_compose_title(title_bits, extra=title_extra))

    else:
        # Categorical: modal label + dominance.  Done by hand because
        # matplotlib's hexbin doesn't natively aggregate categoricals.
        # Mirror plot_scatter's NaN policy: keep missing cells under
        # an explicit '(missing)' category instead of silently
        # dropping them (which would give different cell counts
        # between scatter and hexbin on the same data — QA review B6).
        cats = (
            color_df.set_index('cell_name')[color_field]
            .reindex(embedding_df['cell_name'].values)
            .fillna('(missing)').astype(str)
        )
        # Encode categories to integer codes so we can use hexbin's C param
        # to discover hex membership; the actual reduce is done after.
        unique_cats = sorted(pd.Series(cats).unique().tolist())
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
        dominance = np.zeros(0, dtype=float)   # filled in the else-branch; default keeps the title-block safe.
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
            # or sequential cmap, AND auto-promotes tab10 → tab20 when
            # >10 distinct categories would otherwise have colliding hues.
            cat_palette, _ = pick_cmap(
                'categorical',
                palette_cat=palette_cat,
                palette_cont=palette_cont,
                palette_div=palette_div,
                n_categories=len(unique_cats),
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
                # No per-hex border; a single silhouette around the
                # union of all used hexes is drawn after. The
                # silhouette is fully opaque (structural outline) even
                # though per-hex alpha varies with dominance.
                # antialiased=False suppresses the AA seam between
                # adjacent hexes that would otherwise read as a grid.
                edgecolors='none', linewidths=0,
                antialiaseds=False,
            )
            ax.add_collection(coll)
            ax.set_xlim(xs.min(), xs.max())
            ax.set_ylim(ys.min(), ys.max())

            # One outline around the union of all used hexes.
            _draw_hex_silhouette(
                ax,
                offsets[used],
                hex_polygon,
            )

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
        n_used_hex = int(used.sum())
        median_dom = (
            float(np.median(dominance[used]))
            if n_used_hex and dominance.size == used.size
            else 0.0
        )
        # "modal" = the most-frequent category in each hex.  "α=top₁/top₂"
        # = each hex's alpha tracks the dominance ratio (count of modal
        # class / count of runner-up), pushed through a sigmoid so a
        # fully mixed hex (ratio=1) gets α≈0.25 and a near-pure hex
        # (ratio≥5) gets α≈1.  "median dom=X" is the median ratio across
        # used hexes — high = sharp regional structure, near 1 = mixed.
        title_bits = [
            color_field,
            '⬣ modal class · α=top₁/top₂',
            f'{len(unique_cats)} grps',
            f'n={_fmt_n(n_cells)}',
            f'median top₁/top₂={median_dom:.1f}',
        ]
        ax.set_title(_compose_title(title_bits, extra=title_extra))

    ax.set_xlabel(x_label)
    ax.set_ylabel(y_label)
    ax.set_aspect('equal', adjustable='datalim')
    sns.despine(ax=ax)

    # XY-mode overlays (only meaningful for non-embedding hexbins).
    if regline or marginals:
        if regline:
            groups = None
            if regline == 'by_group' and color_df is not None and color_kind == 'categorical':
                groups = (color_df.set_index('cell_name')[color_field]
                          .reindex(embedding_df['cell_name'].values).astype(str).values)
            attach_regression(ax, xs, ys, groups=groups, kind=regline,
                              palette_cat=palette_cat)
        if marginals:
            attach_marginals(fig, ax, xs, ys, color='#666666')

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
    n_cells = int(len(data_df))
    title_bits = [x_field, geom, f'n={_fmt_n(n_cells)}']
    if hue:
        title_bits.append(f'split by {hue}')
    ax.set_title(_compose_title(title_bits))
    sns.despine(ax=ax)
    _legend_outside(ax)

    return _render_figure(fig, dpi, output_format, caption, source_url)


def _render_grouped_panel(
    ax,
    panel_df: pd.DataFrame,
    *,
    x_field: str,
    y_field: Optional[str],
    hue: Optional[str],
    cat_order: List,
    hue_order: Optional[List],
    geom: str,
    ci: bool,
    bar_mode: str,
    normalize: bool,
    cmap_obj,
    cmap_n: int,
    hue_colour_index: Optional[Dict],
    x_positions: Optional[List[float]] = None,
    group_by_labels: Optional[List[Tuple[str, float, float]]] = None,
) -> bool:
    """Render one panel (one axis) for plot_grouped.

    Returns True if this panel rendered as a stacked bar (so the caller
    knows to use the stack-mode labelling). When faceting, the caller
    feeds a per-panel subset of the data; cat_order/hue_order are SHARED
    across panels so x-positions and colours line up.

    hue_colour_index maps a hue level to a stable column index → colour.
    Built once across all facets; ensures the same hue value gets the
    same colour in every panel even if absent from one of them.

    x_positions: optional list of x-axis positions (same length as
        cat_order). When set, bars are placed at these positions
        instead of np.arange(len(cat_order)) — used by group_by to
        insert gaps between groups. Only honoured for the stacked-bar
        path; seaborn-driven paths ignore this (they manage their own
        positions internally).
    group_by_labels: optional [(group_label, x_center, gap_x), ...].
        When set, a group-label band is drawn under the x-axis with a
        vertical separator at each gap_x. Only meaningful in combination
        with x_positions.
    """
    is_stacked = (geom == 'bar' and hue is not None and bar_mode == 'stack')
    if is_stacked:
        if y_field is None:
            stack_mat = pd.crosstab(panel_df[x_field], panel_df[hue])
        else:
            stack_mat = (
                panel_df.groupby([x_field, hue], observed=True)[y_field]
                .mean().unstack(fill_value=0.0)
            )
        # Align to shared orderings (zero-fill missing rows / hue levels).
        stack_mat = stack_mat.reindex(index=cat_order, columns=hue_order, fill_value=0.0)
        if normalize:
            totals = stack_mat.sum(axis=1).replace(0, np.nan)
            stack_mat = stack_mat.div(totals, axis=0).fillna(0.0)
        xs = (np.asarray(x_positions, dtype=float)
              if x_positions is not None
              else np.arange(len(cat_order), dtype=float))
        bottom = np.zeros(len(cat_order), dtype=float)
        for col in hue_order:
            heights = stack_mat[col].to_numpy(dtype=float)
            idx = hue_colour_index[col] if hue_colour_index else 0
            colour = cmap_obj((idx % cmap_n) / max(cmap_n - 1, 1))
            ax.bar(xs, heights, bottom=bottom, width=0.8,
                   color=colour, edgecolor='white', linewidth=0.3,
                   label=str(col))
            bottom += heights
        ax.set_xticks(xs)
        ax.set_xticklabels(cat_order)
        if normalize:
            ax.set_ylabel('Proportion')
            ax.set_ylim(0, 1)
        else:
            ax.set_ylabel('Cell count' if y_field is None else f'mean {y_field}')
        _draw_group_band(ax, group_by_labels)
        return True

    if y_field is None:
        if hue:
            ct = pd.crosstab(panel_df[x_field], panel_df[hue])
            ct = ct.reindex(index=cat_order, columns=hue_order, fill_value=0)
            long = ct.reset_index().melt(id_vars=x_field, var_name=hue, value_name='count')
            sns.barplot(data=long, x=x_field, y='count', hue=hue,
                        order=cat_order, hue_order=hue_order, ax=ax)
        else:
            counts = panel_df[x_field].value_counts().reindex(cat_order, fill_value=0)
            sns.barplot(x=counts.index, y=counts.values, ax=ax)
        ax.set_ylabel('Cell count')
    elif geom == 'box':
        sns.boxplot(data=panel_df, x=x_field, y=y_field, hue=hue,
                    order=cat_order, hue_order=hue_order,
                    ax=ax, showfliers=False)
    elif geom == 'violin':
        sns.violinplot(data=panel_df, x=x_field, y=y_field, hue=hue,
                       order=cat_order, hue_order=hue_order, ax=ax)
    else:  # bar (dodge)
        errorbar = ('ci', 95) if ci else None
        sns.barplot(data=panel_df, x=x_field, y=y_field, hue=hue,
                    order=cat_order, hue_order=hue_order,
                    ax=ax, errorbar=errorbar)
    _draw_group_band(ax, group_by_labels)
    return False


def _draw_group_band(ax, group_by_labels) -> None:
    """Draw the separator lines for the group-label band.

    Only the SEPARATORS are drawn here (in-axes vertical dashes).
    The text labels are placed by ``_place_group_band_labels`` AFTER
    tight_layout, so they can sit below the rotated tick labels'
    measured extent without colliding with them (anthive-web #153).

    group_by_labels is [(label, x_center, gap_x), ...].
    """
    if not group_by_labels:
        return
    for i, (label, x_center, gap_x) in enumerate(group_by_labels):
        if i > 0:
            ax.axvline(x=gap_x, color='#888', linewidth=0.6,
                       linestyle='--', alpha=0.6)


def _place_group_band_labels(fig, ax, group_by_labels) -> None:
    """Place the group-band TEXT labels below the rotated x-tick labels.

    Measures the actual rendered bbox of the tick labels (post-tight-
    layout) and offsets each group label below that extent. Uses a
    blended transform: data coords for X (so labels stay centred under
    their group regardless of figure size), display coords for Y (so
    we land just under the rotated tick text).

    Called AFTER fig.tight_layout() so the tick labels have their
    final position; otherwise the measurement is wrong.
    """
    if not group_by_labels:
        return
    fig.canvas.draw()  # ensure renderer state is current
    renderer = fig.canvas.get_renderer()
    # Bottom of the x-axis label block (in display coords). Falls back
    # to the axis spine if there are no tick labels to measure.
    tick_labels = ax.get_xticklabels()
    if tick_labels:
        bboxes = [t.get_window_extent(renderer=renderer) for t in tick_labels
                  if t.get_text()]
        if bboxes:
            min_y_display = min(b.y0 for b in bboxes)
        else:
            min_y_display = ax.get_position().y0 * fig.bbox.height
    else:
        min_y_display = ax.get_position().y0 * fig.bbox.height
    # Drop another 6 points of padding under the tick labels.
    pad_pixels = 6 * fig.dpi / 72.0
    y_display = min_y_display - pad_pixels
    # Convert display Y → axes-fraction Y for portability across resize.
    _, y_axes = ax.transAxes.inverted().transform((0, y_display))
    for label, x_center, _gap_x in group_by_labels:
        ax.text(
            x_center, y_axes, label,
            transform=matplotlib.transforms.blended_transform_factory(
                ax.transData, ax.transAxes,
            ),
            ha='center', va='top', fontsize=8, fontweight='bold',
            clip_on=False,
        )


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
    bar_mode: str = 'dodge',
    normalize: bool = False,
    palette_cat: str = _DEFAULT_CATEGORICAL,
    facet_field: Optional[str] = None,
    group_by_field: Optional[str] = None,
) -> bytes:
    """Values grouped by a categorical field: bar (with CI), box, or violin.

    Args:
        data_df:   DataFrame with [cell_name, x_field, (y_field), (hue_field), (facet_field)].
        x_field:   Categorical column for grouping (X axis).
        y_field:   Gene or numerical column for Y axis. None → cell counts.
        hue_field: Optional second categorical column. With ``bar_mode='dodge'``
                   (default) bars are drawn side-by-side per (x × hue);
                   with ``bar_mode='stack'`` they're stacked along Y.
        facet_field: Optional 3rd categorical → one subplot panel per
                   facet level (small multiples). Cardinality cap on
                   facets is 8 panels; beyond that the API rejects the
                   request rather than rendering an unreadable grid.
                   X order, hue order, and colours are SHARED across
                   panels so they're directly comparable. Legend is
                   placed once, outside the rightmost panel.
        geom:      'bar' | 'box' | 'violin'.
        ci:        Show 95% confidence interval on bar plots (seaborn bootstrap).
                   Ignored in stacked mode (segments come from raw counts /
                   aggregates, not a bootstrap).
        bar_mode:  'dodge' (default, current behaviour) | 'stack'. Only
                   meaningful for geom='bar' with a hue_field set — otherwise
                   silently ignored.
        normalize: When ``bar_mode='stack'`` and ``y_field is None``, scale
                   each bar to 100% so segments show within-x composition
                   fractions instead of raw counts.
        output_format: 'png' | 'svg' | 'pdf'.
        caption:   Figure caption.
    """
    _setup_style(font_family, font_size)

    # Resolve the shared orderings from the FULL dataset (so panels
    # show the same x-positions / hue colours even when a panel is
    # missing some levels).
    hue = hue_field if hue_field and hue_field in data_df.columns else None
    hue_order = (data_df[hue].value_counts().index.tolist()
                 if hue else None)

    # Facet axis: only respected if the field is present in data_df.
    facet = (facet_field if facet_field and facet_field in data_df.columns
             else None)
    facet_levels = (data_df[facet].value_counts().index.tolist()
                    if facet else [None])

    # group_by: in-plot clustering of the x bars by a 3rd categorical.
    # Re-orders cat_order so all x-values in one group are adjacent
    # (groups themselves sorted by total count, descending; x within a
    # group sorted by count within-group, descending). Produces gap-
    # adjusted x_positions + a group-label band spec for the renderer.
    group_by = (group_by_field if group_by_field
                and group_by_field in data_df.columns else None)
    x_positions: Optional[List[float]] = None
    group_by_labels: Optional[List[Tuple[str, float, float]]] = None
    cat_order: List
    if group_by is not None:
        # Map every x value to its dominant group_by level (the level
        # with the most cells; deterministic if a tie).
        x_to_group = (
            data_df.groupby([x_field, group_by], observed=True)
            .size().reset_index(name='_n')
            .sort_values(['_n'], ascending=False)
            .drop_duplicates(x_field)
            .set_index(x_field)[group_by]
        )
        # Sort groups by descending total cell count.
        group_totals = data_df[group_by].value_counts()
        group_order = group_totals.index.tolist()
        # Sort x's within each group by descending count.
        x_counts = data_df[x_field].value_counts()
        cat_order = []
        x_positions = []
        group_by_labels = []
        gap = 0.6   # extra width between groups (in bar-position units)
        pos = 0.0
        for grp in group_order:
            xs_in_group = [
                x for x in x_counts.index
                if x_to_group.get(x) == grp
            ]
            if not xs_in_group:
                continue
            group_start = pos
            for x in xs_in_group:
                cat_order.append(x)
                x_positions.append(pos)
                pos += 1.0
            group_end = pos - 1.0
            label_center = (group_start + group_end) / 2.0
            # gap_x for the separator drawn BEFORE this group (mid-gap).
            sep_x = group_start - (gap / 2.0)
            group_by_labels.append((str(grp), label_center, sep_x))
            pos += gap   # gap between groups
    else:
        cat_order = data_df[x_field].value_counts().index.tolist()

    # Stable hue → colour index, computed once across the union of all
    # panels. For stacked bars we want the SAME donor line to get the
    # SAME colour in every Village panel.
    is_stacked_overall = (geom == 'bar' and hue is not None and bar_mode == 'stack')
    cmap_n = 0
    cmap_obj = None
    hue_colour_index: Optional[Dict] = None
    if hue is not None:
        # Reorder hue_order by descending column total (same logic as
        # the single-panel stack mode used previously — biggest at the
        # bottom of each stack).
        if is_stacked_overall:
            if y_field is None:
                col_totals = pd.crosstab(data_df[x_field], data_df[hue]).sum(axis=0)
            else:
                col_totals = (
                    data_df.groupby([hue], observed=True)[y_field].sum()
                )
            hue_order = col_totals.sort_values(ascending=False).index.tolist()
        cmap_name, _ = pick_cmap('categorical', palette_cat=palette_cat,
                                 n_categories=max(len(hue_order), 1))
        cmap_obj = matplotlib.cm.get_cmap(cmap_name)
        cmap_n = getattr(cmap_obj, 'N', 256)
        hue_colour_index = {h: i for i, h in enumerate(hue_order)}

    n_panels = len(facet_levels)
    # Lay out panels in a single row, share the y axis so heights compare.
    panel_width = max(2.5, width / max(n_panels, 1))
    fig, axes = plt.subplots(
        nrows=1, ncols=n_panels,
        figsize=(panel_width * n_panels, height),
        sharey=True, squeeze=False,
    )
    axes = axes[0]

    is_stacked = False
    for ax, facet_value in zip(axes, facet_levels):
        if facet_value is None:
            panel_df = data_df
        else:
            panel_df = data_df[data_df[facet] == facet_value]
        is_stacked = _render_grouped_panel(
            ax, panel_df,
            x_field=x_field, y_field=y_field, hue=hue,
            cat_order=cat_order, hue_order=hue_order,
            geom=geom, ci=ci,
            bar_mode=bar_mode, normalize=normalize,
            cmap_obj=cmap_obj, cmap_n=cmap_n,
            hue_colour_index=hue_colour_index,
            x_positions=x_positions, group_by_labels=group_by_labels,
        ) or is_stacked
        ax.set_xlabel(x_field)
        plt.setp(ax.get_xticklabels(), rotation=45, ha='right')
        if facet_value is not None:
            ax.set_title(f'{facet} = {facet_value}')
        sns.despine(ax=ax)
        # Per-panel legends produced by seaborn need to go away; only
        # the rightmost panel's legend is kept by _legend_outside below.
        leg = ax.get_legend()
        if leg is not None and ax is not axes[-1]:
            leg.remove()

    n_cells = int(len(data_df))
    title_bits = [
        f'{y_field} by {x_field}' if y_field else f'cells per {x_field}',
        ('bar (100% stacked)' if (is_stacked and normalize) else
         ('bar (stacked)' if is_stacked else geom)),
        f'n={_fmt_n(n_cells)}',
        f'{len(cat_order)} grps',
    ]
    if hue:
        title_bits.append(f'split by {hue}')
    if facet:
        title_bits.append(f'facet by {facet} ({n_panels} panels)')
    fig.suptitle(_compose_title(title_bits), y=1.02 if n_panels > 1 else 1.0)

    # One shared legend, outside the rightmost panel.
    _legend_outside(axes[-1], title=hue if (hue and (is_stacked or facet)) else None)
    fig.tight_layout()

    # group_by labels: placed AFTER tight_layout so we can measure the
    # rotated x-tick label extent and offset the band below it. Doing
    # this earlier collided with long rotated labels (anthive-web #153).
    # Reserve bottom margin first so tight_layout doesn't squash them.
    if group_by_labels:
        fig.subplots_adjust(bottom=0.30)
        for ax in axes:
            _place_group_band_labels(fig, ax, group_by_labels)

    return _render_figure(fig, dpi, output_format, caption, source_url)


def plot_heatmap(
    mean_expr_df: pd.DataFrame,
    width: float = 10,
    height: float = 6,
    dpi: int = 150,
    output_format: str = 'png',
    palette_cont: str = _DEFAULT_SEQUENTIAL,
    palette_div: str = _DEFAULT_DIVERGENT,
    color_scale: Optional[str] = None,
    zscore: bool = True,
    title_extra: Optional[str] = None,
    caption: Optional[str] = None,
    source_url: Optional[str] = None,
    font_family: Optional[str] = None,
    font_size: Optional[float] = None,
) -> bytes:
    """Heatmap of mean gene expression per category group.

    Args:
        mean_expr_df: index=category values, columns=gene names.
        zscore:       When True (default) z-score each gene column and render
                      with the divergent palette centred at 0 — highlights
                      relative differences across groups.  When False, plot
                      raw means with the sequential palette (palette_cont).
        color_scale:  Optional override: ``'sequential'`` forces a one-sided
                      ramp even with ``zscore=True``; ``'divergent'`` forces
                      a centred ramp even with raw means.
    """
    _setup_style(font_family, font_size)
    fig, ax = plt.subplots(figsize=(width, height))

    # color_scale override beats the zscore default. (zscore still
    # controls the data shown — divergent palette on raw means is
    # rarely useful but the user gets what they asked for.)
    if zscore:
        plot_df = (mean_expr_df - mean_expr_df.mean()) / (mean_expr_df.std() + 1e-9)
        cbar_label = 'z-score'
        title_lead = 'Mean expression (z-scored)'
        default_kind = 'divergent'
    else:
        plot_df = mean_expr_df
        cbar_label = 'mean expression'
        title_lead = 'Mean expression'
        default_kind = 'sequential'
    cmap_name, used_kind = pick_cmap(
        'continuous',
        palette_cont=palette_cont, palette_div=palette_div,
        values=plot_df.to_numpy(dtype=float),
        color_scale=color_scale or default_kind,
    )
    if used_kind == 'divergent':
        finite = plot_df.to_numpy(dtype=float)
        finite = finite[np.isfinite(finite)]
        vlim = float(np.nanmax(np.abs(finite))) if finite.size else 1.0
        vmin, vmax, center = -vlim, vlim, 0
    else:
        vmin, vmax, center = None, None, None

    sns.heatmap(
        plot_df, ax=ax,
        cmap=cmap_name, center=center,
        vmin=vmin, vmax=vmax,
        linewidths=0.3,
        yticklabels=True,
        xticklabels=True,
        cbar_kws={'label': cbar_label},
    )
    ax.set_xlabel('Gene')
    ax.set_ylabel('')
    plt.setp(ax.get_xticklabels(), rotation=45, ha='right', fontsize=8)
    n_groups, n_genes = mean_expr_df.shape
    title_bits = [title_lead, f'{n_groups} grps', f'{n_genes} genes']
    ax.set_title(_compose_title(title_bits, extra=title_extra))
    fig.tight_layout()

    return _render_figure(fig, dpi, output_format, caption, source_url)


def plot_rolling(
    data_df: pd.DataFrame,
    x_field: str,
    y_fields: List[str],
    color_field: Optional[str] = None,
    color_kind: str = 'categorical',
    window: int = 200,
    show_band: bool = False,
    width: float = 9,
    height: float = 5,
    dpi: int = 150,
    output_format: str = 'png',
    palette_cat: str = _DEFAULT_CATEGORICAL,
    palette_cont: str = _DEFAULT_SEQUENTIAL,
    palette_div: str = _DEFAULT_DIVERGENT,
    title_extra: Optional[str] = None,
    caption: Optional[str] = None,
    source_url: Optional[str] = None,
    font_family: Optional[str] = None,
    font_size: Optional[float] = None,
) -> bytes:
    """Rolling-window line plot along a continuous axis (e.g. pseudotime).

    For each ``y_field``:
      1. Sort cells by ``x_field`` ascending.
      2. Apply a rolling mean of ``window`` cells.
      3. Plot the smoothed line.

    With ``color_field`` (categorical), one line per group is drawn — cells
    are first split by group, then sorted+rolled within each.  Useful for
    comparing trajectories (e.g. wild-type vs. knockout along pseudotime).

    With ``show_band=True``, fills a translucent IQR (q25..q75) ribbon
    around the rolling mean — visualises within-window variability.

    Args:
        data_df:    DataFrame with [cell_name, x_field, *y_fields[, color_field]].
        x_field:    Continuous obs/gene field to use as the trajectory axis.
        y_fields:   List of continuous fields to plot (each becomes a line).
        color_field, color_kind: Optional categorical for per-group split.
        window:     Rolling window in cells (default 200).
        show_band:  Show IQR ribbon around each mean line (default False).
    """
    _setup_style(font_family, font_size)
    fig, ax = plt.subplots(figsize=(width, height))

    if not y_fields:
        raise ValueError("plot_rolling needs at least one y field")
    n_cells = int(len(data_df))
    # Auto-scale the window when the user is on the API default (200) but
    # the dataset is much larger — 200 cells out of 200k is so tight you
    # can't see anything.  Rule of thumb: 1% of the dataset, clamped to
    # [50, 10000] so we don't over- or under-smooth.  User-supplied
    # windows are still honoured.
    if window == 200 and n_cells > 5_000:
        window = max(50, min(10_000, n_cells // 100))
    eff_window = max(2, min(window, n_cells))

    # Decide line / band palette.
    if color_field and color_kind == 'categorical':
        # Multiple groups, each with potentially multiple y fields →
        # categorical palette for groups, dash style for y fields.
        cat_palette, _ = pick_cmap(
            'categorical',
            palette_cat=palette_cat,
            palette_cont=palette_cont,
            palette_div=palette_div,
            n_categories=int(data_df[color_field].nunique()),
        )
        cmap_obj = matplotlib.cm.get_cmap(cat_palette)
        groups = list(pd.Series(data_df[color_field]).dropna().unique())
        groups = sorted(groups, key=str)
        n_palette = getattr(cmap_obj, 'N', 256)
        dash_cycle = ['-', '--', ':', '-.']

        for gi, grp in enumerate(groups):
            sub = data_df[data_df[color_field] == grp].sort_values(x_field)
            if len(sub) < 2:
                continue
            colour = cmap_obj((gi % n_palette) / max(n_palette - 1, 1))
            x_vals = sub[x_field].to_numpy(dtype=float)
            for yi, y in enumerate(y_fields):
                rolled = (pd.Series(sub[y].to_numpy(dtype=float))
                          .rolling(eff_window, min_periods=1, center=True).mean())
                label = f'{y} · {grp}' if len(y_fields) > 1 else str(grp)
                ax.plot(x_vals, rolled, color=colour,
                        linestyle=dash_cycle[yi % len(dash_cycle)],
                        linewidth=1.4, label=label)
                if show_band:
                    series = pd.Series(sub[y].to_numpy(dtype=float))
                    q25 = series.rolling(eff_window, min_periods=1, center=True).quantile(0.25)
                    q75 = series.rolling(eff_window, min_periods=1, center=True).quantile(0.75)
                    ax.fill_between(x_vals, q25, q75, color=colour, alpha=0.15, linewidth=0)
    else:
        # No grouping — one line per y field on a single subset.
        ordered = data_df.sort_values(x_field)
        x_vals = ordered[x_field].to_numpy(dtype=float)
        cat_palette, _ = pick_cmap(
            'categorical',
            palette_cat=palette_cat,
            palette_cont=palette_cont,
            palette_div=palette_div,
            n_categories=len(y_fields),
        )
        cmap_obj = matplotlib.cm.get_cmap(cat_palette)
        n_palette = getattr(cmap_obj, 'N', 256)
        for yi, y in enumerate(y_fields):
            colour = cmap_obj((yi % n_palette) / max(n_palette - 1, 1))
            series = pd.Series(ordered[y].to_numpy(dtype=float))
            rolled = series.rolling(eff_window, min_periods=1, center=True).mean()
            ax.plot(x_vals, rolled, color=colour, linewidth=1.6, label=y)
            if show_band:
                q25 = series.rolling(eff_window, min_periods=1, center=True).quantile(0.25)
                q75 = series.rolling(eff_window, min_periods=1, center=True).quantile(0.75)
                ax.fill_between(x_vals, q25, q75, color=colour, alpha=0.18, linewidth=0)

    ax.set_xlabel(x_field)
    ax.set_ylabel('rolling mean')
    title_bits = [
        ', '.join(y_fields[:3]) + ('…' if len(y_fields) > 3 else ''),
        f'along {x_field}',
        f'window={eff_window}',
        f'n={_fmt_n(n_cells)}',
    ]
    if color_field:
        title_bits.append(f'split by {color_field}')
    ax.set_title(_compose_title(title_bits, extra=title_extra))
    sns.despine(ax=ax)
    _legend_outside(ax, title=color_field if color_field else 'y')

    return _render_figure(fig, dpi, output_format, caption, source_url)


# ── DE-driven geoms ───────────────────────────────────────────────────────────
#
# These plot pre-computed differential-expression results stored in the
# duckdb's __de table (see lib/de_storage.py and doc/DESIGN_var_and_de.md).
# Unlike the cell-level geoms above, the inputs here are model results,
# not raw cell expression — the caller has already done the heavy lifting
# (a Wilcoxon test, a DESeq2 fit, a GLMM, …) and just hands us the
# (effsize, pvalue, padj) per (study, term, contrast, gene).


def plot_de_heatmap(
    matrix_df: pd.DataFrame,
    *,
    value_label: str = 'effect',
    row_label: str = 'contrast',
    col_label: str = 'gene',
    sig_mask: Optional[pd.DataFrame] = None,
    width: float = 10,
    height: float = 6,
    dpi: int = 150,
    output_format: str = 'png',
    palette_cont: str = _DEFAULT_SEQUENTIAL,
    palette_div: str = _DEFAULT_DIVERGENT,
    color_scale: Optional[str] = None,
    annot: bool = False,
    title_extra: Optional[str] = None,
    caption: Optional[str] = None,
    source_url: Optional[str] = None,
    font_family: Optional[str] = None,
    font_size: Optional[float] = None,
) -> bytes:
    """Heatmap of pre-computed DE values: contrasts (rows) × genes/modules (cols).

    Renders Sierksma Fig 2E / 4B-style matrices directly from the
    ``__de`` table — no cell-level expression involved. Each cell is one
    (contrast, gene) effect size (or estimate / log2fc / zeff /
    p-value-derived score, depending on what the caller picked).

    Args:
        matrix_df:   DataFrame indexed by ``contrast``, columns are gene
                     / module names, values are the effect size to show.
                     Caller is responsible for the pivot.
        sig_mask:    Optional boolean DataFrame (same shape as matrix_df)
                     marking significant cells (padj <= threshold). When
                     provided, those cells get a small dot overlay.
        color_scale: 'auto' / 'sequential' / 'divergent'. Defaults to
                     divergent for effect-size data; pass 'sequential'
                     for one-sided value columns (e.g. -log10(padj)).
    """
    _setup_style(font_family, font_size)
    fig, ax = plt.subplots(figsize=(width, height))

    # Values used to pick the cmap family.
    values = matrix_df.to_numpy(dtype=float)
    cmap_name, used_kind = pick_cmap(
        'continuous',
        palette_cont=palette_cont,
        palette_div=palette_div,
        values=values,
        # DE effect-size heatmaps almost always want divergent unless
        # the user explicitly asked for one-sided.
        color_scale=color_scale or 'divergent',
    )
    # Symmetric vmin/vmax when divergent; raw when sequential.
    if used_kind == 'divergent':
        finite = values[np.isfinite(values)]
        vlim = float(np.nanmax(np.abs(finite))) if finite.size else 1.0
        vmin, vmax = -vlim, vlim
    else:
        vmin, vmax = None, None

    sns.heatmap(
        matrix_df,
        ax=ax,
        cmap=cmap_name,
        center=0.0 if used_kind == 'divergent' else None,
        vmin=vmin, vmax=vmax,
        annot=annot,
        fmt='.2f',
        annot_kws={'fontsize': 6},
        cbar_kws={'label': value_label, 'shrink': 0.8},
        linewidths=0.3,
        linecolor='white',
        square=False,
    )
    # Significant cells get a small dot overlay (mirrors Fig 4B's
    # "asterisk in significant cells" convention).
    if sig_mask is not None and sig_mask.shape == matrix_df.shape:
        ys, xs = np.where(sig_mask.to_numpy())
        # Cell centres are at integer + 0.5 in seaborn's heatmap coords.
        ax.scatter(xs + 0.5, ys + 0.5, marker='o',
                   s=6, c='black', linewidths=0, alpha=0.85)

    ax.set_xlabel(col_label)
    ax.set_ylabel(row_label)
    plt.setp(ax.get_xticklabels(), rotation=45, ha='right', fontsize=8)
    plt.setp(ax.get_yticklabels(), rotation=0, fontsize=8)

    n_rows, n_cols = matrix_df.shape
    title_bits = [
        f'DE: {n_rows} {row_label}s × {n_cols} {col_label}s',
        value_label,
    ]
    ax.set_title(_compose_title(title_bits, extra=title_extra))
    fig.tight_layout()
    return _render_figure(fig, dpi, output_format, caption, source_url)


def plot_volcano(
    de_df: pd.DataFrame,
    *,
    padj_threshold: float = 0.05,
    log2fc_threshold: float = 1.0,
    log2fc_clip: Optional[float] = None,        # symmetric clamp of the x-axis effect
    neglog10p_clip: Optional[float] = None,     # upper-bound clamp of -log10(padj)
    n_label: int = 20,
    width: float = 8,
    height: float = 6,
    dpi: int = 150,
    output_format: str = 'png',
    palette_cat: str = _DEFAULT_CATEGORICAL,
    title_extra: Optional[str] = None,
    caption: Optional[str] = None,
    source_url: Optional[str] = None,
    font_family: Optional[str] = None,
    font_size: Optional[float] = None,
) -> bytes:
    """Volcano plot — effect size on x, -log10(padj) on y, top genes labelled.

    The X-axis adapts to the data:

    - **log2fc data** (DESeq2, edgeR, scanpy rank_genes_groups, most GLMM
      ingests): plot ``log2fc``, label the axis "log₂ fold change".  The
      ``log2fc_threshold`` default of 1.0 is sensible (2-fold change).
    - **z-effect data** (zeff-only studies — e.g. module-DE on z-scaled
      eigengene scores, or some GLMM intercept terms): when ``log2fc``
      is all-NULL, plot ``effsize`` (which equals ``zeff`` here), label
      the axis "z-effect", and auto-zero ``log2fc_threshold`` since a
      ≥1 cutoff on z-scores in the ~1e-4 range would render an empty
      "significant" set.

    Args:
        de_df:           DataFrame with at least `gene_name`, `padj`, and
                         either `log2fc` or `effsize` populated.  ``efftype``
                         (when present) is used for the axis label.
        padj_threshold:  Significance line on y (horizontal dashed); rows
                         with padj <= this AND |effect| >= log2fc_threshold
                         are coloured as "significant".
        log2fc_threshold: Effect-size cutoff on x.  Default 1.0 is right
                          for log2fc data; auto-zeroed when the axis
                          falls back to z-effect.
        n_label:         Label this many top genes (smallest padj).
    """
    _setup_style(font_family, font_size)
    fig, ax = plt.subplots(figsize=(width, height))

    df = de_df.copy()

    # Pick the effect axis.  log2fc is the default; fall back to effsize
    # (= COALESCE(log2fc, zeff)) only when log2fc is unusable.  This is
    # what makes the plot Just Work on zeff-only studies (e.g. module
    # eigengene DE) instead of silently rendering empty axes.
    if df['log2fc'].notna().any():
        df['_effect'] = df['log2fc']
        x_label = 'log₂ fold change'
        axis_kind = 'log2fc'
    elif 'effsize' in df.columns and df['effsize'].notna().any():
        df['_effect'] = df['effsize']
        # Use the modal efftype for the axis label so "z-effect" /
        # "log2fc" / "score" all surface correctly.
        if 'efftype' in df.columns and df['efftype'].notna().any():
            kind = df['efftype'].mode().iat[0]
        else:
            kind = 'effect'
        x_label = {
            'zeff':   'z-effect',
            'log2fc': 'log₂ fold change',
        }.get(kind, f'effect ({kind})')
        axis_kind = kind
        # A fold-change-style threshold (≥1 = 2-fold) is meaningless on
        # z-scores in the 1e-4 range — would render an empty 'sig' set.
        # Zero the threshold by default; user can pass it explicitly if
        # they want.
        if log2fc_threshold == 1.0:    # the function's default value
            log2fc_threshold = 0.0
    else:
        # Neither log2fc nor effsize is usable.  Render an empty plot
        # with an explanatory message rather than crashing.
        ax.text(0.5, 0.5,
                'no log2fc or effsize values — check the study',
                transform=ax.transAxes, ha='center', va='center', color='#888')
        ax.set_xlabel('effect size')
        ax.set_ylabel('-log₁₀ adjusted p')
        return _render_figure(fig, dpi, output_format, caption, source_url)

    # Clip padj=0 to a tiny floor so -log10 doesn't blow up to inf.
    floor = max(1e-300, df['padj'].replace(0, np.nan).dropna().min() / 10
                if df['padj'].notna().any() else 1e-300)
    df['_padj_clipped'] = df['padj'].clip(lower=floor)
    df['_neglog10p'] = -np.log10(df['_padj_clipped'])

    # Optional visual clamps — pin outliers to a finite axis range so the
    # bulk of the distribution gets visual space.  Significance / labels
    # use the clamped values (i.e. the label rides at the clamp).
    clip_marks = []
    if log2fc_clip is not None and log2fc_clip > 0:
        df['_effect'] = df['_effect'].clip(-log2fc_clip, +log2fc_clip)
        clip_marks.append(f'|{axis_kind}|≤{log2fc_clip:g}')
    if neglog10p_clip is not None and neglog10p_clip > 0:
        df['_neglog10p'] = df['_neglog10p'].clip(upper=neglog10p_clip)
        clip_marks.append(f'-log₁₀p≤{neglog10p_clip:g}')
    df['_sig'] = (df['padj'] <= padj_threshold) & (df['_effect'].abs() >= log2fc_threshold)
    df['_direction'] = np.where(df['_effect'] > 0, 'up', 'down')
    df['_category'] = np.where(df['_sig'], df['_direction'], 'ns')

    cmap_obj = matplotlib.cm.get_cmap(
        palette_cat if palette_cat in _QUALITATIVE_PALETTES else _DEFAULT_CATEGORICAL
    )
    colors = {
        'ns':   '#bbbbbb',
        'down': cmap_obj(0.0),
        'up':   cmap_obj(0.1),
    }

    for cat in ('ns', 'down', 'up'):
        sub = df[df['_category'] == cat]
        if len(sub) == 0:
            continue
        label = {'ns': 'n.s.', 'down': 'sig ↓', 'up': 'sig ↑'}[cat]
        ax.plot(
            sub['_effect'], sub['_neglog10p'],
            marker='.', linestyle='none',
            markersize=2.5 if cat == 'ns' else 4.0,
            alpha=0.5 if cat == 'ns' else 0.9,
            color=colors[cat], label=label, rasterized=True,
        )

    # Reference line at x=0 — separates up- from down-regulated visually.
    # Same style as the MA / forest plots use for their zero lines, so
    # the three DE geoms read consistently.
    ax.axvline(0, color='#444', linewidth=0.7)

    # Threshold lines (dashed, lighter — distinct from the solid x=0).
    if padj_threshold and padj_threshold > 0:
        ax.axhline(-np.log10(padj_threshold), linestyle='--',
                   color='#888', linewidth=0.8)
    if log2fc_threshold and log2fc_threshold > 0:
        ax.axvline(+log2fc_threshold, linestyle='--', color='#888', linewidth=0.8)
        ax.axvline(-log2fc_threshold, linestyle='--', color='#888', linewidth=0.8)

    # Label top-N significant genes by lowest padj.  When many genes share
    # padj=0 (saturated clip), naive 2pt offsets stack them on top of each
    # other; adjustText repels them apart and draws thin leader lines.
    # Give it some headroom above the data ceiling so it has room to push
    # labels up + out — otherwise the iteration just rotates them around
    # the same crowded zone.
    if n_label > 0:
        labelled = df[df['_sig']].nsmallest(n_label, '_padj_clipped')
        y_max = float(df['_neglog10p'].max())
        ax.set_ylim(top=y_max * 1.12)
        x_lo, x_hi = float(df['_effect'].min()), float(df['_effect'].max())
        x_pad = max(0.6, 0.05 * (x_hi - x_lo))
        ax.set_xlim(x_lo - x_pad, x_hi + x_pad)
        texts = [
            ax.text(row['_effect'], row['_neglog10p'], str(row['gene_name']),
                    fontsize=7, alpha=0.9, ha='center', va='center')
            for _, row in labelled.iterrows()
        ]
        if len(texts) > 1:
            try:
                from adjustText import adjust_text  # type: ignore
                adjust_text(
                    texts, ax=ax,
                    arrowprops=dict(arrowstyle='-', color='#888', lw=0.4,
                                    alpha=0.7),
                    expand_points=(1.8, 2.2),
                    expand_text=(1.4, 1.6),
                    force_text=(0.8, 1.2),
                    force_points=(0.4, 0.6),
                    lim=1500,
                )
            except ImportError:
                pass  # fall-back: stacked labels are still readable enough

    ax.set_xlabel(x_label)
    ax.set_ylabel('-log₁₀ adjusted p')
    n_total = len(df)
    n_sig = int(df['_sig'].sum())
    thresh_label = (
        f'|{axis_kind}|≥{log2fc_threshold:g}' if log2fc_threshold and log2fc_threshold > 0
        else 'padj only'
    )
    title_bits = ['Volcano', f'{n_sig}/{n_total} sig (padj≤{padj_threshold:g}, {thresh_label})']
    if clip_marks:
        title_bits.append('clipped ' + ', '.join(clip_marks))
    ax.set_title(_compose_title(title_bits, extra=title_extra))
    sns.despine(ax=ax)
    _legend_outside(ax, title='significance')

    return _render_figure(fig, dpi, output_format, caption, source_url)


def plot_ma(
    de_df: pd.DataFrame,
    *,
    padj_threshold: float = 0.05,
    log2fc_threshold: float = 1.0,
    log2fc_clip: Optional[float] = None,        # symmetric clamp of the y-axis log2fc
    logmean_clip: Optional[float] = None,       # upper-bound clamp of log10(mean expression)
    n_label: int = 20,
    width: float = 8,
    height: float = 6,
    dpi: int = 150,
    output_format: str = 'png',
    palette_cat: str = _DEFAULT_CATEGORICAL,
    title_extra: Optional[str] = None,
    caption: Optional[str] = None,
    source_url: Optional[str] = None,
    font_family: Optional[str] = None,
    font_size: Optional[float] = None,
) -> bytes:
    """MA plot — mean expression on x (log10), log2fc on y.

    Args:
        de_df: DataFrame with at least `gene_name`, `log2fc`, `padj`,
               `base_mean` (or any "mean expression" column — caller
               renames before passing). Rows with NaN base_mean are
               dropped silently.
    """
    _setup_style(font_family, font_size)
    fig, ax = plt.subplots(figsize=(width, height))

    df = de_df.copy().dropna(subset=['base_mean'])
    df = df[df['base_mean'] > 0]
    if len(df) == 0:
        ax.text(0.5, 0.5, 'no rows with positive base_mean',
                transform=ax.transAxes, ha='center', va='center', color='#888')
        return _render_figure(fig, dpi, output_format, caption, source_url)

    df['_logmean'] = np.log10(df['base_mean'])
    df['_sig'] = (df['padj'] <= padj_threshold) & (df['log2fc'].abs() >= log2fc_threshold)
    df['_direction'] = np.where(df['log2fc'] > 0, 'up', 'down')
    df['_category'] = np.where(df['_sig'], df['_direction'], 'ns')

    # Optional visual clamps — sig classification uses the raw log2fc, so
    # clipping only affects where the dot is drawn, not whether it counts
    # as significant.
    ma_clip_marks = []
    if log2fc_clip is not None and log2fc_clip > 0:
        df['log2fc'] = df['log2fc'].clip(-log2fc_clip, +log2fc_clip)
        ma_clip_marks.append(f'|log2fc|≤{log2fc_clip:g}')
    if logmean_clip is not None and logmean_clip > 0:
        df['_logmean'] = df['_logmean'].clip(upper=logmean_clip)
        ma_clip_marks.append(f'log₁₀mean≤{logmean_clip:g}')

    cmap_obj = matplotlib.cm.get_cmap(
        palette_cat if palette_cat in _QUALITATIVE_PALETTES else _DEFAULT_CATEGORICAL
    )
    colors = {'ns': '#bbbbbb', 'down': cmap_obj(0.0), 'up': cmap_obj(0.1)}

    for cat in ('ns', 'down', 'up'):
        sub = df[df['_category'] == cat]
        if len(sub) == 0:
            continue
        label = {'ns': 'n.s.', 'down': 'sig ↓', 'up': 'sig ↑'}[cat]
        ax.plot(
            sub['_logmean'], sub['log2fc'],
            marker='.', linestyle='none',
            markersize=2.5 if cat == 'ns' else 4.0,
            alpha=0.5 if cat == 'ns' else 0.9,
            color=colors[cat], label=label, rasterized=True,
        )

    ax.axhline(0, color='#444', linewidth=0.7)
    if log2fc_threshold and log2fc_threshold > 0:
        ax.axhline(+log2fc_threshold, linestyle='--', color='#888', linewidth=0.8)
        ax.axhline(-log2fc_threshold, linestyle='--', color='#888', linewidth=0.8)

    if n_label > 0:
        labelled = df[df['_sig']].nsmallest(n_label, 'padj')
        texts = [
            ax.text(row['_logmean'], row['log2fc'], str(row['gene_name']),
                    fontsize=7, alpha=0.9, ha='center', va='center')
            for _, row in labelled.iterrows()
        ]
        if len(texts) > 1:
            try:
                from adjustText import adjust_text  # type: ignore
                adjust_text(
                    texts, ax=ax,
                    arrowprops=dict(arrowstyle='-', color='#888', lw=0.4),
                    expand_points=(1.3, 1.6),
                    force_text=(0.4, 0.6),
                )
            except ImportError:
                pass

    ax.set_xlabel('log₁₀ mean expression')
    ax.set_ylabel('log₂ fold change')
    n_total = len(df)
    n_sig = int(df['_sig'].sum())
    title_bits = ['MA', f'{n_sig}/{n_total} sig (padj≤{padj_threshold:g}, |log2fc|≥{log2fc_threshold:g})']
    if ma_clip_marks:
        title_bits.append('clipped ' + ', '.join(ma_clip_marks))
    ax.set_title(_compose_title(title_bits, extra=title_extra))
    sns.despine(ax=ax)
    _legend_outside(ax, title='significance')

    return _render_figure(fig, dpi, output_format, caption, source_url)


def plot_forest(
    de_df: pd.DataFrame,
    *,
    gene_name: str,
    padj_threshold: float = 0.05,
    width: float = 8,
    height: float = 6,
    dpi: int = 150,
    output_format: str = 'png',
    palette_cat: str = _DEFAULT_CATEGORICAL,
    title_extra: Optional[str] = None,
    caption: Optional[str] = None,
    source_url: Optional[str] = None,
    font_family: Optional[str] = None,
    font_size: Optional[float] = None,
) -> bytes:
    """Forest plot — one gene's effect across many (study, term, contrast).

    Args:
        de_df:     DataFrame with `study_name`, `term`, `contrast`,
                   `log2fc` (or `estimate`), `ci_low`, `ci_high`, `padj`.
                   One row per (study, term, contrast) for the chosen gene.
        gene_name: Just used in the title; data must already be filtered
                   to a single gene.

    Each row gets a horizontal point + error bar.  Significance (padj
    below the threshold) is marked by colour.  Sorted by effect size
    ascending so the visual reads left-to-right as down-regulated →
    up-regulated.
    """
    _setup_style(font_family, font_size)

    df = de_df.copy()
    if len(df) == 0:
        fig, ax = plt.subplots(figsize=(width, height))
        ax.text(0.5, 0.5, f'no DE rows for {gene_name!r}',
                transform=ax.transAxes, ha='center', va='center', color='#888')
        return _render_figure(fig, dpi, output_format, caption, source_url)

    # Effect axis must match the CI axis.  For DESeq2/edgeR pipelines log2fc
    # and ci_low/ci_high are co-stored on the log2 scale, so they coincide.
    # For a GLMM with a natural-log link, log2fc is on log2 scale while
    # estimate / ci_low / ci_high live on the linear-predictor scale —
    # plotting log2fc as the dot with CIs from the linear-predictor scale
    # gives a visually broken plot where the bar doesn't cover the dot.
    # Pick: if CIs are present AND an estimate column is present, use the
    # estimate (matches the CI scale); otherwise fall back to log2fc with
    # no CI bars.  Determine the X-axis label from what was actually picked.
    has_ci = ('ci_low' in df.columns and 'ci_high' in df.columns
              and df['ci_low'].notna().any() and df['ci_high'].notna().any())
    has_estimate = 'estimate' in df.columns and df['estimate'].notna().any()

    if has_ci and has_estimate:
        df['_effect'] = df['estimate'].where(df['estimate'].notna(), df['log2fc'])
        # If estimate and log2fc are numerically identical (DESeq2 / edgeR
        # convention), the axis is still the log2-fc axis — just labelled
        # accordingly.  Otherwise we're on the model-native scale (typically
        # log-link / linear-predictor for GLMM, LMEM).
        diffs = (df['estimate'] - df['log2fc']).abs()
        same_scale = diffs.notna().any() and float(diffs.max()) < 1e-6
        x_label = 'log₂ fold change' if same_scale else 'effect (model-native scale)'
    else:
        df['_effect'] = df['log2fc'].where(df['log2fc'].notna(), df.get('estimate'))
        x_label = 'log₂ fold change'
        has_ci = False   # without a matching estimate, don't draw possibly-mismatched bars

    df = df.dropna(subset=['_effect'])
    if len(df) == 0:
        fig, ax = plt.subplots(figsize=(width, height))
        ax.text(0.5, 0.5, f'no rows with a numeric effect for {gene_name!r}',
                transform=ax.transAxes, ha='center', va='center', color='#888')
        return _render_figure(fig, dpi, output_format, caption, source_url)

    df = df.sort_values('_effect').reset_index(drop=True)
    # Y-axis labels: only show the components that VARY across rows.
    # Constant components (e.g. when /plot/forest is narrowed to one
    # study + one term) are hoisted into the title — keeps the label
    # column compact so CI bars have room.  Always keep contrast on
    # the label even if there's only one row, so a single-row forest
    # still labels itself sensibly.
    study_constant    = df['study_name'].nunique() == 1
    term_constant     = df['term'].nunique() == 1
    label_components = []
    title_constants  = []
    if study_constant:
        title_constants.append(str(df['study_name'].iloc[0]))
    else:
        label_components.append('study_name')
    if term_constant:
        title_constants.append(str(df['term'].iloc[0]))
    else:
        label_components.append('term')
    label_components.append('contrast')   # always shown
    df['_label'] = df[label_components].astype(str).agg(' · '.join, axis=1)
    df['_sig'] = df['padj'] <= padj_threshold

    # Height scales with the number of rows so labels stay readable.
    height_dyn = max(height, 0.25 * len(df) + 1.5)
    fig, ax = plt.subplots(figsize=(width, height_dyn))

    cmap_obj = matplotlib.cm.get_cmap(
        palette_cat if palette_cat in _QUALITATIVE_PALETTES else _DEFAULT_CATEGORICAL
    )
    sig_color = cmap_obj(0.1)
    ns_color = '#bbbbbb'

    ax.axvline(0, color='#444', linewidth=0.7)

    for i, row in df.iterrows():
        color = sig_color if row['_sig'] else ns_color
        if has_ci and pd.notna(row.get('ci_low')) and pd.notna(row.get('ci_high')):
            ax.plot([row['ci_low'], row['ci_high']], [i, i],
                    color=color, linewidth=1.2, solid_capstyle='round')
        ax.plot([row['_effect']], [i],
                marker='o', color=color, markersize=5,
                markeredgecolor='white', markeredgewidth=0.5)

    ax.set_yticks(range(len(df)))
    ax.set_yticklabels(df['_label'].tolist(), fontsize=7)
    ax.set_xlabel(x_label)
    ax.set_ylim(-0.5, len(df) - 0.5)
    n_sig = int(df['_sig'].sum())
    title_bits = ['Forest', gene_name]
    title_bits.extend(title_constants)   # hoisted study / term when constant
    title_bits.append(f'{n_sig}/{len(df)} sig (padj≤{padj_threshold:g})')
    ax.set_title(_compose_title(title_bits, extra=title_extra))
    sns.despine(ax=ax)
    fig.tight_layout()

    return _render_figure(fig, dpi, output_format, caption, source_url)


# ── 2-axis density geom: plot_kde2d ───────────────────────────────────────────


def _quantile_clip(values: 'np.ndarray',
                   lo: float = 0.10, hi: float = 0.90) -> 'Tuple[float, float]':
    """Compute (vmin, vmax) at the lo / hi quantiles of a 1-D array.

    Used by hexbin density colour-scale auto-clipping: without it, the
    densest few hex bins drag the entire gradient and the rest of the
    distribution looks flat.  10–90% is the default that works for
    every plot in the module-annotator iteration.
    """
    finite = values[np.isfinite(values)]
    if finite.size == 0:
        return (0.0, 1.0)
    q_lo = float(np.quantile(finite, lo))
    q_hi = float(np.quantile(finite, hi))
    if q_hi <= q_lo:
        q_hi = q_lo + 1.0
    return (q_lo, q_hi)


# ── shared overlays for XY-mode geoms: marginals + regression ────────────────


_REGLINE_MAX_GROUPS = 30


def attach_marginals(fig, ax_main, xs, ys,
                     *, color: str = '#666666') -> None:
    """Append univariate KDE strips on the top and right of ``ax_main``.

    Uses ``make_axes_locatable`` so the marginal axes share the main
    axes' data range automatically and the figure layout stays clean.
    Called by ``plot_scatter`` / ``plot_hexbin`` / ``plot_kde2d`` when
    their ``marginals=True`` flag is set.
    """
    from mpl_toolkits.axes_grid1 import make_axes_locatable
    xs = np.asarray(xs, dtype=float)
    ys = np.asarray(ys, dtype=float)
    xs = xs[np.isfinite(xs)]
    ys = ys[np.isfinite(ys)]
    if xs.size < 5 or ys.size < 5:
        return  # not enough data for a KDE
    divider = make_axes_locatable(ax_main)
    ax_top   = divider.append_axes('top',   size='12%', pad=0.05, sharex=ax_main)
    ax_right = divider.append_axes('right', size='12%', pad=0.05, sharey=ax_main)
    # KDE strips (seaborn handles the bw + fill).
    sns.kdeplot(x=xs, ax=ax_top,   color=color, fill=True, linewidth=0.6, bw_adjust=1.0)
    sns.kdeplot(y=ys, ax=ax_right, color=color, fill=True, linewidth=0.6, bw_adjust=1.0)
    for marginal in (ax_top, ax_right):
        marginal.tick_params(labelbottom=False, labelleft=False, labelright=False,
                             labeltop=False, length=0)
        marginal.set_xlabel(''); marginal.set_ylabel('')
        for side in ('top', 'right', 'bottom', 'left'):
            marginal.spines[side].set_visible(False)


def attach_regression(ax, xs, ys,
                      *, groups: 'Optional[np.ndarray]' = None,
                      kind: str = 'global',
                      palette_cat: str = _DEFAULT_CATEGORICAL) -> None:
    """Overlay a regression line + correlation stats on ``ax``.

    Args:
        ax:     the main axes (caller is responsible for axis scales).
        xs, ys: the transformed values that were plotted.  Must be 1-D
                arrays of the same length.
        groups: optional 1-D array of categorical labels aligned with
                xs/ys.  When given and ``kind='by_group'``, one line +
                stats box per group (cap ``_REGLINE_MAX_GROUPS``).
        kind:   ``'global'`` (one line across all points) or
                ``'by_group'`` (per-group regression).
    """
    from scipy.stats import linregress, spearmanr

    xs = np.asarray(xs, dtype=float)
    ys = np.asarray(ys, dtype=float)
    finite = np.isfinite(xs) & np.isfinite(ys)
    xs, ys = xs[finite], ys[finite]
    if xs.size < 3:
        return

    def _fit_and_draw(_xs, _ys, color, label_prefix=''):
        try:
            lr = linregress(_xs, _ys)
        except Exception:
            return None
        try:
            spear, _ = spearmanr(_xs, _ys, nan_policy='omit')
        except Exception:
            spear = float('nan')
        # Line spanning the data range
        xline = np.array([float(_xs.min()), float(_xs.max())])
        yline = lr.slope * xline + lr.intercept
        ax.plot(xline, yline, color=color, linewidth=1.2, alpha=0.9)
        return {
            'label':     label_prefix,
            'slope':     float(lr.slope),
            'intercept': float(lr.intercept),
            'pearson':   float(lr.rvalue),
            'r2':        float(lr.rvalue) ** 2,
            'pvalue':    float(lr.pvalue),
            'spearman':  float(spear),
            'n':         int(_xs.size),
        }

    cmap_name = palette_cat if palette_cat in _QUALITATIVE_PALETTES else _DEFAULT_CATEGORICAL
    cmap_obj = matplotlib.cm.get_cmap(cmap_name)

    if groups is None or kind == 'global':
        stats = _fit_and_draw(xs, ys, color='#1d4ed8')
        if stats:
            text = (
                f"n={stats['n']:,}\n"
                f"slope={stats['slope']:+.3g}\n"
                f"R²={stats['r2']:.3f}\n"
                f"r={stats['pearson']:+.3f}  ρ={stats['spearman']:+.3f}"
            )
            ax.text(0.02, 0.98, text, transform=ax.transAxes,
                    fontsize=7, va='top', ha='left',
                    bbox=dict(facecolor='white', alpha=0.75, edgecolor='#cccccc',
                              boxstyle='round,pad=0.3'))
        return

    # Per-group regression — group_labels aligned 1:1 with xs/ys.
    groups = np.asarray(groups)[finite]
    unique = sorted({str(g) for g in groups if g is not None})
    if len(unique) > _REGLINE_MAX_GROUPS:
        unique = unique[:_REGLINE_MAX_GROUPS]
    rows: 'List[str]' = []
    n_palette = getattr(cmap_obj, 'N', 256)
    for i, label in enumerate(unique):
        mask = groups.astype(str) == label
        if mask.sum() < 3:
            continue
        color = cmap_obj((i % n_palette) / max(n_palette - 1, 1))
        stats = _fit_and_draw(xs[mask], ys[mask], color=color, label_prefix=label)
        if stats:
            rows.append(
                f"{label}: n={stats['n']:,} slope={stats['slope']:+.2g} "
                f"R²={stats['r2']:.2f} ρ={stats['spearman']:+.2f}"
            )
    if rows:
        # Keep the box readable: max 8 lines.
        if len(rows) > 8:
            shown = rows[:8] + [f"… ({len(rows) - 8} more groups)"]
        else:
            shown = rows
        ax.text(0.02, 0.98, "\n".join(shown), transform=ax.transAxes,
                fontsize=6, va='top', ha='left', family='monospace',
                bbox=dict(facecolor='white', alpha=0.85, edgecolor='#cccccc',
                          boxstyle='round,pad=0.3'))


def plot_kde2d(
    data_df: pd.DataFrame,
    x_field: str,
    y_field: str,
    *,
    transform_x: str = 'linear',
    transform_y: str = 'linear',
    asinh_scale: float = 1.0,
    kde_n: int = 15_000,             # subsample size for the KDE fit
    kde_bw: Optional[float] = 0.22,  # bandwidth (None → scott's rule)
    n_levels: int = 10,
    grid_res: int = 200,             # grid resolution per axis
    # kde2d density is one-sided count-of-mass — sequential by nature.
    # palette_cont threads in the user's choice; color_scale='divergent'
    # is ignored here because diverging contours on a density don't
    # mean anything.
    palette_cont: str = _DEFAULT_SEQUENTIAL,
    color_scale: Optional[str] = None,     # accepted + ignored (density is always sequential)
    iso_overlay: bool = True,        # thin white iso-density contours
    point_overlay: bool = False,     # raster scatter behind the contours
    point_alpha: float = 0.05,
    marginals: bool = False,         # univariate KDE strips on top/right
    regline: Optional[str] = None,   # 'global' | None — regression line overlay
    seed: int = 0,
    width: float = 7,
    height: float = 6,
    dpi: int = 150,
    output_format: str = 'png',
    title_extra: Optional[str] = None,
    caption: Optional[str] = None,
    source_url: Optional[str] = None,
    font_family: Optional[str] = None,
    font_size: Optional[float] = None,
) -> bytes:
    """2-D Gaussian kernel-density estimate over two continuous fields.

    Args:
        data_df:       DataFrame with at least ``[x_field, y_field]``.
        transform_x / transform_y:
                       per-axis transform (see ``TRANSFORMS``). Applied
                       BEFORE the KDE fit so the contours sit in the
                       transformed space.
        kde_n:         subsample N cells for the KDE fit (default 15k —
                       full evaluation on 200k+ cells is slow and adds
                       no visible detail).
        kde_bw:        bandwidth.  None → ``scipy.stats.gaussian_kde``'s
                       Scott's-rule default.  Numeric → that float.
        n_levels:      number of filled contour levels (default 10).
        grid_res:      evaluation-grid resolution per axis (default 200,
                       so 40,000 grid points; usually <0.5s).
        iso_overlay:   draw thin white iso-density contour lines on top
                       of the filled contours (highlights modes).
        point_overlay: raster scatter behind the contours (default off;
                       fine on small N).
    """
    from scipy.stats import gaussian_kde
    _setup_style(font_family, font_size)
    fig, ax = plt.subplots(figsize=(width, height))

    df = data_df[[x_field, y_field]].dropna()
    if len(df) < 5:
        ax.text(0.5, 0.5, 'too few cells for a KDE',
                transform=ax.transAxes, ha='center', va='center', color='#888')
        return _render_figure(fig, dpi, output_format, caption, source_url)

    xs = apply_axis_transform(df[x_field].to_numpy(), transform_x, asinh_scale)
    ys = apply_axis_transform(df[y_field].to_numpy(), transform_y, asinh_scale)
    finite = np.isfinite(xs) & np.isfinite(ys)
    xs, ys = xs[finite], ys[finite]
    n_total = int(finite.sum())
    if n_total < 5:
        ax.text(0.5, 0.5, 'too few finite values after transform',
                transform=ax.transAxes, ha='center', va='center', color='#888')
        return _render_figure(fig, dpi, output_format, caption, source_url)

    # Subsample for the KDE fit (default 15k cells).  The grid evaluation
    # uses the full bandwidth from the subsampled fit, so the density
    # estimate is unbiased — the subsample only reduces fit cost.
    rng = np.random.default_rng(seed)
    if n_total > kde_n:
        idx = rng.choice(n_total, size=kde_n, replace=False)
        fit_xs, fit_ys = xs[idx], ys[idx]
    else:
        fit_xs, fit_ys = xs, ys

    bw_method = kde_bw if kde_bw is not None else 'scott'
    kde = gaussian_kde(np.vstack([fit_xs, fit_ys]), bw_method=bw_method)

    # Evaluation grid spans the FULL data range (not the subsample's),
    # padded by 3% on each side so contours don't clip at the edges.
    pad = 0.03
    xmin, xmax = xs.min(), xs.max()
    ymin, ymax = ys.min(), ys.max()
    xrange, yrange = (xmax - xmin) or 1.0, (ymax - ymin) or 1.0
    xmin -= pad * xrange; xmax += pad * xrange
    ymin -= pad * yrange; ymax += pad * yrange
    grid_x = np.linspace(xmin, xmax, grid_res)
    grid_y = np.linspace(ymin, ymax, grid_res)
    grid_xx, grid_yy = np.meshgrid(grid_x, grid_y)
    z = kde(np.vstack([grid_xx.ravel(), grid_yy.ravel()])).reshape(grid_res, grid_res)

    if point_overlay:
        ax.plot(xs, ys, marker='.', linestyle='none', markersize=1.5,
                color='#444444', alpha=point_alpha, rasterized=True)

    # Honour palette_cont for the density colormap. pick_cmap handles
    # the qualitative-palette / divergent-palette fallbacks defensively
    # — kde2d density is one-sided so we always want sequential.
    cmap_name, _ = pick_cmap(
        'continuous', palette_cont=palette_cont,
        color_scale='sequential',
    )
    cs = ax.contourf(grid_xx, grid_yy, z, levels=n_levels, cmap=cmap_name)
    if iso_overlay:
        ax.contour(grid_xx, grid_yy, z, levels=n_levels,
                   colors='white', linewidths=0.4, alpha=0.6)

    cbar = plt.colorbar(cs, ax=ax, shrink=0.7)
    cbar.set_label('density', fontsize=9)

    ax.set_xlabel(_axis_label_with_transform(x_field, transform_x))
    ax.set_ylabel(_axis_label_with_transform(y_field, transform_y))
    title_bits = [
        '2-D KDE',
        f'{y_field} vs {x_field}',
        f'n={_fmt_n(n_total)}{" (fit "+_fmt_n(len(fit_xs))+")" if n_total > kde_n else ""}',
    ]
    ax.set_title(_compose_title(title_bits, extra=title_extra))
    sns.despine(ax=ax)

    # Overlays — drawn AFTER the title so they read on top of the contours.
    if regline:
        attach_regression(ax, xs, ys, kind='global')
    if marginals:
        attach_marginals(fig, ax, xs, ys, color='#666666')

    fig.tight_layout()

    return _render_figure(fig, dpi, output_format, caption, source_url)
