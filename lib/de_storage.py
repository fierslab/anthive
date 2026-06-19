"""DE storage layer — schema, ingestion, sidecar parquet, read helpers.

Tables managed by this module:

* ``__de_study`` — one row per DE analysis (study).
* ``__de`` — one row per (study_id, term, contrast, gene_id).
* ``de`` — denormalised view joining the above to ``__genes``.

Tables are created lazily on first write. A duckdb without any DE data
has zero DE-related tables and the read API returns an empty list.

Coordination with ``ant-serve``: this module's writer opens the duckdb
read-write, which conflicts with ``ant-serve``'s read-only pool. Callers
should pass ``db_id`` + ``ant_serve_url`` to ``DEWriter``; on a
``Conflicting lock`` error the writer POSTs ``/admin/release/{db_id}``,
retries, then POSTs ``/admin/restore/{db_id}`` on exit. Same dance as
``AnnotationWriter``.

See ``doc/DESIGN_var_and_de.md`` for the design rationale.
"""

from __future__ import annotations

import fcntl
import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import duckdb


def _get_post_ant_serve():
    """Lazy import of ``_post_ant_serve`` from ``anthive_db``.

    Done lazily because ``anthive_db`` pulls in pandas at import time,
    and consumers of this module (notably ``ant-de-ingest``) don't carry
    pandas in their PEP-723 dep header. We only need the helper when
    we actually have to coordinate with a running ant-serve.
    """
    try:
        from anthive_db import _post_ant_serve  # type: ignore
    except ImportError:
        from lib.anthive_db import _post_ant_serve  # type: ignore
    return _post_ant_serve


# ── stable constants ──────────────────────────────────────────────────────────

# Set of anthive __de columns that are first-class (a real column on the
# table).  Anything else mapped by a preset flows into stats_extra (JSON).
_DE_FIRST_CLASS = (
    'log2fc', 'zeff', 'pvalue', 'padj',
    'estimate', 'SE', 'ci_low', 'ci_high',
)


@dataclass
class StudySpec:
    """The metadata describing one DE study — feeds ``__de_study``."""
    study_id: str
    name: Optional[str] = None
    description: Optional[str] = None
    method: Optional[str] = None
    target: str = 'gene'   # 'gene' | 'module'
    reference_type: Optional[str] = None
    covariates: Optional[List[str]] = None    # JSON-serialised at write
    n_tests_total: Optional[int] = None
    n_cells_total: Optional[int] = None
    subset_filter: Optional[Dict[str, Any]] = None
    source: Optional[str] = None
    created_at: Optional[datetime] = None
    notes: Optional[str] = None
    # (v2) Which obs column the 'cluster' dimension of __de rows points at.
    # Set when the DE is per-cluster (e.g. cluster_obs_column='leiden'
    # means contrast='DAM_vs_homeo' for cluster='leiden:3' refers to
    # obs[leiden]==3). NULL for non-cluster DEs. mus's de.cfg key:
    # de_cluster_obs_column.
    cluster_obs_column: Optional[str] = None


@dataclass
class IngestReport:
    """Summary of an ingest() call — what was actually written."""
    study_id: str
    n_rows_inserted: int
    n_terms: int
    n_contrasts: int
    n_genes: int
    n_genes_unmatched: int = 0
    n_genes_total_in_input: int = 0
    sidecar_path: Optional[Path] = None
    warnings: List[str] = field(default_factory=list)


# ── schema ────────────────────────────────────────────────────────────────────

# v1 → v2 (2026-06-19): add cluster_obs_column. Captures the obs-column
# name that 'cluster' values in __de.contrast (or __de.term) reference,
# so the web UI / API can render per-cluster DE next to the cell-level
# categorical of the same name. Nullable — non-cluster DEs leave it
# NULL. Migration: ALTER TABLE __de_study ADD COLUMN IF NOT EXISTS.
DE_SCHEMA_VERSION = 2

_CREATE_DE_STUDY = """
CREATE TABLE IF NOT EXISTS __de_study (
    study_id            VARCHAR PRIMARY KEY,
    name                VARCHAR,
    description         VARCHAR,
    method              VARCHAR,
    target              VARCHAR,
    reference_type      VARCHAR,
    covariates          VARCHAR,         -- JSON list
    n_tests_total       INTEGER,
    n_cells_total       INTEGER,
    subset_filter       VARCHAR,         -- JSON dict
    source              VARCHAR,
    created_at          TIMESTAMP,
    notes               VARCHAR,
    cluster_obs_column  VARCHAR,         -- (v2) which obs column 'cluster' refs
    schema_version      INTEGER DEFAULT """ + str(DE_SCHEMA_VERSION) + """
)
"""

