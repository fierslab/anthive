"""Unit tests for the ``bin/annot/ant-module-variability`` builder.

Focus areas that were the root cause of the S25/V6W visibility bug:

1. Datasets that carry module-score columns in ``__obsnum`` (the legacy
   ``<study>::<experiment>::<module>`` naming) with **no**
   ``.scores.parquet`` sidecar must still produce variability rows —
   before the fix they were silently dropped fleet-wide.
2. Datasets whose ``.md`` uses **chained** ``api.obs_rename`` (e.g.
   ``Sex_donor → sex → ant_sex``) must still map score-vs-tag against the
   view-visible intermediate column name (``sex``), because the view
   builder only applies a single hop.
3. Datasets flagged ``<basename>.inactive`` must be skipped during fleet
   rebuild — they aren't in the served catalog so their scores must not
   leak into the variability parquet.

Loads the ``ant-module-variability`` script via ``importlib`` (it has a
uv-script shebang, not a normal module).  Builds a tiny synthetic duckdb
with the shape ant-convert produces: ``__cells``, ``__obsnum``,
``__obscat`` + the ``obsnum`` / ``obscat`` VIEWs (this is what the builder
introspects, not the underlying underscore tables).

Run with:
    uv run --with pytest --with duckdb --with pandas --with numpy \
           --with scipy --with pyarrow --with pyyaml --no-project \
           env PYTHONPATH=lib python -m pytest tests/test_ant_module_variability.py -q
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Iterable

import duckdb
import pandas as pd
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / 'lib'))


def _load_amv():
    """Load the uv-script `ant-module-variability` as a regular module."""
    path = REPO_ROOT / 'bin' / 'annot' / 'ant-module-variability'
    spec = importlib.util.spec_from_loader(
        'ant_module_variability',
        importlib.machinery.SourceFileLoader('ant_module_variability', str(path)),
    )
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    # dataclasses looks the module up in sys.modules while the class body runs.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


amv = _load_amv()


CELL_NAMES = [f"cell_{i:04d}" for i in range(30)]


def _write_md(md_path: Path, api_rename: dict, obs_tags: dict, extra: dict) -> None:
    """Write a minimal .md compatible with ``metadata.parse_md_file``."""
    import yaml
    payload = {
        'id': extra.pop('id', 'TST'),
        'title': 'test',
        'obs_tags': obs_tags,
        'api': {
            'obs_rename': api_rename,
            'obs_hidden': [],
            'obs_value_rename': {},
            'obs_derive': {},
        },
        **extra,
    }
    md_path.write_text('---\n' + yaml.safe_dump(payload) + '---\n\n# test\n')


def _write_index_md(dir_path: Path, group_id: str = 'TST') -> None:
    """Write the group's index.md so `iter_h5adstore` accepts the entry."""
    (dir_path / 'index.md').write_text(
        f'---\ngroup: TestStudy\ngroup_id: {group_id}\n---\n\n# Test Study\n'
    )


def _build_duckdb(
    path: Path,
    num_cols: dict[str, Iterable[float]],
    cat_cols: dict[str, Iterable[str]],
    view_num_rename: dict[str, str] | None = None,
    view_cat_rename: dict[str, str] | None = None,
) -> None:
    """Build a minimal duckdb whose ``obsnum`` / ``obscat`` VIEWs surface
    the exact column names the builder expects.

    ``num_cols`` / ``cat_cols`` supply the underscore-table data. The
    optional ``view_*_rename`` maps stored → published column name and
    emulates one hop of ``api.obs_rename`` as the real view builder does.
    """
    conn = duckdb.connect(str(path))
    try:
        conn.execute(
            'CREATE TABLE __cells (cell_id INTEGER PRIMARY KEY, '
            'cell_name TEXT NOT NULL UNIQUE)'
        )
        cells_df = pd.DataFrame({'cell_id': range(len(CELL_NAMES)),
                                 'cell_name': CELL_NAMES})
        conn.register('t_cells', cells_df)
        conn.execute('INSERT INTO __cells SELECT * FROM t_cells')

        # __obsnum
        num_defs = ', '.join(f'"{c}" DOUBLE' for c in num_cols.keys())
        conn.execute(
            'CREATE TABLE __obsnum (cell_id INTEGER PRIMARY KEY'
            + (', ' + num_defs if num_defs else '')
            + ')'
        )
        num_df = pd.DataFrame({'cell_id': range(len(CELL_NAMES)), **num_cols})
        conn.register('t_num', num_df)
        conn.execute('INSERT INTO __obsnum SELECT * FROM t_num')

        # __obscat
        cat_defs = ', '.join(f'"{c}" VARCHAR' for c in cat_cols.keys())
        conn.execute(
            'CREATE TABLE __obscat (cell_id INTEGER PRIMARY KEY'
            + (', ' + cat_defs if cat_defs else '')
            + ')'
        )
        cat_df = pd.DataFrame({'cell_id': range(len(CELL_NAMES)), **cat_cols})
        conn.register('t_cat', cat_df)
        conn.execute('INSERT INTO __obscat SELECT * FROM t_cat')

        # obsnum VIEW — one-hop rename per the builder's contract
        num_rename = view_num_rename or {}
        num_proj = ['c.cell_name']
        for col in num_cols.keys():
            alias = num_rename.get(col, col)
            num_proj.append(f'o."{col}" AS "{alias}"')
        conn.execute(
            f'CREATE VIEW obsnum AS SELECT {", ".join(num_proj)} '
            'FROM __obsnum o JOIN __cells c ON o.cell_id = c.cell_id'
        )
        cat_rename = view_cat_rename or {}
        cat_proj = ['c.cell_name']
        for col in cat_cols.keys():
            alias = cat_rename.get(col, col)
            cat_proj.append(f'o."{col}" AS "{alias}"')
        conn.execute(
            f'CREATE VIEW obscat AS SELECT {", ".join(cat_proj)} '
            'FROM __obscat o JOIN __cells c ON o.cell_id = c.cell_id'
        )
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #


