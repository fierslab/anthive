"""lib/disease_rule.py: the Braak x CERAD 2x2 disease definition."""
from __future__ import annotations

import pandas as pd
import pytest

import disease_rule as rule


@pytest.mark.parametrize('braak, cerad, expected', [
    (3, 'probable', 'AD'), (6, 'definite', 'AD'), (4.0, 'definite', 'AD'),
    (0, 'none', 'control'), (2, 'sparse', 'control'), (1, 'none', 'control'),
    (3, 'none', None), (5, 'sparse', None),                     # PART-like: tangles without plaques
    (2, 'probable', None), (0, 'definite', None),               # amyloid-only
    (None, 'definite', None), (4, None, None), (float('nan'), 'none', None), ('x', 'none', None),
])
def test_classify_boundaries(braak, cerad, expected):
    assert rule.classify(braak, cerad) == expected


def _spec(**overrides):
    spec = {'rule': 'braak_cerad', 'column': 'disease_braak_cerad',
            'braak': {'column': 'Braak', 'map': {'Braak 0': 0, 'Braak III': 3, 'Reference': None}},
            'cerad': {'column': 'CERAD score', 'map': {'Absent': 'none', 'Moderate': 'probable',
                                                       'Reference': None}}}
    spec.update(overrides)
    return spec


def test_classify_frame_applies_maps_and_numeric_columns():
    spec = rule.validate_spec(_spec())
    braak = pd.Series(['Braak 0', 'Braak III', 'Braak III', 'Reference', None])
    cerad = pd.Series(['Absent', 'Moderate', 'Absent', 'Reference', 'Moderate'])
    assert rule.classify_frame(braak, cerad, spec).tolist() == ['control', 'AD', None, None, None]
    numeric = rule.validate_spec({'rule': 'braak_cerad', 'column': 'c', 'braak': {'column': 'Braak'},
                                  'cerad': {'column': 'CERAD', 'map': {1: 'definite', 2: 'probable',
                                                                       3: 'sparse', 4: 'none'}}})
    labels = rule.classify_frame(pd.Series([3.0, 5.0, 1.0, 2.0]), pd.Series([1.0, 2.0, 4.0, 3.0]), numeric)
    assert labels.tolist() == ['AD', 'AD', 'control', 'control']       # float keys match integer map keys


@pytest.mark.parametrize('bad', [
    None, {}, {'rule': 'other'}, _spec(column=''), _spec(braak={}), _spec(cerad={'column': 'x', 'map': {'a': 'high'}}),
    _spec(braak={'column': 'B', 'map': {'x': 'three'}}),
])
def test_validate_spec_rejects(bad):
    with pytest.raises(rule.DiseaseRuleError):
        rule.validate_spec(bad)


def test_module_imports_without_pandas():
    """ant-md-check imports disease_rule through facet_lint and has no pandas in its script deps."""
    import subprocess
    import sys
    from pathlib import Path

    lib_dir = Path(__file__).resolve().parent.parent / 'lib'
    code = ("import sys; sys.modules['pandas'] = None; sys.path.insert(0, %r); "
            "import disease_rule, facet_lint; print('ok')" % str(lib_dir))
    result = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'ok'
