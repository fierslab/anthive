"""
Build a clean WGCNA modules h5ad from prs.modules_city.WGCNA.h5ad,
inheriting all obs / obsm from the Village.6M reference so the result is
fully browsable on its own.

Output:
    data/h5adstore/Sierksma2025/Village.6M/Village.6M.WGCNA_modules.h5ad

Provenance:
    X (235106 x 13)  ← prs.modules_city.WGCNA.h5ad/X  (Seurat WGCNA module
                       eigengenes, MEblack … MEyellow)
    obs              ← Village.6M.h5ad/obs (full 206-col mirror)
    obsm             ← Village.6M.h5ad/obsm (X_umap)
    var              ← prs.modules_city.WGCNA.h5ad/var  (13 ME color names)
                       + a `description` column with the biological labels
                       used by ant-score downstream
"""
from __future__ import annotations

import sys
from pathlib import Path

import anndata as ad
import numpy as np
import pandas as pd

ROOT = Path('/data/1/users/mark/anthive4/data/h5adstore/Sierksma2025/Village.6M')
SRC_MOD = ROOT / 'prs.modules_city.WGCNA.h5ad'
SRC_REF = ROOT / 'Village.6M.h5ad'
OUT     = ROOT / 'Village.6M.WGCNA_modules.h5ad'

# Biological annotation for each WGCNA module — sourced from the
# Sierksma 2025 ant-score panel naming we already use elsewhere.
ME_DESCRIPTIONS = {
    'MEblack':        'Mitochondrial',
    'MEblue':         'Innate immune response',
    'MEbrown':        'Lysosome',
    'MEgreen':        'ER / immune',
    'MEgreenyellow':  'Cell migration',
    'MEmagenta':      'Antigen presentation',
    'MEpink':         'Morphology / immune',
    'MEpurple':       'Cell division',
    'MEred':          'Immune response',
    'MEsalmon':       'Y-linked',
    'MEtan':          'Cytokine activation',
    'MEturquoise':    'Translation',
    'MEyellow':       'Cytokine',
}


def main() -> int:
    print(f'Loading X + var from {SRC_MOD.name} ...')
    src_modules = ad.read_h5ad(SRC_MOD)
    print(f'  shape={src_modules.shape}')
    assert src_modules.shape[1] == 13, f'expected 13 modules, got {src_modules.shape[1]}'

    print(f'Loading obs + obsm from {SRC_REF.name} ...')
    # Read backed='r' would be ideal for the giant Village.6M but anndata
    # backed mode can be finicky with downstream copying; load fully.
    src_ref = ad.read_h5ad(SRC_REF)
    print(f'  ref shape={src_ref.shape}  obs cols={src_ref.obs.shape[1]}')

    # Cell-order alignment check
    assert src_modules.n_obs == src_ref.n_obs, \
        f'cell count mismatch: modules={src_modules.n_obs} ref={src_ref.n_obs}'
    if not np.array_equal(src_modules.obs_names.values, src_ref.obs_names.values):
        print('  cell-name ORDER differs — reindexing src_ref to modules order')
        ref_aligned = src_ref[src_modules.obs_names].copy()
    else:
        print('  cell-name order matches ✓')
        ref_aligned = src_ref

    # Build new AnnData
    print('Assembling new AnnData ...')
    new_var = src_modules.var.copy()
    # Add description column for each module — used as a friendly label
    new_var['description'] = [ME_DESCRIPTIONS.get(g, '') for g in new_var.index]
    new_obs = ref_aligned.obs.copy()
    new_obsm = {k: ref_aligned.obsm[k].copy() for k in ref_aligned.obsm.keys()}

    new = ad.AnnData(
        X=src_modules.X.astype(np.float32),
        obs=new_obs,
        var=new_var,
        obsm=new_obsm,
    )
    new.uns['module_scores'] = {
        'method': 'Seurat WGCNA on Sierksma 2025 city PRS cohort',
        'source_h5ad': 'prs.modules_city.WGCNA.h5ad',
        'reference_obs': 'Village.6M.h5ad',
        'n_modules': 13,
        'feature_naming': 'Seurat ME<color> eigengenes',
    }

    print(f'Writing {OUT.name} ...')
    new.write_h5ad(OUT, compression='gzip')
    print(f'  shape={new.shape}  obs cols={new.obs.shape[1]}  obsm={list(new.obsm.keys())}')
    print(f'Done. {OUT.stat().st_size / 1024 / 1024:.1f} MB')
    return 0


if __name__ == '__main__':
    sys.exit(main())