# v1 → v2 column add (idempotent — duckdb honours IF NOT EXISTS).
_MIGRATE_DE_STUDY_V2 = (
    "ALTER TABLE __de_study ADD COLUMN IF NOT EXISTS "
    "cluster_obs_column VARCHAR"
)

_CREATE_DE = """
CREATE TABLE IF NOT EXISTS __de (
    study_id    VARCHAR NOT NULL,
    term        VARCHAR NOT NULL,
    contrast    VARCHAR NOT NULL,
    gene_id     INTEGER NOT NULL,

    -- Core stats (first-class)
    log2fc      DOUBLE,
    zeff        DOUBLE,
    effsize     DOUBLE,
    efftype     VARCHAR,
    pvalue      DOUBLE,
    padj        DOUBLE,

    -- Original-scale model output
    estimate    DOUBLE,
    SE          DOUBLE,
    ci_low      DOUBLE,
    ci_high     DOUBLE,

    -- Method-specific extras (preserved verbatim)
    stats_extra VARCHAR,             -- JSON dict

    PRIMARY KEY (study_id, term, contrast, gene_id)
)
"""

# Indexes for the two main query patterns.
_CREATE_INDEXES = [
    "CREATE INDEX IF NOT EXISTS __de_padj   ON __de (study_id, term, contrast, padj)",
    "CREATE INDEX IF NOT EXISTS __de_effect ON __de (study_id, term, contrast, effsize)",
    "CREATE INDEX IF NOT EXISTS __de_gene   ON __de (gene_id, study_id)",
]

_CREATE_DE_VIEW = """
CREATE OR REPLACE VIEW de AS
SELECT s.study_id, s.name AS study_name, s.method, s.target,
       s.reference_type,
       d.term, d.contrast,
       g.gene_name,
       d.log2fc, d.zeff, d.effsize, d.efftype,
       d.pvalue, d.padj,
       d.estimate, d.SE, d.ci_low, d.ci_high,
       d.stats_extra
FROM __de d
JOIN __de_study s USING (study_id)
JOIN __genes    g USING (gene_id)
"""


def ensure_schema(conn: duckdb.DuckDBPyConnection) -> None:
    """Create __de_study + __de + view + indexes if missing.

    Idempotent. Safe to call on every connection. Called by ``DEWriter``
    before any insert, and by the read API endpoints (they need the
    view to query against, even if it returns 0 rows).

    Also applies forward-only schema migrations — currently:
    * v1 → v2: ADD COLUMN __de_study.cluster_obs_column (nullable).
    Older duckdbs without DE data simply get the v2 shape on create;
    older duckdbs WITH DE data pick up the new column via ALTER.
    """
    conn.execute(_CREATE_DE_STUDY)
    conn.execute(_CREATE_DE)
    # Forward migrations (idempotent).
    conn.execute(_MIGRATE_DE_STUDY_V2)
    for sql in _CREATE_INDEXES:
        conn.execute(sql)
    conn.execute(_CREATE_DE_VIEW)


def has_de_data(conn: duckdb.DuckDBPyConnection) -> bool:
    """Return True if this duckdb has any DE rows.

    Cheap probe used by ``/info`` and ``/de`` to short-circuit when no
    DE is present.
    """
    try:
        n = conn.execute(
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_name = '__de_study'"
        ).fetchone()
        if not n or n[0] == 0:
            return False
        n = conn.execute("SELECT count(*) FROM __de_study").fetchone()
        return bool(n and n[0] > 0)
    except duckdb.CatalogException:
        return False


# ── writer with locking + coordination ────────────────────────────────────────


class DEWriterError(Exception):
    pass


