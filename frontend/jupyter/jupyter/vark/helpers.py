"""Notebook-friendly helpers around :class:`vark.AnthiveClient`.

Port of the legacy ``anthelper.py`` from the antclient era. These
functions exist purely for human ergonomics in Jupyter — they print to
stdout rather than returning structured data. Use the client methods
directly when you need values.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Optional


@lru_cache(32)
def find_database(client,
                  search_string: Optional[str] = None,
                  n: int = 20,
                  verbose: bool = True) -> None:
    """Search databases by name / title / group and print a summary.

    Args:
        client: :class:`vark.AnthiveClient`.
        search_string: Substring matched against ``id``, ``title``,
            ``group`` (case-insensitive). ``None`` shows the first ``n``.
        n: Maximum results to show.
        verbose: Show full per-dataset block.
    """
    databases = client.get_databases()

    if search_string is not None:
        query = search_string.lower()
        databases = [
            db for db in databases
            if query in db['id'].lower()
            or query in db.get('title', '').lower()
            or query in db.get('group', '').lower()
        ]

    if not databases:
        print("No databases found")
        return

    for record in sorted(databases, key=lambda x: -x.get('year', 0))[:n]:
        if verbose:
            print(f"{record['id']}")
            print(f"  - group       : {record.get('group', '')}")
            print(f"  - title       : {record.get('title', '')} "
                  f"({record.get('year', '')})")
            print(f"  - size        : {record.get('n_cells', '?')} cells "
                  f"/ {record.get('n_genes', '?')} genes")
            layers = record.get('layers', {})
            layer_names = (
                ', '.join(layers.keys()) if isinstance(layers, dict)
                else ', '.join(layers)
            )
            print(f"  - layers      : {layer_names}")
            obs = record.get('obs', {})
            num_cols = obs.get('numerical', [])
            cat_cols = obs.get('categorical', [])
            print(
                f"  - categorical : {len(cat_cols)} — "
                f"{', '.join(cat_cols[:3])}"
                f"{', ...' if len(cat_cols) > 3 else ''}"
            )
            print(
                f"  - numerical   : {len(num_cols)} — "
                f"{', '.join(num_cols[:3])}"
                f"{', ...' if len(num_cols) > 3 else ''}"
            )
        else:
            print(record['id'])


def find_metadata(client,
                  db_id: str,
                  search_string: Optional[str] = None) -> None:
    """Print numerical + categorical obs columns for a dataset.

    Args:
        client: :class:`vark.AnthiveClient`.
        db_id: ``group/file`` id.
        search_string: Substring filter (case-insensitive). ``None``
            shows everything.
    """
    import textwrap

    fields = client.get_metadata_fields(db_id)

    if search_string is not None:
        query = search_string.lower()
        fields = {
            'numerical':   [f for f in fields['numerical']
                            if query in f.lower()],
            'categorical': [f for f in fields['categorical']
                            if query in f.lower()],
        }

    numerical = fields['numerical']
    categorical = fields['categorical']

    if not numerical and not categorical:
        print("No results found")
        return

    def _wrap(items):
        return "    " + "\n    ".join(
            textwrap.wrap(", ".join(items), width=70)
        )

    if numerical:
        print("# Numerical")
        print(_wrap(numerical))
    if categorical:
        print("# Categorical")
        print(_wrap(categorical))


__all__ = ["find_database", "find_metadata"]
