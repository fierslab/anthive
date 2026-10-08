"""Modality of a dataset: nuclei or cells.

Single-nucleus and single-cell data detect genes differently (nuclei carry more intronic and nuclear
transcripts, fewer mitochondrial and cytoplasmic ones), so a reader must not take a detection difference between
a nuclei and a cell dataset for biology. The modality comes from the ``suspension_type`` of the dataset's
``.h5ad.mus`` annotation (``nucleus`` / ``cell`` / ``na``) and, when that is missing, from the technology name
(``snRNA-seq`` and friends). Human cells xenografted into mouse brain are sorted live cells.
"""
from __future__ import annotations

from typing import Any, Dict, Mapping, Optional

NUCLEI = 'nuclei'
CELLS = 'cells'
UNKNOWN = 'unknown'

#: Facet scope whose cells are sorted live cells (graft cells isolated from the host brain).
XENOGRAFT_SCOPE = 'xenograft_human_in_mouse'
XENOGRAFT_NOTE = 'sorted live cells'

_SUSPENSION = {'nucleus': NUCLEI, 'nuclei': NUCLEI, 'cell': CELLS, 'cells': CELLS}
_LABELS = {NUCLEI: 'nuclei', CELLS: 'cells', UNKNOWN: 'not recorded'}


def modality_of(suspension_type: Any = None, technology: Any = None, facet_scope: Optional[str] = None
                ) -> Dict[str, Optional[str]]:
    """Modality of one dataset.

    Args:
        suspension_type: ``.h5ad.mus`` ``suspension_type`` (``nucleus``, ``cell``, ``na``).
        technology: ``.h5ad.mus`` ``technology``; a name starting with ``sn`` (snRNA-seq) means nuclei.
        facet_scope: The dataset's facet scope; a xenograft carries the note ``sorted live cells``.

    Returns:
        ``{modality, label, note}``: ``modality`` is ``nuclei``, ``cells`` or ``unknown``; ``label`` the plain
        word for a tag; ``note`` is ``sorted live cells`` for a xenograft of cells, else None.
    """
    modality = _SUSPENSION.get(str(suspension_type or '').strip().lower())
    if modality is None:
        modality = NUCLEI if str(technology or '').strip().lower().startswith('sn') else UNKNOWN
    note = XENOGRAFT_NOTE if facet_scope == XENOGRAFT_SCOPE and modality == CELLS else None
    return {'modality': modality, 'label': _LABELS[modality], 'note': note}


def modality_of_metadata(meta: Optional[Mapping[str, Any]]) -> Dict[str, Optional[str]]:
    """``modality_of`` for a dataset metadata dict (``suspension_type``, ``technology``, ``facet_scope``)."""
    meta = meta or {}
    return modality_of(meta.get('suspension_type'), meta.get('technology'), meta.get('facet_scope'))
