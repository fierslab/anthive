"""SQLite-backed cache for /gene_stats results.

Why this exists
---------------
Per-dataset `db.get_gene_stats()` is 0.5–2.4 s on a real catalog (107 dbs),
so a single `/gene_stats_all?genes=APOE` sequential pass took ~50 s with
case_sensitive=true and timed out (>4 min) with the default LOWER()
case-fold lookup. The web UI's `/genes/{gene}` atlas page can't consume
that latency.

Key design choices
------------------
- **Content-stable fingerprint.** Cache key includes the dataset's mus
  sha256 (the `.h5ad.mus` `sha256` field — same as `h5ad_inspected_sha256`).
  Since `.h5ad` content drives the duckdb content (via ant-convert), this
  sha is the right invariant: re-running ant-convert without an h5ad
  change still produces the same stats.
- **Coarse mtime bucket.** Added as a second component so a touched-but-
  not-rebuilt duckdb still invalidates, but at day-granularity — rsync
  preserving mtime is fine, an explicit rebuild crosses the bucket.
- **Long TTL.** 90 days. Stats are deterministic w.r.t. the fingerprint;
  TTL is just a defense against any subtle invariant we missed.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, Optional

CACHE_TTL_SECONDS = 60 * 60 * 24 * 90   # 90 days

_SCHEMA = """
CREATE TABLE IF NOT EXISTS gene_stats (
    db_id          TEXT NOT NULL,
    gene_query_lc  TEXT NOT NULL,    -- always lowercased; case-sensitivity captured separately
    case_sensitive INTEGER NOT NULL,
    layer          TEXT NOT NULL,
    fingerprint    TEXT NOT NULL,
    stats_json     TEXT NOT NULL,
    created_at     INTEGER NOT NULL,
    PRIMARY KEY (db_id, gene_query_lc, case_sensitive, layer, fingerprint)
);
CREATE INDEX IF NOT EXISTS idx_db_fingerprint ON gene_stats (db_id, fingerprint);
"""


class GeneStatsCache:
    """SQLite cache for per-(db, gene) expression stats.

    Thread-safe for concurrent reads; writes serialize on the connection.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False because ant-serve calls into this from
        # both the asyncio event loop and worker threads dispatched via
        # asyncio.to_thread / ThreadPoolExecutor.
        self._conn = sqlite3.connect(
            str(self.path), check_same_thread=False, isolation_level=None
        )
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(_SCHEMA)

    @staticmethod
    def fingerprint(mus_sha256: Optional[str], duckdb_mtime: float) -> Optional[str]:
        """Build the cache key component that captures dataset content.

        Returns None when the mus sha is unknown — caller should skip the
        cache rather than risk a stale entry under an empty fingerprint.

        mtime is bucketed to 1-day granularity: a same-second touch must
        not invalidate (rsync-with-mtime would otherwise reset everything),
        but a real rebuild crosses the bucket on the same day in practice
        because ant-convert writes a fresh file.
        """
        if not mus_sha256:
            return None
        mtime_bucket = int(duckdb_mtime // 86400) if duckdb_mtime else 0
        # 16 chars of sha is plenty — 2^64 collision space, well under
        # the ~100 datasets per fingerprint we'd ever have.
        return f"{mus_sha256[:16]}:{mtime_bucket}"

    def get(
        self,
        db_id: str,
        gene_query: str,
        case_sensitive: bool,
        layer: str,
        fingerprint: str,
    ) -> Optional[Dict[str, Any]]:
        row = self._conn.execute(
            "SELECT stats_json, created_at FROM gene_stats "
            "WHERE db_id=? AND gene_query_lc=? AND case_sensitive=? "
            "  AND layer=? AND fingerprint=?",
            (db_id, gene_query.lower(), int(case_sensitive), layer, fingerprint),
        ).fetchone()
        if not row:
            return None
        stats_json, created_at = row
        if time.time() - created_at > CACHE_TTL_SECONDS:
            return None
        try:
            return json.loads(stats_json)
        except (ValueError, TypeError):
            return None

    def put(
        self,
        db_id: str,
        gene_query: str,
        case_sensitive: bool,
        layer: str,
        fingerprint: str,
        stats: Dict[str, Any],
    ) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO gene_stats "
            "(db_id, gene_query_lc, case_sensitive, layer, fingerprint, stats_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                db_id,
                gene_query.lower(),
                int(case_sensitive),
                layer,
                fingerprint,
                json.dumps(stats),
                int(time.time()),
            ),
        )

    def gc_stale(self, keep_fingerprints_per_db: Dict[str, str]) -> int:
        """Drop entries whose fingerprint no longer matches the current one.

        Returns number of rows deleted. Safe to call rarely (or never —
        SQLite VACUUM will reclaim if file grows).
        """
        deleted = 0
        for db_id, current_fp in keep_fingerprints_per_db.items():
            cur = self._conn.execute(
                "DELETE FROM gene_stats WHERE db_id=? AND fingerprint != ?",
                (db_id, current_fp),
            )
            deleted += cur.rowcount or 0
        return deleted

    def stats(self) -> Dict[str, Any]:
        """Cache-wide stats for /admin/gene-stats-cache/stats."""
        n_rows = self._conn.execute("SELECT COUNT(*) FROM gene_stats").fetchone()[0]
        n_dbs = self._conn.execute(
            "SELECT COUNT(DISTINCT db_id) FROM gene_stats"
        ).fetchone()[0]
        n_genes = self._conn.execute(
            "SELECT COUNT(DISTINCT gene_query_lc) FROM gene_stats"
        ).fetchone()[0]
        oldest = self._conn.execute(
            "SELECT MIN(created_at) FROM gene_stats"
        ).fetchone()[0]
        size = self.path.stat().st_size if self.path.exists() else 0
        return {
            "rows": n_rows,
            "distinct_databases": n_dbs,
            "distinct_genes": n_genes,
            "oldest_entry_unix": oldest,
            "ttl_seconds": CACHE_TTL_SECONDS,
            "db_path": str(self.path),
            "db_size_bytes": size,
        }

    def clear(self) -> int:
        """Wipe every entry. Returns rows deleted."""
        cur = self._conn.execute("DELETE FROM gene_stats")
        return cur.rowcount or 0

    def close(self) -> None:
        self._conn.close()
