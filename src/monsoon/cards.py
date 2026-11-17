"""Six-state worker cards for the conversation router.

The stored task status stays the assistant ledger's own vocabulary. This
projection is what the conversation lane and the workbench read: a bounded
summary, never the raw worker log.
"""
from __future__ import annotations

WORKER_STATES = ('progress', 'waiting', 'ready', 'blocked', 'failed', 'dropped')
CARD_LIMIT = 600
# Local policy stops are failures of the task, not pauses the person retries.
FAILED_BLOCKS = frozenset({'CONTENT_POLICY_BLOCKED', 'REPORT_POLICY_BLOCKED'})


def project_card(task: dict) -> dict:
    status = task.get('status') or ''
    state = task.get('state') if isinstance(task.get('state'), dict) else {}
    blocked = state.get('blocked')
    if status in ('queued', 'running'):
        worker = 'progress'
    elif status in ('waiting_approval', 'awaiting_confirmation'):
        worker = 'waiting'
    elif status == 'completed':
        worker = 'ready'
    elif status == 'cancelled':
        worker = 'dropped'
    elif status == 'blocked' and blocked in FAILED_BLOCKS:
        worker = 'failed'
    else:
        worker = 'blocked'
    if status == 'waiting_approval':
        human = 'approve'
    elif status == 'awaiting_confirmation':
        human = 'confirm'
    elif status == 'blocked' and worker == 'blocked':
        human = 'review'
    else:
        human = None
    summary = f"{task.get('kind') or 'task'} · {task.get('title') or ''} · {status}"
    if blocked:
        summary += ' · ' + str(blocked)[:80]
    return dict(
        worker_state=worker,
        human_action=human,
        question_for_trigger_id=state.get('requested_in') or state.get('approval_id'),
        card_summary=summary[:CARD_LIMIT],
    )