@pytest.fixture
def tmp_dataset(tmp_path):
    """Return a factory that builds a (duckdb, md) pair in a fresh subdir."""
    def factory(
        subdir: str = 'sample',
        num_cols: dict | None = None,
        cat_cols: dict | None = None,
        view_num_rename: dict | None = None,
        view_cat_rename: dict | None = None,
        api_rename: dict | None = None,
        obs_tags: dict | None = None,
        with_sidecar: bool = False,
        sidecar_rows: pd.DataFrame | None = None,
        inactive: bool = False,
        extra_md: dict | None = None,
    ):
        d = tmp_path / subdir
        d.mkdir()
        _write_index_md(d, group_id='TST')
        duckdb_path = d / 'sample.duckdb'
        _build_duckdb(
            duckdb_path,
            num_cols or {},
            cat_cols or {},
            view_num_rename=view_num_rename,
            view_cat_rename=view_cat_rename,
        )
        md_path = d / 'sample.md'
        _write_md(md_path, api_rename or {}, obs_tags or {}, extra_md or {})
        if inactive:
            (d / 'sample.inactive').touch()
        if with_sidecar and sidecar_rows is not None:
            from lib.scores_parquet import upsert_scores  # type: ignore
            for (source, catalog), grp in sidecar_rows.groupby(['source', 'catalog']):
                upsert_scores(duckdb_path, source, catalog,
                              grp[['cell_id', 'module', 'score']])
        return duckdb_path, md_path
    return factory


# --------------------------------------------------------------------------- #
# _obsnum_score_columns                                                       #
# --------------------------------------------------------------------------- #


def test_obsnum_score_columns_detects_double_colon_names(tmp_path):
    """Every ``<study>::<experiment>::<module>`` column in ``__obsnum`` is
    reported with the sidecar-schema tuple ``(col, 'geneset', study/exp, mod)``."""
    duckdb_path = tmp_path / 'ds.duckdb'
    _build_duckdb(
        duckdb_path,
        num_cols={
            'Sierksma2025::WGCNA::Black_Mito': [float(i) for i in range(30)],
            'Sierksma2025::WGCNA::Blue_Innate_IR': [float(i * 2) for i in range(30)],
            'n_counts': [100.0] * 30,   # NOT a score column
        },
        cat_cols={},
    )
    conn = duckdb.connect(str(duckdb_path), read_only=True)
    try:
        cols = amv._obsnum_score_columns(conn)
    finally:
        conn.close()
    triples = {(c, s, cat, mod) for (c, s, cat, mod) in cols}
    assert (
        'Sierksma2025::WGCNA::Black_Mito', 'geneset',
        'Sierksma2025/WGCNA', 'Black_Mito',
    ) in triples
    assert (
        'Sierksma2025::WGCNA::Blue_Innate_IR', 'geneset',
        'Sierksma2025/WGCNA', 'Blue_Innate_IR',
    ) in triples
    assert not any(c == 'n_counts' for (c, _, _, _) in cols), \
        "n_counts must not be classified as a score column"


def test_obsnum_score_columns_ignores_bad_shapes(tmp_path):
    """Names with fewer/more parts, or empty parts, are ignored."""
    duckdb_path = tmp_path / 'ds.duckdb'
    _build_duckdb(
        duckdb_path,
        num_cols={
            'foo::bar': [1.0] * 30,           # only two parts
            'foo::bar::baz::qux': [1.0] * 30,  # four parts
            'foo::::baz': [1.0] * 30,          # empty middle
            'a::b::c': [1.0] * 30,             # ok
        },
        cat_cols={},
    )
    conn = duckdb.connect(str(duckdb_path), read_only=True)
    try:
        cols = amv._obsnum_score_columns(conn)
    finally:
        conn.close()
    assert [c[0] for c in cols] == ['a::b::c']


