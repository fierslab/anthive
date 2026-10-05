"""Facet mapping: how author columns project onto ``ant_*`` facets.

Covers ``lib/facet_mapping.py`` (rename, value rename, hidden values,
derive with default, constant, native, redaction, gap hints), the facet
definitions in ``lib/obscol_tags_vocab.yaml`` and the ant-serve endpoints
``GET /databases/{id}/facets`` and ``GET /facets/{name}`` against a real
DuckDB built with the production view SQL. ant-serve is a script with heavy
module-level imports, so the endpoint definitions are extracted with ``ast``
(same approach as tests/test_mcp_links.py).
"""
from __future__ import annotations

import ast
import asyncio
import copy
import logging
import os
import sys
from collections import OrderedDict
from contextlib import contextmanager
from pathlib import Path
from threading import Lock
from typing import Any, Dict, List, Optional, Set, Tuple

import duckdb
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
LIB_DIR = REPO_ROOT / 'lib'
if str(LIB_DIR) not in sys.path:
    sys.path.insert(0, str(LIB_DIR))

import facet_mapping  # noqa: E402
import obscol_tags  # noqa: E402
from anthive_db import build_obs_view_sql, safe_ident  # noqa: E402
from canonical_columns import CANONICAL_TAGS  # noqa: E402
from facet_aliases import canonical_facet, is_deprecated_facet_column  # noqa: E402
from facet_mapping import (  # noqa: E402
    candidate_columns, project_dataset, redact_hidden_sources, translate_observed,
)
from metadata import get_api_config, parse_md_file  # noqa: E402

SERVE_PATH = REPO_ROOT / 'bin' / 'api' / 'ant-serve'

API_CONFIG = {
    'obs_rename': {'Sex': 'ant_sex', 'diagnosis': 'ant_disease', 'Braak': 'ant_braak_raw_unused',
                   'individual': 'ant_donor'},
    'obs_hidden': ['Braak', 'secret_code'],
    'obs_value_rename': {'Sex': {'M': 'm', 'F': 'f'}, 'diagnosis': {'Control': 'control'}},
    'obs_value_hidden': {'Sex': ['unknown']},
    'obs_derive': {
        'ant_braak_group': {'source': 'braak_num', 'dtype': 'categorical',
                            'map': {0: 'low', 3: 'medium', 6: 'high'}, 'default': 'other'},
        'ant_apoe4_carrier': {'source': 'secret_code', 'dtype': 'categorical',
                             'map': {'E3/E4': 'E4+', 'E3/E3': 'E4-'}},
    },
    'obs_constant': {'ant_tissue': {'value': 'prefrontal cortex', 'dtype': 'categorical'}},
}


