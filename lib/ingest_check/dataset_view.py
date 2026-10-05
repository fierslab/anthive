"""Read-only access to a dataset's ``.duckdb`` as the served ``ant_*`` views.

Attaches the file read-only and rebuilds TEMP ``obscat`` / ``obsnum`` views
from the *current* ``.md`` ``api:`` section (the same SQL the server uses,
``anthive_db.build_obs_view_sql``). The views therefore show what the
dataset will publish with the submitted metadata, even when the ``.duckdb``
was converted from an older ``.md``. Nothing is written to the file.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

import pandas as pd


class DatasetView:
    """A read-only DuckDB connection with the published obs views.

    Args:
        duckdb_path: The dataset ``.duckdb``.
        api_config: The ``api:`` section of the ``.md`` (``metadata.get_api_config``).
        group_id: Group id for ``{group_id}`` templates.
        db_label: Label used in view warnings.
        store_root: Where join tables live.
        threads: DuckDB threads.
        memory_limit_gb: DuckDB memory limit.
        temp_dir: DuckDB spill directory.
    """

    def __init__(self, duckdb_path: Path, api_config: Mapping[str, Any], group_id: Optional[str],
                 db_label: str = '', store_root: Optional[Path] = None, threads: int = 4,
                 memory_limit_gb: int = 16, temp_dir: Optional[Path] = None) -> None:
        import duckdb  # noqa: PLC0415
        from anthive_db import build_obs_view_sql  # noqa: PLC0415

        self.path = Path(duckdb_path)
        self.conn = duckdb.connect(':memory:', config={'threads': threads, 'memory_limit': f'{memory_limit_gb}GB'})
        if temp_dir is not None:
            Path(temp_dir).mkdir(parents=True, exist_ok=True)
            self.conn.execute(f"SET temp_directory='{temp_dir}'")
        self.conn.execute(f"ATTACH '{self.path}' AS ds (READ_ONLY)")
        self.conn.execute('USE ds')
        self.stored_columns: Dict[str, str] = {}
        underscore_columns: Dict[str, List[str]] = {}
        for table, kind in (('__obscat', 'categorical'), ('__obsnum', 'numerical')):
            names = [row[0] for row in self.conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_catalog='ds' AND table_name = ? ORDER BY ordinal_position", [table]).fetchall()]
            underscore_columns[table] = names
            for name in names:
                if name != 'cell_id':
                    self.stored_columns.setdefault(name, kind)
        for view_name, table, sibling in (('obscat', '__obscat', '__obsnum'), ('obsnum', '__obsnum', '__obscat')):
            if not underscore_columns[table]:
                continue
            sql = build_obs_view_sql(view_name, table, underscore_columns[table], dict(api_config or {}),
                                     sibling_underscore_table=sibling, sibling_columns=underscore_columns[sibling],
                                     db_label=db_label, group_id=group_id, store_root=store_root)
            self.conn.execute('CREATE OR REPLACE TEMP VIEW' + sql[len('CREATE VIEW'):])

    def close(self) -> None:
        """Close the connection."""
        self.conn.close()

    def view_columns(self, view_name: str) -> Dict[str, str]:
        """Column name -> DuckDB type of ``obscat`` or ``obsnum`` (empty when absent)."""
        try:
            return {row[0]: row[1] for row in self.conn.execute(f'DESCRIBE {view_name}').fetchall()}
        except Exception:  # noqa: BLE001 - the view does not exist
            return {}

    def published_columns(self) -> Dict[str, str]:
        """Public name -> ``categorical`` / ``numerical`` over both views."""
        columns = {name: 'categorical' for name in self.view_columns('obscat') if name != 'cell_name'}
        for name in self.view_columns('obsnum'):
            if name != 'cell_name':
                columns.setdefault(name, 'numerical')
        return columns

    def n_cells(self) -> int:
        """Number of rows in ``__cells``."""
        return int(self.conn.execute('SELECT count(*) FROM __cells').fetchone()[0])

    def cell_names(self) -> List[str]:
        """Cell names (barcodes) in ``cell_id`` order."""
        return [row[0] for row in self.conn.execute('SELECT cell_name FROM __cells ORDER BY cell_id').fetchall()]

    def cell_frame(self, wanted: Iterable[str]) -> pd.DataFrame:
        """``cell_id`` plus the wanted published columns, in ``cell_id`` order.

        Columns absent from both views are skipped. A name in both views
        comes from ``obscat``.
        """
        categorical = self.view_columns('obscat')
        numerical = self.view_columns('obsnum')
        wanted = list(dict.fromkeys(wanted))
        from_cat = [name for name in wanted if name in categorical and name != 'cell_name']
        from_num = [name for name in wanted if name in numerical and name not in categorical and name != 'cell_name']
        if from_cat:
            select = ', '.join('o."' + name.replace('"', '""') + '"' for name in from_cat)
            frame = self.conn.execute(
                f'SELECT c.cell_id, {select} FROM __cells c JOIN obscat o ON o.cell_name = c.cell_name '
                f'ORDER BY c.cell_id').df()
        else:
            frame = self.conn.execute('SELECT cell_id FROM __cells ORDER BY cell_id').df()
        if from_num:
            select = ', '.join('o."' + name.replace('"', '""') + '"' for name in from_num)
            numeric = self.conn.execute(
                f'SELECT c.cell_id, {select} FROM __cells c JOIN obsnum o ON o.cell_name = c.cell_name '
                f'ORDER BY c.cell_id').df()
            frame = frame.merge(numeric, on='cell_id', how='left')
        if len(frame) != self.n_cells():
            raise RuntimeError('cell_name join changed the row count: duplicate cell names in __cells')
        return frame

    def distinct_values(self, column: str, limit: int = 5000) -> Optional[List[Tuple[Any, int]]]:
        """``(value, n_cells)`` of the published column, most frequent first; None when unreadable."""
        view = 'obscat' if column in self.view_columns('obscat') else 'obsnum'
        quoted = '"' + column.replace('"', '""') + '"'
        try:
            return self.conn.execute(
                f'SELECT {quoted}, COUNT(*) FROM {view} WHERE {quoted} IS NOT NULL '
                f'GROUP BY 1 ORDER BY 2 DESC LIMIT {int(limit)}').fetchall()
        except Exception:  # noqa: BLE001
            return None