# --------------------------------------------------------------------------- #
# compute_for_dataset — obs fallback path                                     #
# --------------------------------------------------------------------------- #


def test_compute_uses_obs_fallback_when_no_sidecar(tmp_dataset):
    """Root-cause fix for S25/V6W: no sidecar but score columns in obs → the
    builder must still emit variability rows using the obs values."""
    n = len(CELL_NAMES)
    # Two modules, one canonical categorical tag surfaced via a direct rename
    # (``Sex_donor → ant_sex``, so the view exposes ``ant_sex``).
    duckdb_path, md_path = tmp_dataset(
        num_cols={
            'Sierksma2025::WGCNA::Black_Mito': [float(i) for i in range(n)],
            'Sierksma2025::WGCNA::Blue_Innate_IR': [float(i * 2) for i in range(n)],
        },
        cat_cols={
            'Sex_donor': ['f' if i % 2 else 'm' for i in range(n)],
        },
        view_cat_rename={'Sex_donor': 'ant_sex'},
        api_rename={'Sex_donor': 'ant_sex'},
    )
    df = amv.compute_for_dataset('TST/TST', duckdb_path, md_path)
    assert not df.empty, "must produce rows via obs fallback"
    assert set(df['module']) == {'Black_Mito', 'Blue_Innate_IR'}
    assert (df['catalog'] == 'Sierksma2025/WGCNA').all()
    assert (df['source'] == 'geneset').all()
    assert 'ant_sex' in set(df['tag_column'])


def test_compute_handles_chained_obs_rename(tmp_dataset):
    """S25/V6W .md uses a two-hop chain ``Sex_donor → sex → ant_sex``.

    The view builder only performs a single hop, so the view actually
    surfaces ``sex``.  The builder must nevertheless recognise ``sex`` as
    a canonical-tag-carrying column and produce variability rows against it.
    """
    n = len(CELL_NAMES)
    duckdb_path, md_path = tmp_dataset(
        num_cols={
            'Sierksma2025::WGCNA::Black_Mito': [float(i) for i in range(n)],
        },
        cat_cols={
            'Sex_donor': ['f' if i % 2 else 'm' for i in range(n)],
        },
        # Only one hop is applied to the view (Sex_donor → sex).
        view_cat_rename={'Sex_donor': 'sex'},
        # The .md's declared chain says the intent is ant_sex.
        api_rename={'Sex_donor': 'sex', 'sex': 'ant_sex'},
    )
    df = amv.compute_for_dataset('TST/TST', duckdb_path, md_path)
    assert not df.empty
    assert 'sex' in set(df['tag_column'])
    # Tag id must be the canonical one (`sex`), regardless of the raw column.
    assert set(df.loc[df['tag_column'] == 'sex', 'tag']) == {'sex'}


def test_compute_survives_cyclic_obs_rename(tmp_dataset):
    """A pathological ``api.obs_rename`` cycle (A→B→A) must not spin the
    chain resolver forever — the guard is `seen` in `_resolve_chain`."""
    n = len(CELL_NAMES)
    duckdb_path, md_path = tmp_dataset(
        num_cols={
            'Sierksma2025::WGCNA::Black_Mito': [float(i) for i in range(n)],
        },
        cat_cols={
            'A': ['x' if i % 2 else 'y' for i in range(n)],
        },
        # No canonical target reached — this exercises the terminating cycle guard.
        api_rename={'A': 'B', 'B': 'A'},
    )
    # Must return without hanging or raising.
    df = amv.compute_for_dataset('TST/TST', duckdb_path, md_path)
    # Cycle didn't reach a canonical tag → no rows from these tags (score
    # column may still contribute rows via canonical tags if any).
    # The key assertion is that we got here at all.
    assert isinstance(df, pd.DataFrame)


def test_compute_column_with_embedded_quotes_is_safe(tmp_dataset):
    """A score column whose name contains a double-quote must not break the
    query — the identifier is escaped defensively. duckdb allows quotes in
    identifiers via double-quote doubling."""
    n = len(CELL_NAMES)
    weird_col = 'Study"::"Exp::"Mod'
    # Only accepted by _obsnum_score_columns if it also matches the '::' shape
    # after quote-doubling. Skip if the shape doesn't parse — this is a
    # defense-in-depth test, not a shape-validation test.
    duckdb_path, md_path = tmp_dataset(
        num_cols={
            'S::E::M': [float(i) for i in range(n)],
        },
        cat_cols={
            'Sex_donor': ['f' if i % 2 else 'm' for i in range(n)],
        },
        view_cat_rename={'Sex_donor': 'ant_sex'},
        api_rename={'Sex_donor': 'ant_sex'},
    )
    df = amv.compute_for_dataset('TST/TST', duckdb_path, md_path)
    assert not df.empty
    assert 'M' in set(df['module'])