class DEWriter:
    """Context manager — opens a duckdb r/w with locking + ant-serve
    coordination, ensures the DE schema, and exposes ``ingest_rows()``
    and ``replace_study()``.

    Mirrors ``AnnotationWriter`` for the lock-and-coordinate dance; see
    ``lib/anthive_db.py`` for the canonical implementation.
    """

    LOCK_TIMEOUT_SECONDS = 30

    def __init__(
        self,
        duckdb_path: Path,
        *,
        db_id: Optional[str] = None,
        ant_serve_url: Optional[str] = None,
    ):
        self.duckdb_path = Path(duckdb_path)
        if not self.duckdb_path.exists():
            raise DEWriterError(f"duckdb not found: {self.duckdb_path}")
        self.db_id = db_id
        self.ant_serve_url = ant_serve_url.rstrip('/') if ant_serve_url else None
        self.lock_path = self.duckdb_path.with_suffix(
            self.duckdb_path.suffix + '.lock'
        )
        self.rebuilding_path = self.duckdb_path.with_suffix(
            self.duckdb_path.suffix + '.rebuilding'
        )
        self._lock_fd = None
        self._conn: Optional[duckdb.DuckDBPyConnection] = None
        self._released_db = False

    def __enter__(self) -> 'DEWriter':
        if self.rebuilding_path.exists():
            raise DEWriterError(
                f"{self.duckdb_path.name} is being rebuilt by ant-convert "
                f"(sentinel: {self.rebuilding_path}). Refusing to open."
            )
        self._acquire_lock()
        try:
            self._conn = self._open_with_release_retry()
            ensure_schema(self._conn)
        except Exception:
            self._release_lock()
            raise
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        try:
            if self._conn is not None:
                try:
                    self._conn.close()
                except Exception:
                    pass
                self._conn = None
            if self._released_db:
                _get_post_ant_serve()(
                    self.ant_serve_url, f'/admin/restore/{self.db_id}',
                    on_failure='warn',
                )
                self._released_db = False
        finally:
            self._release_lock()

    # ── connection open ───────────────────────────────────────────────────────

    def _open_with_release_retry(self) -> duckdb.DuckDBPyConnection:
        try:
            return duckdb.connect(str(self.duckdb_path), read_only=False)
        except duckdb.IOException as exc:
            if 'Conflicting lock' not in str(exc):
                raise
            if not self.ant_serve_url or not self.db_id:
                raise DEWriterError(
                    f"DuckDB lock conflict on {self.duckdb_path.name}, and "
                    f"no (db_id, ant_serve_url) given for coordination. "
                    f"Either stop ant-serve, or pass --ant-serve-url + a "
                    f"db_id.\n\nOriginal error: {exc}"
                )
            print(f"  ⚠ DuckDB lock held — asking ant-serve to release {self.db_id}…")
            result = _get_post_ant_serve()(
                self.ant_serve_url, f'/admin/release/{self.db_id}',
                on_failure='raise',
            )
            if not (result and result.get('ok')):
                raise DEWriterError(
                    f"ant-serve release of {self.db_id!r} did not succeed: {result}"
                )
            self._released_db = True
            try:
                return duckdb.connect(str(self.duckdb_path), read_only=False)
            except duckdb.IOException as exc2:
                _get_post_ant_serve()(
                    self.ant_serve_url, f'/admin/restore/{self.db_id}',
                    on_failure='ignore',
                )
                self._released_db = False
                raise DEWriterError(
                    f"DuckDB still locked after ant-serve release: {exc2}"
                )

    # ── locking ───────────────────────────────────────────────────────────────

    def _acquire_lock(self) -> None:
        deadline = time.monotonic() + self.LOCK_TIMEOUT_SECONDS
        self._lock_fd = open(self.lock_path, 'w')
        while True:
            try:
                fcntl.flock(self._lock_fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    self._lock_fd.close()
                    self._lock_fd = None
                    raise DEWriterError(
                        f"Could not acquire lock on {self.lock_path} within "
                        f"{self.LOCK_TIMEOUT_SECONDS}s — another writer is active."
                    )
                time.sleep(0.5)

    def _release_lock(self) -> None:
        if self._lock_fd is not None:
            try:
                fcntl.flock(self._lock_fd.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
            try:
                self._lock_fd.close()
            except Exception:
                pass
            self._lock_fd = None
            try:
                self.lock_path.unlink()
            except FileNotFoundError:
                pass

    # ── core writes ───────────────────────────────────────────────────────────

    @property
    def conn(self) -> duckdb.DuckDBPyConnection:
        if self._conn is None:
            raise DEWriterError("DEWriter is not open (use as a context manager).")
        return self._conn

    def study_exists(self, study_id: str) -> bool:
        row = self.conn.execute(
            "SELECT count(*) FROM __de_study WHERE study_id = ?", [study_id]
        ).fetchone()
        return bool(row and row[0] > 0)

    def get_study(self, study_id: str) -> Optional[Dict[str, Any]]:
        cols = ['study_id', 'name', 'description', 'method', 'target',
                'reference_type', 'covariates', 'n_tests_total',
                'n_cells_total', 'subset_filter', 'source', 'created_at',
                'notes', 'cluster_obs_column', 'schema_version']
        row = self.conn.execute(
            f"SELECT {', '.join(cols)} FROM __de_study WHERE study_id = ?",
            [study_id],
        ).fetchone()
        if row is None:
            return None
        return dict(zip(cols, row))

    def upsert_study(self, spec: StudySpec) -> None:
        """Insert or update the __de_study row.

        Idempotent. If the row already exists, all non-None fields in
        ``spec`` overwrite the existing values; existing fields are
        preserved when the spec has ``None`` for them. This lets the
        ingester refresh metadata without losing fields supplied by an
        earlier call.
        """
        existing = self.get_study(spec.study_id)
        # Merge: prefer spec's non-None fields, fall back to existing.
        merged = {
            'study_id':       spec.study_id,
            'name':           spec.name           or (existing or {}).get('name'),
            'description':    spec.description    or (existing or {}).get('description'),
            'method':         spec.method         or (existing or {}).get('method'),
            'target':         spec.target         or (existing or {}).get('target') or 'gene',
            'reference_type': spec.reference_type or (existing or {}).get('reference_type'),
            'covariates':     (json.dumps(spec.covariates) if spec.covariates is not None
                               else (existing or {}).get('covariates')),
            'n_tests_total':  spec.n_tests_total  if spec.n_tests_total is not None
                              else (existing or {}).get('n_tests_total'),
            'n_cells_total':  spec.n_cells_total  if spec.n_cells_total is not None
                              else (existing or {}).get('n_cells_total'),
            'subset_filter':  (json.dumps(spec.subset_filter) if spec.subset_filter is not None
                               else (existing or {}).get('subset_filter')),
            'source':         spec.source         or (existing or {}).get('source'),
            'created_at':     spec.created_at     or (existing or {}).get('created_at')
                              or datetime.now(timezone.utc),
            'notes':          spec.notes          or (existing or {}).get('notes'),
            'cluster_obs_column':
                spec.cluster_obs_column or (existing or {}).get('cluster_obs_column'),
        }
        self.conn.execute(
            "DELETE FROM __de_study WHERE study_id = ?", [spec.study_id]
        )
        self.conn.execute(
            "INSERT INTO __de_study (study_id, name, description, method, "
            "target, reference_type, covariates, n_tests_total, "
            "n_cells_total, subset_filter, source, created_at, notes, "
            "cluster_obs_column) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [merged[k] for k in (
                'study_id', 'name', 'description', 'method', 'target',
                'reference_type', 'covariates', 'n_tests_total',
                'n_cells_total', 'subset_filter', 'source', 'created_at',
                'notes', 'cluster_obs_column',
            )],
        )

    def delete_study(self, study_id: str) -> int:
        """Remove a study and all its DE rows.

        Returns the number of __de rows deleted.
        """
        n = self.conn.execute(
            "SELECT count(*) FROM __de WHERE study_id = ?", [study_id]
        ).fetchone()
        n_rows = n[0] if n else 0
        self.conn.execute("DELETE FROM __de WHERE study_id = ?", [study_id])
        self.conn.execute("DELETE FROM __de_study WHERE study_id = ?", [study_id])
        return n_rows

    def ingest_rows(
        self,
        *,
        spec: StudySpec,
        rows_table: str,
        gene_col: str = 'gene',
        term_col: Optional[str] = 'term',
        contrast_col: Optional[str] = 'contrast',
        fixed_term: Optional[str] = None,
        fixed_contrast: Optional[str] = None,
    ) -> Tuple[int, int]:
        """Move rows from a staging duckdb view into ``__de``.

        ``rows_table`` is the name of a DuckDB view or table on the same
        connection that holds the already-mapped rows (one row per
        contrast × gene), with anthive column names. The caller is
        responsible for registering it (typically a CSV → DuckDB read
        followed by ``SELECT … AS gene, … AS log2fc, …``).

        Either ``term_col`` (the file has a term column) OR
        ``fixed_term`` (the term is constant for this file, e.g. DESeq2
        one-file-one-contrast) must be set. Same for contrast.

        Returns ``(rows_inserted, genes_unmatched)``.
        """
        if (term_col is None) == (fixed_term is None):
            raise DEWriterError(
                "ingest_rows: exactly one of term_col / fixed_term must be set"
            )
        if (contrast_col is None) == (fixed_contrast is None):
            raise DEWriterError(
                "ingest_rows: exactly one of contrast_col / fixed_contrast must be set"
            )

        # Persist the study row first (FK target).
        self.upsert_study(spec)

        term_expr = (f'"{term_col}"' if term_col else
                     f"'{fixed_term}'::VARCHAR")
        contrast_expr = (f'"{contrast_col}"' if contrast_col else
                         f"'{fixed_contrast}'::VARCHAR")
        gene_expr = f'"{gene_col}"'

        # Determine which optional first-class columns the staging table has.
        staging_cols = {
            row[0] for row in
            self.conn.execute(f'DESCRIBE SELECT * FROM {rows_table}').fetchall()
        }

        # R / pandas often write "NA" as a string literal for missing
        # values; convert those to NULL before casting to DOUBLE so the
        # ingest doesn't choke on mixed-type columns. TRY_CAST also turns
        # any other non-numeric string into NULL silently — sensible for
        # DE inputs where everything other than NA should be a number.
        # Keep two parallel lists: the bare expression (used in COALESCE
        # / CASE later), and the "expr AS alias" form for the SELECT list.
        first_class_expr: Dict[str, str] = {}
        for col in _DE_FIRST_CLASS:
            if col in staging_cols:
                first_class_expr[col] = (
                    f'TRY_CAST(NULLIF(CAST("{col}" AS VARCHAR), \'NA\') AS DOUBLE)'
                )
            else:
                first_class_expr[col] = 'NULL::DOUBLE'

        # stats_extra: any non-anthive numeric column (preset's mapped
        # extras like base_mean, score, pct_in, pct_out). Caller must
        # have already named them; we just bundle leftovers.
        from de_presets import ANTHIVE_DE_COLUMNS, STATS_EXTRA_COLUMNS  # type: ignore
        extra_cols = [c for c in staging_cols
                      if c in STATS_EXTRA_COLUMNS and c not in ANTHIVE_DE_COLUMNS]
        if extra_cols:
            kv_parts = []
            for col in extra_cols:
                kv_parts.append(f"'{col}', \"{col}\"")
            stats_extra_expr = (
                "to_json(struct_pack("
                + ", ".join(f'"{c}" := "{c}"' for c in extra_cols)
                + "))"
            )
        else:
            stats_extra_expr = "NULL::VARCHAR"

        # Insert with gene_name → gene_id resolution + effsize/efftype derivation.
        sql = f"""
            INSERT OR REPLACE INTO __de (
                study_id, term, contrast, gene_id,
                log2fc, zeff, effsize, efftype,
                pvalue, padj,
                estimate, SE, ci_low, ci_high,
                stats_extra
            )
            SELECT
                '{_sql_escape(spec.study_id)}'                    AS study_id,
                CAST({term_expr} AS VARCHAR)                       AS term,
                CAST({contrast_expr} AS VARCHAR)                   AS contrast,
                g.gene_id                                          AS gene_id,
                {first_class_expr['log2fc']}                          AS log2fc,
                {first_class_expr['zeff']}                            AS zeff,
                COALESCE({first_class_expr['log2fc']},
                         {first_class_expr['zeff']})                  AS effsize,
                CASE WHEN {first_class_expr['log2fc']} IS NOT NULL
                     THEN 'log2fc'
                     WHEN {first_class_expr['zeff']}   IS NOT NULL
                     THEN 'zeff'
                     ELSE NULL END                                    AS efftype,
                {first_class_expr['pvalue']}                          AS pvalue,
                {first_class_expr['padj']}                            AS padj,
                {first_class_expr['estimate']}                        AS estimate,
                {first_class_expr['SE']}                              AS SE,
                {first_class_expr['ci_low']}                          AS ci_low,
                {first_class_expr['ci_high']}                         AS ci_high,
                {stats_extra_expr}                                 AS stats_extra
            FROM {rows_table} r
            JOIN __genes g ON g.gene_name = CAST(r.{gene_expr} AS VARCHAR)
        """
        n_before = self._row_count(spec.study_id)
        self.conn.execute(sql)
        n_after = self._row_count(spec.study_id)

        n_input = self.conn.execute(
            f'SELECT count(*) FROM {rows_table}'
        ).fetchone()[0]
        n_matched = self.conn.execute(
            f'SELECT count(*) FROM {rows_table} r '
            f'JOIN __genes g ON g.gene_name = CAST(r.{gene_expr} AS VARCHAR)'
        ).fetchone()[0]
        n_unmatched = n_input - n_matched

        return (n_after - n_before, n_unmatched)

    def _row_count(self, study_id: str) -> int:
        return self.conn.execute(
            "SELECT count(*) FROM __de WHERE study_id = ?", [study_id]
        ).fetchone()[0]


