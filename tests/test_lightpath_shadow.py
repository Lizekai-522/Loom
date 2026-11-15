from dataclasses import FrozenInstanceError

import pytest

from loom.lightpath_shadow import assess


def valid():
    return dict(trusted_policy=True, frozen_suite=True, complete_spec=True,
                side_effect_free=True, semantic_review_required=False,
                budget_ok=True, unknown_state=False)


def test_candidate_is_only_advisory_and_immutable():
    result = assess(**valid())
    assert (result.recommendation, result.enforcement) == ('L1_candidate', False)
    with pytest.raises(FrozenInstanceError):
        result.enforcement = True


@pytest.mark.parametrize('key,value', [
    ('trusted_policy', False), ('frozen_suite', False), ('complete_spec', False),
    ('side_effect_free', False), ('semantic_review_required', True),
    ('budget_ok', False), ('unknown_state', True), ('budget_ok', None),
    ('trusted_policy', 1),
])
def test_ambiguous_or_risky_inputs_fail_closed(key, value):
    values = valid()
    values[key] = value
    result = assess(**values)
    assert result.recommendation == 'L2'
    assert result.enforcement is False