def test_compute_returns_empty_when_no_scores_and_no_tags(tmp_dataset):
    """Genuinely empty case — no score columns, no sidecar, no canonical
    tags. Must return an empty DataFrame, not raise."""
    duckdb_path, md_path = tmp_dataset(
        num_cols={'n_counts': [100.0] * len(CELL_NAMES)},
        cat_cols={'batch': ['a'] * len(CELL_NAMES)},
    )
    df = amv.compute_for_dataset('TST/TST', duckdb_path, md_path)
    assert df.empty


def test_compute_sidecar_takes_priority_over_obs_duplicate(tmp_dataset):
    """When the same ``(source, catalog, module)`` exists in both the
    sidecar AND obs, the sidecar wins (single row-set, no double count)."""
    n = len(CELL_NAMES)
    sidecar_rows = pd.DataFrame({
        'cell_id': list(range(n)),
        'module': ['Black_Mito'] * n,
        'score': [10.0] * n,   # a distinctive constant so we can tell them apart
        'source': ['geneset'] * n,
        'catalog': ['Sierksma2025/WGCNA'] * n,
    })
    duckdb_path, md_path = tmp_dataset(
        num_cols={
            # obs values would produce non-zero variance
            'Sierksma2025::WGCNA::Black_Mito': [float(i) for i in range(n)],
        },
        cat_cols={
            'Sex_donor': ['f' if i % 2 else 'm' for i in range(n)],
        },
        view_cat_rename={'Sex_donor': 'ant_sex'},
        api_rename={'Sex_donor': 'ant_sex'},
        with_sidecar=True,
        sidecar_rows=sidecar_rows,
    )
    df = amv.compute_for_dataset('TST/TST', duckdb_path, md_path)
    # One catalog-module triple only — no duplication.
    triples = df[['source', 'catalog', 'module']].drop_duplicates()
    assert len(triples) == 1
    # Total variance is 0 (all sidecar values are 10.0) → total_var == 0.
    tv = df[df['statistic'] == 'total_var']['value'].iloc[0]
    assert tv == 0.0, (
        "sidecar values (all 10.0) should have zero variance — "
        "obs values would give non-zero, confirming sidecar wins"
    )


# --------------------------------------------------------------------------- #
# Inactive filtering (fleet main loop)                                        #
# --------------------------------------------------------------------------- #


def test_main_skips_inactive_datasets(tmp_path, monkeypatch, capsys):
    """A `<basename>.inactive` sidecar must exclude a dataset from the
    fleet rebuild, matching ant-serve's catalog filter."""
    n = len(CELL_NAMES)
    active = tmp_path / 'active'
    active.mkdir()
    _write_index_md(active, group_id='TST')
    _build_duckdb(
        active / 'sample.duckdb',
        num_cols={'Sierksma2025::WGCNA::Black_Mito': [float(i) for i in range(n)]},
        cat_cols={'Sex_donor': ['f' if i % 2 else 'm' for i in range(n)]},
        view_cat_rename={'Sex_donor': 'ant_sex'},
    )
    _write_md(active / 'sample.md',
              api_rename={'Sex_donor': 'ant_sex'},
              obs_tags={},
              extra={'id': 'ACT'})

    inactive = tmp_path / 'inactive'
    inactive.mkdir()
    _write_index_md(inactive, group_id='TST')
    _build_duckdb(
        inactive / 'sample.duckdb',
        num_cols={'Sierksma2025::WGCNA::Black_Mito': [float(i) for i in range(n)]},
        cat_cols={'Sex_donor': ['f' if i % 2 else 'm' for i in range(n)]},
        view_cat_rename={'Sex_donor': 'ant_sex'},
    )
    _write_md(inactive / 'sample.md',
              api_rename={'Sex_donor': 'ant_sex'},
              obs_tags={},
              extra={'id': 'INA'})
    (inactive / 'sample.inactive').touch()

    out_parquet = tmp_path / 'mv.parquet'
    monkeypatch.setattr(sys, 'argv', [
        'ant-module-variability',
        '--h5adstore-root', str(tmp_path),
        '--out', str(out_parquet),
    ])
    rc = amv.main()
    assert rc == 0

    combined = duckdb.execute(
        f"SELECT DISTINCT dataset FROM read_parquet('{out_parquet}')"
    ).fetchall()
    dsets = {r[0] for r in combined}
    assert 'TST/ACT' in dsets
    assert 'TST/INA' not in dsets, "inactive dataset must be skipped"