def _sql_escape(value: str) -> str:
    """Escape a string literal for inline SQL."""
    return value.replace("'", "''")


# ── sidecar parquet I/O ───────────────────────────────────────────────────────


def sidecar_path_for(h5ad_path: Path) -> Path:
    """Return the canonical sidecar path for a dataset's DE data.

    ``foo.h5ad`` → ``foo.de.parquet`` (right next to the h5ad).
    """
    name = h5ad_path.name
    # Strip the .h5ad suffix without losing earlier dots in the stem.
    if name.endswith('.h5ad'):
        base = name[:-len('.h5ad')]
    else:
        base = h5ad_path.stem
    return h5ad_path.parent / f"{base}.de.parquet"


def write_sidecar(
    sidecar: Path,
    duckdb_path: Path,
    *,
    only_study: Optional[str] = None,
) -> int:
    """Export the duckdb's DE rows to a parquet sidecar.

    Single file per dataset; the ``study_id`` column tells consumers
    which study each row belongs to. If ``only_study`` is given,
    only that study is exported (other studies in any existing sidecar
    are merged back in, so re-exporting one study doesn't drop others).

    Returns the number of rows written.
    """
    sidecar = Path(sidecar)
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    tmp = sidecar.with_suffix(sidecar.suffix + '.tmp')

    con = duckdb.connect()
    con.execute(f"ATTACH '{duckdb_path}' AS src (READ_ONLY)")

    # If a sidecar already exists AND we're only refreshing one study,
    # union the un-touched studies' rows from the old parquet with the
    # fresh duckdb rows for `only_study`.
    if only_study and sidecar.exists():
        union_sql = f"""
            WITH old_sidecar AS (
                SELECT * FROM read_parquet('{sidecar}')
                WHERE study_id <> '{_sql_escape(only_study)}'
            ),
            new_from_db AS (
                SELECT * FROM src.de WHERE study_id = '{_sql_escape(only_study)}'
            )
            SELECT * FROM old_sidecar
            UNION ALL BY NAME
            SELECT * FROM new_from_db
        """
        n_sql = f"SELECT count(*) FROM ({union_sql})"
        export_sql = f"COPY ({union_sql}) TO '{tmp}' (FORMAT PARQUET, COMPRESSION ZSTD)"
    else:
        where = f"WHERE study_id = '{_sql_escape(only_study)}'" if only_study else ""
        n_sql = f"SELECT count(*) FROM src.de {where}"
        export_sql = (f"COPY (SELECT * FROM src.de {where}) "
                      f"TO '{tmp}' (FORMAT PARQUET, COMPRESSION ZSTD)")

    n = con.execute(n_sql).fetchone()[0]
    if n == 0:
        # Nothing to write; remove a stale sidecar if it has zero rows for everything.
        if sidecar.exists() and not only_study:
            sidecar.unlink()
        con.close()
        return 0
    con.execute(export_sql)
    con.close()
    tmp.replace(sidecar)
    return n


