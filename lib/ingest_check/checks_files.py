"""File level checks: hash, size, name, h5ad structure, DuckDB integrity."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

from . import Outcome, check, fail, ok, warn
from .context import MODE_EXISTING
from .scratch import isolated_copy_for_check

_NAME_PART = re.compile(r'^[A-Za-z0-9._-]+$')


@check('files.sha256', needs=('h5ad',))
def files_sha256(ctx) -> Outcome:
    """Hash of the h5ad against the declared hash and the ``sha256`` in the ``.md``."""
    actual = ctx.sha256
    declared = ctx.declared_sha256
    in_md = ctx.md.get('sha256')
    for label, expected in (('declared hash', declared), ('sha256 in the .md', in_md)):
        if expected and str(expected).lower() != actual:
            fix = ('Re-upload the file, or correct the hash. The .md sha256 is filled in by the server for new '
                   'datasets; remove it if unsure.')
            details = {'sha256': actual, 'expected': str(expected).lower()}
            if label == 'sha256 in the .md' and ctx.mode == MODE_EXISTING:
                return warn(f'the h5ad hash does not match the {label} (stale catalogue entry)',
                            'Refresh the .md (sha256, n_obs, layer_stats) with ant build-all.', **details)
            return fail(f'the h5ad hash does not match the {label}', fix, **details)
    if not declared and not in_md:
        return ok('hash computed; nothing declared to compare with', sha256=actual)
    return ok('hash matches', sha256=actual)


@check('files.size', needs=('h5ad',))
def files_size(ctx) -> Outcome:
    """File size against ``datasets_upload_max_gb``."""
    size = ctx.h5ad_path.stat().st_size
    limit_bytes = ctx.settings.max_upload_gb * 1024 ** 3
    details = {'size_bytes': size, 'limit_gb': ctx.settings.max_upload_gb}
    if size > limit_bytes:
        return fail(f'{size / 1024 ** 3:.1f} GB is over the {ctx.settings.max_upload_gb:g} GB limit',
                    'Split the dataset or ask an admin to raise datasets_upload_max_gb.', **details)
    return ok(f'{size / 1024 ** 3:.2f} GB', **details)


@check('files.filename')
def files_filename(ctx) -> Outcome:
    """Basename and group folder name follow the naming rules (no path tricks)."""
    from h5ad_utils import validate_filename  # noqa: PLC0415

    valid, message = validate_filename(f'{ctx.basename}.h5ad')
    group_name = ctx.group_dir.name
    if not valid:
        return fail(message or 'invalid file name', 'Rename the file: letters, digits, dot, dash, underscore; '
                    'at most 50 characters before .h5ad.', basename=ctx.basename)
    if not _NAME_PART.match(group_name) or group_name.startswith('_') or group_name.startswith('.'):
        return fail(f'group folder name {group_name!r} is not allowed',
                    'Use letters, digits, dot, dash and underscore; do not start with _ or a dot.',
                    group=group_name)
    return ok('file and group names are valid', basename=ctx.basename, group=group_name)


@check('h5ad.structure', needs=('h5ad',))
def h5ad_structure(ctx) -> Outcome:
    """The h5ad opens and has X (or layers), obs and var."""
    from h5ad_utils import get_h5ad_info, validate_structure  # noqa: PLC0415

    try:
        adata = ctx.adata
        problems = validate_structure(adata)
        info = get_h5ad_info(adata)
    except Exception as exc:  # noqa: BLE001 - a broken file raises anything from OSError to KeyError
        return fail(f'the h5ad cannot be read ({type(exc).__name__})', 'Check that the file is a complete AnnData '
                    'h5ad; re-save it with anndata and re-upload.', error=str(exc)[:200])
    details = {'n_obs': info['n_cells'], 'n_vars': info['n_genes'], 'layers': info['layers'],
               'obsm_keys': info['obsm_keys'], 'n_obs_columns': info['n_obs_columns']}
    if problems:
        return fail('; '.join(problems), 'Write a complete AnnData object (X, obs, var) and re-upload.', **details)
    if not info['obsm_keys']:
        return warn('no embedding in obsm (the dataset page needs at least one X_* embedding, UMAP preferred)',
                    'Add adata.obsm["X_umap"] before saving the h5ad.', **details)
    return ok(f'{info["n_cells"]} cells x {info["n_genes"]} genes', **details)


def _report_lines(output: str) -> list:
    """Lines of the tool's ``WARNINGS & ERRORS`` block, without the banner and the final verdict."""
    lines: list = []
    inside = False
    for raw in output.splitlines():
        text = raw.strip()
        if text == 'WARNINGS & ERRORS':
            inside = True
            continue
        if inside and set(text) == {'='}:
            if lines:
                break
            continue
        if inside and text and not text.startswith('(Warnings/errors logged'):
            lines.append(text)
    return lines


