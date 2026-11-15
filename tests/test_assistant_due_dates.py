import json

import pytest

from loom.store import InvalidInput, Store
from monsoon.runtime import ACTION_MARK, parse_actions
from monsoon.store import AssistantStore


@pytest.mark.parametrize('due', ['2026-02-29', '2026-04-31', '0000-01-01', '2026-13-01'])
def test_invalid_model_due_is_not_saved_as_a_due_date(due):
    action = {'type': 'todo', 'text': '准备资料', 'due': due}
    assert parse_actions(ACTION_MARK + json.dumps([action]))[0]['due'] is None


def test_leap_day_survives_action_parsing():
    action = {'type': 'todo', 'text': '准备资料', 'due': '2028-02-29'}
    assert parse_actions(ACTION_MARK + json.dumps([action]))[0]['due'] == '2028-02-29'


def test_storage_rejects_impossible_date_before_writing(tmp_path):
    data = AssistantStore(Store(tmp_path / 'test.db'))
    with pytest.raises(InvalidInput, match='valid date'):
        data.add_todo('tenant', 'nonexistent', body='test', source='user', actor='user', due='2026-02-31')