def read_sidecar_studies(sidecar: Path) -> List[Dict[str, Any]]:
    """Quick peek: list the studies stored in a sidecar parquet.

    Returns ``[{study_id, n_rows, n_terms, n_contrasts}, ...]``.
    Used by ant-convert's post-rebuild re-ingestion to know which
    studies need to be re-created.
    """
    if not sidecar.exists():
        return []
    con = duckdb.connect()
    rows = con.execute(f"""
        SELECT
            study_id,
            count(*) AS n_rows,
            count(DISTINCT term) AS n_terms,
            count(DISTINCT (term || '|' || contrast)) AS n_contrasts
        FROM read_parquet('{sidecar}')
        GROUP BY study_id
        ORDER BY study_id
    """).fetchall()
    con.close()
    return [
        {'study_id': r[0], 'n_rows': r[1], 'n_terms': r[2], 'n_contrasts': r[3]}
        for r in rows
    ]


def reimport_sidecar(
    duckdb_path: Path,
    sidecar: Path,
) -> Dict[str, int]:
    """Re-ingest a sidecar parquet into a freshly-built duckdb.

    Called by ``ant-convert`` after a full rebuild — the duckdb's
    ``__de`` tables are gone, but the sidecar parquet survived. We
    re-create them.

    The sidecar's columns mirror the ``de`` view: it has ``study_name``
    + first-class stats + ``stats_extra`` JSON, and ``gene_name``
    instead of ``gene_id``. We split it back into ``__de_study`` and
    ``__de`` rows.

    Returns ``{study_id: n_rows_inserted, ...}``.

    Does NOT need locking — ant-convert is the only writer at this
    point in the rebuild.
    """
    if not sidecar.exists():
        return {}

    con = duckdb.connect(str(duckdb_path), read_only=False)
    ensure_schema(con)

    # Pull study-level metadata from any of its rows. The sidecar's
    # `de` view shape doesn't carry every field on __de_study (covariates
    # / source / notes / subset_filter are in study metadata only), so
    # the reimport carries whatever it has and the rest stays NULL.
    studies = con.execute(f"""
        SELECT DISTINCT study_id, study_name, method, target, reference_type
        FROM read_parquet('{sidecar}')
    """).fetchall()

    result: Dict[str, int] = {}
    for row in studies:
        study_id, study_name, method, target, reference_type = row
        spec = StudySpec(
            study_id=study_id, name=study_name, method=method,
            target=target or 'gene',
            reference_type=reference_type,
            source=f"reimport_sidecar:{sidecar.name}",
            created_at=datetime.now(timezone.utc),
        )
        # Persist the study row (DEWriter.upsert_study reuses this logic;
        # we inline it because we don't need locking here)
        con.execute(
            "DELETE FROM __de_study WHERE study_id = ?", [spec.study_id]
        )
        con.execute(
            "INSERT INTO __de_study (study_id, name, method, target, "
            "reference_type, source, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [spec.study_id, spec.name, spec.method, spec.target,
             spec.reference_type, spec.source, spec.created_at],
        )
        # Re-create __de rows joining via gene_name → gene_id.
        sql = f"""
            INSERT OR REPLACE INTO __de (
                study_id, term, contrast, gene_id,
                log2fc, zeff, effsize, efftype, pvalue, padj,
                estimate, SE, ci_low, ci_high, stats_extra
            )
            SELECT
                p.study_id, p.term, p.contrast, g.gene_id,
                p.log2fc, p.zeff, p.effsize, p.efftype, p.pvalue, p.padj,
                p.estimate, p.SE, p.ci_low, p.ci_high, p.stats_extra
            FROM read_parquet('{sidecar}') p
            JOIN __genes g ON g.gene_name = p.gene_name
            WHERE p.study_id = '{_sql_escape(study_id)}'
        """
        con.execute(sql)
        n = con.execute(
            "SELECT count(*) FROM __de WHERE study_id = ?", [study_id]
        ).fetchone()[0]
        result[study_id] = n

    con.close()
    return result