def _by_facet(entries: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    return {e['facet']: e for e in entries}


# ── lib/facet_mapping.py ────────────────────────────────────────────────────

def test_rename_with_value_rename_and_hidden_values():
    entries = _by_facet(project_dataset(API_CONFIG, ['ant_sex', 'ant_disease'], []))
    sex = entries['ant_sex']
    assert sex['mapping_kind'] == 'renamed'
    assert sex['source_columns'] == ['Sex']
    assert sex['sources'] == [{'column': 'Sex', 'published_as': 'ant_sex', 'hidden': False}]
    assert sex['live'] is True and sex['dtype'] == 'categorical'
    assert sex['unmapped'] == 'passthrough'
    assert sex['value_map'] == [
        {'from': 'unknown', 'to': None, 'rule': 'hidden'},
        {'from': 'M', 'to': 'm', 'rule': 'mapped'},
        {'from': 'F', 'to': 'f', 'rule': 'mapped'},
    ]


def test_hidden_value_wins_over_rename():
    config = {'obs_rename': {'Sex': 'ant_sex'},
              'obs_value_rename': {'Sex': {'unknown': 'u', 'M': 'm'}},
              'obs_value_hidden': {'Sex': ['unknown']}}
    sex = project_dataset(config, ['ant_sex'])[0]
    assert [r['from'] for r in sex['value_map']] == ['unknown', 'M']
    assert sex['value_map'][0]['rule'] == 'hidden'


def test_renamed_hidden_column_is_not_live():
    braak = _by_facet(project_dataset(API_CONFIG, [], []))['ant_braak_raw_unused']
    assert braak['source_hidden'] is True
    assert braak['live'] is False


def test_derive_with_default_and_translation_counts():
    entries = _by_facet(project_dataset(API_CONFIG, ['ant_braak_group'], []))
    group = entries['ant_braak_group']
    assert group['mapping_kind'] == 'derived'
    assert group['source_columns'] == ['braak_num']
    assert group['unmapped'] == 'default'
    assert group['default'] == 'other'
    assert [(r['from'], r['to']) for r in group['value_map']] == [(0, 'low'), (3, 'medium'), (6, 'high')]
    # A numerical source compares numerically: 3.0 matches the key 3.
    translate_observed(group, [(3.0, 50), (5.0, 7), (None, 4)], n_distinct=2)
    rows = {r['from']: r for r in group['value_map']}
    assert rows[None]['to'] == 'other' and rows[None]['rule'] == 'default'  # NULL takes the default
    assert rows[3]['to'] == 'medium' and rows[3]['rule'] == 'mapped' and rows[3]['n_cells'] == 50
    assert rows[5]['to'] == 'other' and rows[5]['rule'] == 'default' and rows[5]['n_cells'] == 7
    # configured but absent from the data: zero cells (the list is complete)
    assert rows[0]['n_cells'] == 0 and rows[6]['n_cells'] == 0
    assert group['value_map_truncated'] is False
    assert group['n_distinct_source_values'] == 2


def test_rename_translation_marks_passthrough_and_truncation():
    sex = _by_facet(project_dataset(API_CONFIG, ['ant_sex']))['ant_sex']
    translate_observed(sex, [('M', 10), ('X', 3), ('unknown', 2)], n_distinct=5)
    rows = {r['from']: r for r in sex['value_map']}
    assert rows['M']['to'] == 'm' and rows['M']['rule'] == 'mapped'
    assert rows['X']['to'] == 'X' and rows['X']['rule'] == 'passthrough'
    assert rows['unknown']['to'] is None and rows['unknown']['rule'] == 'hidden'
    # 'F' was not in the truncated top list: its count is unknown, not zero
    assert rows['F']['n_cells'] is None
    assert sex['value_map_truncated'] is True


def test_constant_and_native():
    entries = _by_facet(project_dataset(API_CONFIG, ['ant_tissue', 'ant_region'], ['ant_age']))
    tissue = entries['ant_tissue']
    assert tissue['mapping_kind'] == 'constant'
    assert tissue['constant_value'] == 'prefrontal cortex'
    assert tissue['source_columns'] == [] and tissue['value_map'] == []
    region = entries['ant_region']
    assert region['mapping_kind'] == 'native' and region['source_columns'] == ['ant_region']
    assert entries['ant_age']['dtype'] == 'numerical'


def test_non_facet_targets_are_ignored():
    config = {'obs_rename': {'Cell_type_full': 'celltype'},
              'obs_derive': {'astro_contaminated': {'source': 'x', 'dtype': 'categorical', 'map': {'a': 'yes'}}}}
    assert project_dataset(config, ['celltype', 'astro_contaminated']) == []


def test_redaction_of_hidden_source():
    apoe = _by_facet(project_dataset(API_CONFIG, ['ant_apoe4_carrier']))['ant_apoe4_carrier']
    assert apoe['source_hidden'] is True
    translate_observed(apoe, [('E3/E4', 5), ('E2/E4', 2), ('E3/E3', 8)], n_distinct=3)
    clean = redact_hidden_sources(apoe)
    assert clean['redacted'] is True
    assert clean['source_columns'] == [] and clean['sources'] == []
    assert all(row['from'] is None for row in clean['value_map'])
    assert 'secret_code' not in repr(clean)
    assert 'E2/E4' not in repr(clean) and 'E3/E3' not in repr(clean)
    by_to = {(r['to'], r['rule']): r['n_cells'] for r in clean['value_map']}
    assert by_to[('E4+', 'mapped')] == 5
    assert by_to[('E4-', 'mapped')] == 8
    assert by_to[(None, 'default')] == 2
    # the input entry is untouched (admins still get it)
    assert apoe['source_columns'] == ['secret_code']


def test_redaction_leaves_public_sources_alone():
    sex = _by_facet(project_dataset(API_CONFIG, ['ant_sex']))['ant_sex']
    assert redact_hidden_sources(sex) == sex


def test_candidate_columns_heuristics():
    columns = ['Sex_donor', 'Gender', 'passage', 'Age_at_death', 'agent', 'BraakStage',
               'individualID', 'brain_region', 'Diagnosis', 'ant_sex', 'ucell__x__sex',
               'StudyX::Exp::sex', 'Sussex']
    assert candidate_columns(columns, ['sex', 'gender']) == ['Sex_donor', 'Gender']
    assert candidate_columns(columns, ['age']) == ['Age_at_death']
    assert candidate_columns(columns, ['braak']) == ['BraakStage']
    # a hint, not a classifier: 'Sex_donor' names the donor too
    assert candidate_columns(columns, ['donor', 'individual']) == ['Sex_donor', 'individualID']
    assert candidate_columns(columns, ['region']) == ['brain_region']
    assert candidate_columns(columns, ['disease', 'diagnosis', 'dx']) == ['Diagnosis']
    assert candidate_columns(columns, []) == []


# ── definitions (lib/obscol_tags_vocab.yaml) ───────────────────────────────

def test_every_canonical_tag_has_a_facet_definition():
    for tag in CANONICAL_TAGS:
        definition = obscol_tags.facet_definition(f'ant_{tag}')
        assert definition['defined'], tag
        assert definition['summary'] and definition['missing'], tag
        assert definition['kind'] in ('categorical', 'numerical'), tag


def test_facet_definition_shape_and_status():
    sex = obscol_tags.facet_definition('ant_sex')
    assert sex['facet'] == 'ant_sex' and sex['tag'] == 'sex' and sex['label'] == 'Sex'
    assert 'm' in sex['values'] and 'sex' in sex['hint_keywords']
    unknown = obscol_tags.facet_definition('ant_lesion')
    assert unknown['defined'] is False and unknown['summary'] == ''
    status = obscol_tags.facet_definitions_status()
    assert status['state'] == 'under_audit'
    assert 'audit' in status['note']


def test_definitions_avoid_em_dashes():
    for tag in obscol_tags.known_tags():
        definition = obscol_tags.facet_definition(tag)
        for field in ('summary', 'definition', 'values', 'missing'):
            assert '—' not in definition[field], (tag, field)


# ── ant-serve endpoints (extracted with ast, real DuckDB) ──────────────────

_SERVE_WANTED = {
    '_require_admin', '_caller_is_admin', '_is_facet_column',
    '_facet_definition', '_facet_definitions_status',
    '_collect_facet_columns_per_dataset', '_facet_value_distribution', '_facet_dataset_stats',
    '_FACET_STATS_CACHE', '_FACET_STATS_CACHE_MAX', '_FACET_STATS_LOCK', '_plot_db_fingerprint',
    '_FACET_API_CONFIG_CACHE', '_FACET_PROJECTION_CACHE', '_FACET_PROJECTION_CACHE_MAX',
    '_FACET_PROJECTION_LOCK', '_FACET_VALUE_ROWS_LIMIT', '_FACET_MAPPING_KEYS',
    '_facet_api_config', '_quote_stored_column', '_facet_add_counts',
    '_dataset_facet_projection', '_facet_entry_for_caller', '_facet_non_carriers',
    'get_dataset_facets', 'get_facet_detail', '_secondary_db_ids', '_join_tables_signature',
}

DB_ID = 'G1/ds1'
OTHER_ID = 'G1/ds2'

MD_TEXT = """---
id: ds1
api:
  obs_rename:
    Sex: ant_sex
    individual: ant_donor
  obs_hidden:
  - secret_code
  obs_value_rename:
    Sex:
      M: m
      F: f
  obs_value_hidden:
    Sex:
    - unknown
  obs_derive:
    ant_braak_group:
      source: braak_num
      dtype: categorical
      map:
        0: low
        3: medium
        6: high
      default: other
    ant_apoe4_carrier:
      source: secret_code
      dtype: categorical
      map:
        E3/E4: E4+
        E3/E3: E4-
  obs_constant:
    ant_tissue:
      value: prefrontal cortex
      dtype: categorical
---
"""

OTHER_MD = """---
id: ds2
api:
  obs_rename:
    Gender: ant_sex
---
"""


def _build_duckdb(path: Path, api_config: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    """A small dataset: 6 cells, author columns, views built like ant-convert."""
    conn = duckdb.connect(str(path))
    conn.execute("CREATE TABLE __cells AS SELECT * FROM (VALUES "
                 "(0,'c0'),(1,'c1'),(2,'c2'),(3,'c3'),(4,'c4'),(5,'c5')) t(cell_id, cell_name)")
    conn.execute("CREATE TABLE __obscat AS SELECT * FROM (VALUES "
                 "(0,'M','d1','E3/E4'),(1,'M','d1','E3/E4'),(2,'F','d2','E3/E3'),"
                 "(3,'F','d2','E3/E3'),(4,'unknown','d3','E2/E4'),(5,'X','d3','E2/E4')) "
                 "t(cell_id, \"Sex\", individual, secret_code)")
    conn.execute("CREATE TABLE __obsnum AS SELECT * FROM (VALUES "
                 "(0,0.0),(1,3.0),(2,3.0),(3,5.0),(4,6.0),(5,NULL)) t(cell_id, braak_num)")
    conn.execute("ALTER TABLE __obsnum ALTER braak_num TYPE DOUBLE")
    for view, table, sibling in (('obscat', '__obscat', '__obsnum'), ('obsnum', '__obsnum', '__obscat')):
        cols = [r[0] for r in conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = ?", [table]).fetchall()]
        sib = [r[0] for r in conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = ?", [sibling]).fetchall()]
        conn.execute(build_obs_view_sql(view, table, cols, api_config,
                                        sibling_underscore_table=sibling, sibling_columns=sib))
    cat = [r[0] for r in conn.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name = 'obscat' "
        "AND column_name <> 'cell_name'").fetchall()]
    num = [r[0] for r in conn.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name = 'obsnum' "
        "AND column_name <> 'cell_name'").fetchall()]
    conn.close()
    return cat, num


class _FakeRegistry:
    def __init__(self, tmp_path: Path):
        self._db_md_bases: Dict[str, Path] = {}
        self.data_folder = tmp_path
        self.metadata_cache: Dict[str, Dict[str, Any]] = {}
        self.paths: Dict[str, Path] = {}
        self.databases: Dict[str, str] = {}
        md = tmp_path / 'ds1.md'
        md.write_text(MD_TEXT)
        api_config = get_api_config(parse_md_file(md)[0])
        cat, num = _build_duckdb(tmp_path / 'ds1.duckdb', api_config)
        self._db_md_bases[DB_ID] = md
        self.paths[DB_ID] = tmp_path / 'ds1.duckdb'
        self.databases[DB_ID] = str(self.paths[DB_ID])
        self.metadata_cache[DB_ID] = {
            'id': DB_ID, 'title': 'Dataset One', 'group_id': 'G1', 'file_id': 'ds1',
            'n_cells': 6, 'collection': {'group': 'Group One'},
            'obs': {'categorical': cat, 'numerical': num},
        }
        other_md = tmp_path / 'ds2.md'
        other_md.write_text(OTHER_MD)
        self._db_md_bases[OTHER_ID] = other_md
        self.paths[OTHER_ID] = tmp_path / 'ds2.duckdb'
        self.databases[OTHER_ID] = str(self.paths[OTHER_ID])
        # ds2 declares ant_sex, but its served view does not expose it.
        self.metadata_cache[OTHER_ID] = {
            'id': OTHER_ID, 'title': 'Dataset Two', 'group_id': 'G1', 'file_id': 'ds2',
            'n_cells': 10, 'collection': {'group': 'Group One'},
            'obs': {'categorical': ['Gender', 'Diagnosis', 'brain_region'], 'numerical': ['AgeDeath']},
        }

    def get_database_metadata(self, db_id: str) -> Dict[str, Any]:
        return self.metadata_cache[db_id]

    def get_database_path(self, db_id: str) -> str:
        if db_id not in self.paths:
            raise KeyError(db_id)
        return str(self.paths[db_id])

    def list_databases(self) -> List[Dict[str, Any]]:
        return list(self.metadata_cache.values())


class _PooledDb:
    def __init__(self, conn):
        self._conn = conn


@pytest.fixture
def serve(tmp_path):
    from fastapi import FastAPI, HTTPException, Query, Request
    from fastapi.testclient import TestClient

    registry = _FakeRegistry(tmp_path)
    registry.opened = []

    @contextmanager
    def _pooled_db(db_id: str):
        registry.opened.append(db_id)
        conn = duckdb.connect(registry.get_database_path(db_id), read_only=True)
        try:
            yield _PooledDb(conn)
        finally:
            conn.close()

    tree = ast.parse(SERVE_PATH.read_text())
    body = []
    for node in tree.body:
        name = getattr(node, 'name', None)
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            name = next((t.id for t in targets if isinstance(t, ast.Name)), None)
        if name in _SERVE_WANTED:
            body.append(node)
    found = {getattr(n, 'name', None) or next(
        (t.id for t in (n.targets if isinstance(n, ast.Assign) else [n.target]) if isinstance(t, ast.Name)), None)
        for n in body}
    assert found == _SERVE_WANTED, _SERVE_WANTED - found
    app = FastAPI()
    namespace: Dict[str, Any] = {
        'app': app, 'registry': registry, '_pooled_db': _pooled_db,
        'parse_md_file': parse_md_file, 'safe_ident': safe_ident, 'duckdb': duckdb,
        'log': logging.getLogger('test-facet-mapping'), 'OrderedDict': OrderedDict,
        '_plot_os': os, 'Lock': Lock, 'copy': copy, 'Path': Path, 'asyncio': asyncio,
        'Query': Query, 'Request': Request, 'HTTPException': HTTPException,
        'Dict': Dict, 'List': List, 'Any': Any, 'Optional': Optional, 'Tuple': Tuple, 'Set': Set,
        'get_auth_settings': lambda cfg=None: {'mode': 'authelia', 'admins': set()},
        '_load_module_variability': lambda force=False: None,
        'canonical_facet': canonical_facet, 'is_deprecated_facet_column': is_deprecated_facet_column,
        '_facets_visible': lambda: [],
        'facet_hidden_by_config': lambda facet, visible=None: bool(visible) and facet not in visible,
    }
    exec(compile(ast.Module(body=body, type_ignores=[]), str(SERVE_PATH), 'exec',
                 dont_inherit=True), namespace)
    client = TestClient(app)
    client.registry = registry
    return client


ADMIN = {'Remote-User': 'root', 'Remote-Groups': 'users,admins'}
USER = {'Remote-User': 'alice', 'Remote-Groups': 'users'}


def test_dataset_facets_endpoint_projection(serve):
    body = serve.get(f'/databases/{DB_ID}/facets', headers=USER).json()
    facets = {f['facet']: f for f in body['facets']}
    assert set(facets) == {'ant_sex', 'ant_donor', 'ant_braak_group', 'ant_apoe4_carrier', 'ant_tissue'}
    assert body['definitions_status']['state'] == 'under_audit'
    assert body['hidden_sources_visible'] is False

    sex = facets['ant_sex']
    assert sex['label'] == 'Sex' and sex['defined'] is True
    assert sex['mapping_kind'] == 'renamed' and sex['source_columns'] == ['Sex']
    rows = {r['from']: r for r in sex['value_map']}
    assert rows['M'] == {'from': 'M', 'to': 'm', 'rule': 'mapped', 'n_cells': 2}
    assert rows['F']['to'] == 'f' and rows['F']['n_cells'] == 2
    assert rows['unknown']['rule'] == 'hidden' and rows['unknown']['to'] is None
    assert rows['X']['rule'] == 'passthrough' and rows['X']['to'] == 'X'
    assert sex['n_cells'] == 6 and sex['n_missing'] == 1  # 'unknown' became NULL

    group = facets['ant_braak_group']
    assert group['mapping_kind'] == 'derived' and group['default'] == 'other'
    group_rows = {r['from']: r for r in group['value_map']}
    assert group_rows[3.0]['to'] == 'medium' and group_rows[3.0]['n_cells'] == 2
    assert group_rows[5.0]['rule'] == 'default' and group_rows[5.0]['to'] == 'other'
    assert group_rows[6.0]['n_cells'] == 1
    # A NULL Braak stage also falls through to the default, as in the view.
    assert group_rows[None] == {'from': None, 'to': 'other', 'rule': 'default', 'n_cells': 1}
    assert group['n_missing'] == 0

    tissue = facets['ant_tissue']
    assert tissue['mapping_kind'] == 'constant'
    assert tissue['constant_value'] == 'prefrontal cortex'
    assert tissue['n_cells'] == 6 and tissue['n_missing'] == 0


def test_dataset_facets_hide_obs_hidden_sources_from_non_admins(serve):
    text = serve.get(f'/databases/{DB_ID}/facets', headers=USER).text
    assert 'secret_code' not in text
    assert 'E2/E4' not in text and 'E3/E3' not in text
    body = serve.get(f'/databases/{DB_ID}/facets', headers=USER).json()
    apoe = {f['facet']: f for f in body['facets']}['ant_apoe4_carrier']
    assert apoe['redacted'] is True and apoe['source_hidden'] is True
    assert apoe['source_columns'] == []
    assert {(r['to'], r['n_cells']) for r in apoe['value_map']} == {('E4+', 2), ('E4-', 2), (None, 2)}


def test_dataset_facets_show_hidden_sources_to_admins(serve):
    body = serve.get(f'/databases/{DB_ID}/facets', headers=ADMIN).json()
    assert body['hidden_sources_visible'] is True
    apoe = {f['facet']: f for f in body['facets']}['ant_apoe4_carrier']
    assert apoe['redacted'] is False and apoe['source_columns'] == ['secret_code']
    assert {r['from'] for r in apoe['value_map']} == {'E3/E4', 'E3/E3', 'E2/E4'}


def test_dataset_facets_404(serve):
    assert serve.get('/databases/G1/nope/facets').status_code == 404


def test_facet_detail_carriers_and_non_carriers(serve):
    body = serve.get('/facets/ant_sex', headers=USER).json()
    assert body['definition']['label'] == 'Sex' and body['definition']['defined'] is True
    assert body['definitions_status']['state'] == 'under_audit'
    [carrier] = body['datasets']
    assert carrier['db_id'] == DB_ID
    assert carrier['mapping_kind'] == 'renamed'
    assert carrier['source_columns'] == ['Sex']
    assert {r['from']: r['to'] for r in carrier['value_map']}['M'] == 'm'
    [other] = body['non_carriers']
    assert other['db_id'] == OTHER_ID
    assert other['candidate_columns'] == ['Gender']
    assert other['declared'] is True and other['declared_kind'] == 'renamed'


def test_facet_detail_hint_keywords_for_other_facets(serve):
    body = serve.get('/facets/ant_braak_group', headers=USER).json()
    [other] = body['non_carriers']
    assert other['declared'] is False and other['candidate_columns'] == []
    body = serve.get('/facets/ant_donor', headers=USER).json()
    assert body['non_carriers'][0]['candidate_columns'] == []


def test_facet_detail_without_mapping(serve):
    body = serve.get('/facets/ant_sex?mapping=false').json()
    assert body['non_carriers'] is None
    assert 'mapping_kind' not in body['datasets'][0]


def test_facet_detail_value_distribution_is_cached_until_the_duckdb_changes(serve):
    first = serve.get('/facets/ant_sex?mapping=false').json()
    opened_after_first = len(serve.registry.opened)
    assert opened_after_first >= 1
    assert serve.get('/facets/ant_sex?mapping=false').json() == first
    assert len(serve.registry.opened) == opened_after_first  # warm: no database opened
    path = serve.registry.paths[DB_ID]
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000_000))
    assert serve.get('/facets/ant_sex?mapping=false').json() == first
    assert len(serve.registry.opened) > opened_after_first  # fingerprint changed: re-read