_MISSING_SCORE_COLUMN = re.compile(r'^-?\s*__obsnum\.(\S+) \(numerical obs column missing\)$')


def _covered_by_scores(lines: list, database: Path) -> bool:
    """True when every finding is a numerical obs column that lives in the ``.scores.parquet`` sidecar.

    ``ant-score`` moved module scores out of ``__obsnum`` into the sidecar, but the ``.md`` still lists
    them in ``obsnum_include`` and ``ant-duckdb-check`` does not know the sidecar.
    """
    from scores_parquet import scores_path  # noqa: PLC0415

    names = [match.group(1) for match in (_MISSING_SCORE_COLUMN.match(line) for line in lines
                                           if not line.rstrip().endswith(':')) if match]
    findings = [line for line in lines if not line.rstrip().endswith(':')]
    sidecar = scores_path(database)
    if not names or len(names) != len(findings) or not sidecar.is_file():
        return False
    import duckdb  # noqa: PLC0415

    connection = duckdb.connect(':memory:')
    try:
        from anthive_db import AnthiveDB  # noqa: PLC0415

        stored = {AnthiveDB._score_col_name(source, catalog, module) for source, catalog, module in  # noqa: SLF001
                  connection.execute('SELECT DISTINCT source, catalog, module FROM read_parquet(?)',
                                     [str(sidecar)]).fetchall()}
    finally:
        connection.close()
    return set(names) <= stored


@check('duckdb.integrity', needs=('duckdb',))
def duckdb_integrity(ctx) -> Outcome:
    """``bin/ingest/ant-duckdb-check`` on a private copy of the (scratch) ``.duckdb``."""
    path = ctx.duckdb
    if path is None:
        return fail('no .duckdb could be built: ' + (ctx.convert_error or 'scratch convert not possible '
                                                       '(h5ad or .md missing, or --no-convert without a database)'),
                    'Fix the h5ad or the .md keys that ant-convert reads (layers_include, obsm_include, '
                    'obscat_include); see the message.', convert_error=ctx.convert_error)
    script = Path(__file__).resolve().parents[2] / 'bin' / 'ingest' / 'ant-duckdb-check'
    ctx.scratch_root.mkdir(parents=True, exist_ok=True)
    # ant-duckdb-check writes (it builds __gene_bins, may edit the .md, logs to ./ant-<date>.log): only ever
    # run it on a private copy, with the scratch dir as working directory.
    target = isolated_copy_for_check(path, ctx.md_path, ctx.scratch_root)
    result = subprocess.run([str(script), str(target)], capture_output=True, text=True, cwd=ctx.scratch_root,
                            check=False)
    lines = _report_lines(result.stdout)
    if result.returncode != 0 and _covered_by_scores(lines, target):
        return warn('score columns listed in the .md are in the .scores.parquet sidecar, which this tool does not '
                    'know; nothing else is wrong', 'Nothing to fix.', findings=lines[:15])
    if result.returncode != 0:
        return fail('the DuckDB file fails the integrity check', 'Fix the h5ad or .md so ant-convert produces a '
                    'consistent database.', exit_code=result.returncode, findings=lines[:15])
    if lines:
        return warn('the DuckDB file passes with warnings', 'See the findings.', findings=lines[:15])
    return ok('schema and counts are consistent')