# ── read helpers (used by the API) ────────────────────────────────────────────


def list_studies(conn: duckdb.DuckDBPyConnection) -> List[Dict[str, Any]]:
    """Return the studies in this duckdb, with row counts.

    Empty list if the DE schema doesn't exist. Used by GET /de.
    """
    if not has_de_data(conn):
        return []
    rows = conn.execute("""
        SELECT s.study_id, s.name, s.description, s.method, s.target,
               s.reference_type, s.covariates, s.n_tests_total,
               s.n_cells_total, s.source, s.created_at, s.notes,
               (SELECT count(*)            FROM __de WHERE study_id = s.study_id) AS n_rows,
               (SELECT count(DISTINCT term) FROM __de WHERE study_id = s.study_id) AS n_terms,
               (SELECT count(DISTINCT term || '|' || contrast)
                  FROM __de WHERE study_id = s.study_id) AS n_contrasts
        FROM __de_study s
        ORDER BY s.created_at DESC NULLS LAST, s.study_id
    """).fetchall()
    cols = ('study_id', 'name', 'description', 'method', 'target',
            'reference_type', 'covariates', 'n_tests_total',
            'n_cells_total', 'source', 'created_at', 'notes',
            'n_rows', 'n_terms', 'n_contrasts')
    out = []
    for row in rows:
        d = dict(zip(cols, row))
        for k in ('covariates',):
            if d[k]:
                try:
                    d[k] = json.loads(d[k])
                except (TypeError, json.JSONDecodeError):
                    pass
        out.append(d)
    return out


