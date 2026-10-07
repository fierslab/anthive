"""Donor-level Braak / APOE backfill from paper supplementary tables (decision L31, 2026-10-07).

What it does: reads a donor table (supplement of the paper), joins it to the per-cell donor id of one
dataset duckdb, and writes obs columns with AnnotationWriter (one column per writer block, backup of the
duckdb taken first). Prints the join report (donors matched / unmatched, value counts), never donor ids
of controlled-access cohorts.

Usage: backfill_donor_supp.py <dataset> [--dry-run]
Datasets: olah (O20; ROSMAP clinical table joined through the projid of Olah Supplementary Data 1, sheet RUSH; the 3 epilepsy donors have no source), gerrits (G21), grubman (G19; adds individual_supp because the deposit donor_id is a library pair)
Inputs: supplementary tables under $ANT_SUPP_DIR (default ./supp_tables; not stored in git). See doc/curation/L31_donor_supp_backfill.md.
Outputs: obs columns braak_supp (DOUBLE), apoe_supp, braak_note_supp (VARCHAR) in the duckdb __obsnum / __obscat.
Run with envs/anthive-data/bin/python.
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Dict, Optional

import duckdb
import openpyxl
import pandas as pd

REPO = Path('/data/1/users/mark/anthive4')
sys.path.insert(0, str(REPO / 'lib'))
from anthive_db import AnnotationWriter  # noqa: E402
from config import get_config, get_api_url  # noqa: E402

STORE = Path('/data/1/project/h5adstore')
SUPP = Path(os.environ.get('ANT_SUPP_DIR', './supp_tables'))  # downloaded supplementary tables; never committed
TODAY = '2026-10-07'
ROMAN = {'0': 0, 'I': 1, 'II': 2, 'III': 3, 'IV': 4, 'V': 5, 'VI': 6}


def apoe_from_digits(raw) -> Optional[str]:
    """'34' -> 'E3/E4' (lower allele first); NA / blank -> None."""
    if raw is None:
        return None
    text = str(raw).strip()
    if text.endswith('.0'):
        text = text[:-2]
    if len(text) != 2 or not text.isdigit() or any(ch not in '234' for ch in text):
        return None
    low, high = sorted(text)
    return f'E{low}/E{high}'


def apoe_from_slash(raw) -> Optional[str]:
    """'E3/E4' -> 'E3/E4' with lower allele first; NA -> None."""
    if raw is None or str(raw).strip() in ('', 'NA', 'nan'):
        return None
    alleles = [a.strip() for a in str(raw).upper().split('/')]
    if len(alleles) != 2 or any(a not in ('E2', 'E3', 'E4') for a in alleles):
        return None
    alleles.sort()
    return '/'.join(alleles)


def braak_integer(raw) -> Optional[float]:
    """Single Roman numeral stage -> integer; ranges, ABC-score strings, blanks -> None."""
    if raw is None:
        return None
    text = str(raw).strip()
    return float(ROMAN[text]) if text in ROMAN else None


def gerrits_table() -> pd.DataFrame:
    workbook = openpyxl.load_workbook(SUPP / 'gerrits' / '401_2021_2263_MOESM2_ESM.xlsx', read_only=True)
    rows = list(workbook['Table S1'].iter_rows(values_only=True))
    header = list(rows[2])
    frame = pd.DataFrame([r for r in rows[3:] if r[0] is not None], columns=header)
    # two regions per donor: values must agree across regions
    for col in ('apoe_genotype', 'Braak or ABC score'):
        assert frame.groupby('donor #')[col].nunique(dropna=False).max() == 1, col
    per_donor = frame.drop_duplicates('donor #').set_index('donor #')
    out = pd.DataFrame(index=per_donor.index.map(lambda n: f'Donor{int(n)}'))
    out['braak_supp'] = [braak_integer(v) for v in per_donor['Braak or ABC score']]
    out['apoe_supp'] = [apoe_from_digits(v) for v in per_donor['apoe_genotype']]
    out['braak_note_supp'] = [str(v) for v in per_donor['Braak or ABC score']]
    return out


def grubman_table() -> pd.DataFrame:
    workbook = openpyxl.load_workbook(SUPP / 'grub_3.xlsx', read_only=True)
    rows = list(workbook['Supplementary Table 1'].iter_rows(values_only=True))
    frame = pd.DataFrame([r[:7] for r in rows[2:]], columns=list(rows[1][:7]))
    records: Dict[str, dict] = {}
    for _, row in frame.iterrows():
        pair, donor_slot = row['Case#'].split(' donor ')
        first, second = pair.split('_')
        individual = first if donor_slot == '1' else second
        records[individual] = {
            'braak_supp': braak_integer(row['Braak']),
            'apoe_supp': apoe_from_slash(row['ApoE']),
            'braak_note_supp': str(row['Braak']) if row['Braak'] is not None else None,
        }
    return pd.DataFrame.from_dict(records, orient='index')


GRUBMAN_LIBRARY_OF_PAIR = {
    'AD1_AD2': 'AD_15042_04076', 'AD3_AD4': 'AD_15064_08005', 'AD5_AD6': 'AD_09241_09006',
    'Ct1_Ct2': 'ct_08026_07143', 'Ct3_Ct4': 'ct_07635_06972', 'Ct5_Ct6': 'ct_03481_06849',
}


def grubman_cell_columns(cells: pd.DataFrame, table: pd.DataFrame) -> Dict[str, pd.Series]:
    """Per-cell individual (barcode + library join to the authors' cell table), then Table 1 values.

    The deposit donor_id is the first member of the two-individual library pair (AD1 stands for the pair
    AD1_AD2). The authors' per-cell table (Nature Neuroscience 2019, source data of Fig. 1b-e) names the
    individual of every cell; cells the authors left undetermined (AD-un, Ct-un) get no individual and
    take a pair value only when both members of the pair agree.
    """
    workbook = openpyxl.load_workbook(SUPP / 'grub_4.xlsx', read_only=True)
    rows = [r[:5] for r in workbook['panels b,c,d,e'].iter_rows(min_row=2, values_only=True) if r[0]]
    sheet = pd.DataFrame(rows, columns=['sampleID', 'umap1', 'umap2', 'condition', 'individual'])
    sheet['barcode'] = sheet['sampleID'].str.split('_').str[0]
    sheet['library'] = sheet['sampleID'].str.split('_', n=1).str[1]
    cells = cells.copy()
    cells['barcode'] = cells.index.str.split('_').str[0]
    cells['pair'] = cells.index.str.split('_', n=1).str[1]
    cells['library'] = cells['pair'].map(GRUBMAN_LIBRARY_OF_PAIR)
    assert cells['library'].notna().all()
    joined = cells.reset_index().merge(sheet[['barcode', 'library', 'individual']], on=['barcode', 'library'], how='left')
    assert len(joined) == len(cells) and joined['individual'].notna().all(), 'cell join incomplete'
    joined['individual_known'] = joined['individual'].where(~joined['individual'].str.endswith('-un'))
    out: Dict[str, pd.Series] = {}
    out['individual_supp'] = pd.Series(joined['individual_known'].values, index=cells.index)
    for name in ('braak_supp', 'apoe_supp', 'braak_note_supp'):
        by_individual = joined['individual_known'].map(table[name])
        pair_members = {pair: pair.split('_') for pair in GRUBMAN_LIBRARY_OF_PAIR}
        pair_value = {}
        for pair, members in pair_members.items():
            values = {table.loc[m, name] for m in members}
            values = {v for v in values if v is not None and not (isinstance(v, float) and pd.isna(v))}
            pair_value[pair] = next(iter(values)) if len(values) == 1 and all(
                table.loc[m, name] is not None and not (isinstance(table.loc[m, name], float) and pd.isna(table.loc[m, name]))
                for m in members) else None
        fallback = joined['pair'].map(pair_value)
        merged = by_individual.where(joined['individual_known'].notna(), fallback)
        out[name] = pd.Series(merged.values, index=cells.index)
    return out


def olah_table() -> pd.DataFrame:
    """Braak (braaksc) and APOE (apoe_genotype) of the 14 ROSMAP donors; tidy table made from Supplementary
    Data 1 (RUSH sheet: sample -> projid) and ROSMAP_clinical.csv (Rush, in _synapse_raw). Contains no donor ids."""
    frame = pd.read_csv(SUPP / 'olah_donor_table.tsv', sep='\t', dtype=str)
    frame['donor'] = frame['sample'] + '_GM'
    frame = frame.set_index('donor')
    out = pd.DataFrame(index=frame.index)
    out['braak_supp'] = [braak_integer(v) if v in ROMAN else float(v) for v in frame['braaksc']]
    out['apoe_supp'] = [apoe_from_digits(v) for v in frame['apoe_genotype']]
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('dataset', choices=['gerrits', 'grubman', 'olah'])
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()

    if args.dataset == 'gerrits':
        collection, stem, join_col = 'Gerrits2021', 'gerrits2021_ad_brain', 'donor_id'
        table = gerrits_table()
    elif args.dataset == 'olah':
        collection, stem, join_col = 'Olah2020', 'olah2020_microglia', 'donor_id'
        table = olah_table()
    else:
        collection, stem, join_col = 'Grubman2019', 'Grubman2019_entorhinal_AD', 'donor_id'
        table = grubman_table()
    duckdb_path = STORE / collection / f'{stem}.duckdb'
    print(table.to_string() if args.dataset != "olah" else table.drop(columns=[]).to_string())

    con = duckdb.connect(str(duckdb_path), read_only=True)
    cells = con.execute(
        f'SELECT c.cell_name, o."{join_col}" AS donor FROM __cells c JOIN __obscat o USING (cell_id)'
    ).fetchdf().set_index('cell_name')
    con.close()
    donors_in_obs = sorted(cells['donor'].dropna().unique())
    matched = [d for d in donors_in_obs if d in table.index]
    print(f'obs donors {len(donors_in_obs)}, matched {len(matched)}, unmatched {sorted(set(donors_in_obs) - set(matched))}')
    print(f'table rows not in obs: {sorted(set(table.index) - set(donors_in_obs))}')
    if args.dataset != 'olah':
        assert len(matched) == len(donors_in_obs), 'unmatched donors'

    if args.dataset == 'grubman':
        columns = grubman_cell_columns(cells, table)
        print('join: all', len(cells), 'cells matched to an individual; unassigned (-un) cells:',
              int(columns['individual_supp'].isna().sum()))
        print(pd.crosstab(cells['donor'], columns['individual_supp'].fillna('undetermined')).to_string())
    else:
        columns = {name: cells['donor'].map(table[name]) for name in table.columns}
    for name, series in columns.items():
        print(name, 'cells with value', int(series.notna().sum()), 'of', len(series),
              'donors with value', int(cells['donor'][series.notna()].nunique()))
    if args.dry_run:
        return 0

    backup_dir = STORE / collection / f'_backup_{TODAY}'
    backup_dir.mkdir(exist_ok=True)
    backup_path = backup_dir / duckdb_path.name
    if not backup_path.exists():
        shutil.copy2(duckdb_path, backup_path)
        print('backup', backup_path)

    run_id = f'l31-{args.dataset}-{int(time.time())}'
    config = get_config()
    api_url = f'{get_api_url(config)}/api/'
    index_meta = (STORE / collection / 'index.md').read_text()
    data_meta = (STORE / collection / f'{stem}.md').read_text()
    group_id = [l.split(':', 1)[1].strip() for l in index_meta.splitlines() if l.startswith('group_id:')][0]
    file_id = [l.split(':', 1)[1].strip() for l in data_meta.splitlines() if l.startswith('id:')][0]
    db_id = f'{group_id}/{file_id}'
    print('db_id', db_id, 'run_id', run_id)
    for name, series in columns.items():
        with AnnotationWriter(duckdb_path, annotator='curation-L31', run_id=run_id,
                              catalog='donor_supp', db_id=db_id, ant_serve_url=api_url) as writer:
            if name == 'braak_supp':
                writer.add_numerical(name, series.astype('float64'), on_conflict='fail')
            else:
                writer.add_categorical(name, series.astype('object'), on_conflict='fail')
            writer.commit()
        print('written', name)
    return 0


if __name__ == '__main__':
    sys.exit(main())