def get_study(conn: duckdb.DuckDBPyConnection, study_id: str) -> Optional[Dict[str, Any]]:
    """Return one study's metadata + a per-term row count.

    None if no such study.
    """
    if not has_de_data(conn):
        return None
    studies = [s for s in list_studies(conn) if s['study_id'] == study_id]
    if not studies:
        return None
    study = studies[0]
    terms = conn.execute("""
        SELECT term, count(DISTINCT contrast) AS n_contrasts, count(*) AS n_rows
        FROM __de WHERE study_id = ?
        GROUP BY term ORDER BY term
    """, [study_id]).fetchall()
    study['terms'] = [
        {'term': t[0], 'n_contrasts': t[1], 'n_rows': t[2]} for t in terms
    ]
    return study


def list_contrasts(
    conn: duckdb.DuckDBPyConnection,
    study_id: str, term: str,
) -> List[Dict[str, Any]]:
    """Return the contrasts within one (study, term) + summary stats.

    Empty list if (study, term) is unknown.
    """
    if not has_de_data(conn):
        return []
    rows = conn.execute("""
        SELECT contrast,
               count(*) AS n_genes,
               count(*) FILTER (WHERE padj < 0.05) AS n_sig_05,
               count(*) FILTER (WHERE padj < 0.01) AS n_sig_01,
               count(*) FILTER (WHERE padj < 0.001) AS n_sig_001
        FROM __de WHERE study_id = ? AND term = ?
        GROUP BY contrast ORDER BY contrast
    """, [study_id, term]).fetchall()
    return [
        {'contrast': r[0], 'n_genes': r[1],
         'n_sig_padj_lt_0.05': r[2],
         'n_sig_padj_lt_0.01': r[3],
         'n_sig_padj_lt_0.001': r[4]}
        for r in rows
    ]


def get_contrast_rows(
    conn: duckdb.DuckDBPyConnection,
    study_id: str, term: str, contrast: str,
    *,
    sort: str = 'padj',         # 'padj' | 'effsize' | 'estimate'
    direction: str = 'both',     # 'up' | 'down' | 'both'
    padj_max: Optional[float] = None,
    abs_log2fc_min: Optional[float] = None,
    limit: int = 200,
    offset: int = 0,
) -> List[Dict[str, Any]]:
    """Paged gene rows for one (study, term, contrast).

    Used by GET /de/{study}/terms/{term}/contrasts/{contrast}.
    """
    if sort not in ('padj', 'effsize', 'estimate'):
        raise ValueError(f"sort must be 'padj' | 'effsize' | 'estimate', got {sort!r}")
    if direction not in ('up', 'down', 'both'):
        raise ValueError(f"direction must be 'up' | 'down' | 'both', got {direction!r}")
    if not has_de_data(conn):
        return []

    where = ['d.study_id = ?', 'd.term = ?', 'd.contrast = ?']
    params: List[Any] = [study_id, term, contrast]
    if padj_max is not None:
        where.append('d.padj <= ?')
        params.append(padj_max)
    if abs_log2fc_min is not None:
        where.append('abs(d.log2fc) >= ?')
        params.append(abs_log2fc_min)
    if direction == 'up':
        where.append('d.effsize > 0')
    elif direction == 'down':
        where.append('d.effsize < 0')

    # padj asc puts most-significant first; effsize / estimate sort
    # by magnitude (top up + top down via abs() unless direction set).
    if sort == 'padj':
        order = 'd.padj ASC NULLS LAST'
    elif direction in ('up', 'down'):
        order = f"d.{sort} {'DESC' if direction == 'up' else 'ASC'} NULLS LAST"
    else:
        order = f"abs(d.{sort}) DESC NULLS LAST"

    sql = f"""
        SELECT g.gene_name,
               d.log2fc, d.zeff, d.effsize, d.efftype,
               d.pvalue, d.padj,
               d.estimate, d.SE, d.ci_low, d.ci_high,
               d.stats_extra
        FROM __de d
        JOIN __genes g USING (gene_id)
        WHERE {' AND '.join(where)}
        ORDER BY {order}
        LIMIT ? OFFSET ?
    """
    params.extend([limit, offset])
    rows = conn.execute(sql, params).fetchall()
    cols = ('gene_name', 'log2fc', 'zeff', 'effsize', 'efftype',
            'pvalue', 'padj', 'estimate', 'SE', 'ci_low', 'ci_high',
            'stats_extra')
    out = []
    for row in rows:
        d = dict(zip(cols, row))
        if d['stats_extra']:
            try:
                d['stats_extra'] = json.loads(d['stats_extra'])
            except (TypeError, json.JSONDecodeError):
                pass
        out.append(d)
    return out


def get_gene_rows(
    conn: duckdb.DuckDBPyConnection,
    gene_name: str,
) -> List[Dict[str, Any]]:
    """All DE rows for one gene across every study/term/contrast.

    Used by GET /de/by-gene/{gene} — feeds forest plots.
    """
    if not has_de_data(conn):
        return []
    rows = conn.execute("""
        SELECT s.study_id, s.name AS study_name, s.method, d.term, d.contrast,
               d.log2fc, d.zeff, d.effsize, d.efftype, d.pvalue, d.padj,
               d.estimate, d.SE, d.ci_low, d.ci_high, d.stats_extra
        FROM __de d
        JOIN __genes g USING (gene_id)
        JOIN __de_study s USING (study_id)
        WHERE g.gene_name = ?
        ORDER BY s.study_id, d.term, d.contrast
    """, [gene_name]).fetchall()
    cols = ('study_id', 'study_name', 'method', 'term', 'contrast',
            'log2fc', 'zeff', 'effsize', 'efftype', 'pvalue', 'padj',
            'estimate', 'SE', 'ci_low', 'ci_high', 'stats_extra')
    out = []
    for row in rows:
        d = dict(zip(cols, row))
        if d['stats_extra']:
            try:
                d['stats_extra'] = json.loads(d['stats_extra'])
            except (TypeError, json.JSONDecodeError):
                pass
        out.append(d)
    return out
