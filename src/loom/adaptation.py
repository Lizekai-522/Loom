"""Topology adaptation (FR-019), sampling consistency (FR-027), graph repair
primitives (FR-032) and risk-based review sampling with confidence bounds (FR-054).

All functions are deterministic; callers persist decisions through the audited
services. Nothing here calls a model or widens authority.
"""
from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import math

from pydantic import ValidationError

from .store import Conflict, InvalidInput, canonical
from .workflow import WorkflowDefinition

TOPOLOGIES = ('standard', 'direct')
DIRECT_MIN_EXAMPLES = 3


# ----- FR-019 ---------------------------------------------------------------
def choose_topology(prompt, examples, requested='auto'):
    """`direct` skips analyst/tester when the user already supplied enough
    acceptance examples; otherwise the full requirements→tests→build pipeline."""
    if requested not in ('auto',) + TOPOLOGIES:
        raise InvalidInput('topology must be auto, standard or direct')
    count = len(examples or [])
    if requested == 'direct' and count < DIRECT_MIN_EXAMPLES:
        raise InvalidInput(f'direct topology needs at least {DIRECT_MIN_EXAMPLES} acceptance examples')
    if requested != 'auto':
        return requested, 'requested by the user'
    if count >= DIRECT_MIN_EXAMPLES:
        return 'direct', f'{count} user acceptance examples already specify the behaviour'
    return 'standard', 'requirements and tests must be derived from the description'


def topology_report(rows):
    """Compare outcomes per topology. rows: dicts with topology, state, cost, model_calls."""
    report = {}
    for row in rows:
        item = report.setdefault(row['topology'], dict(topology=row['topology'], sessions=0, delivered=0, verified=0,
                                                       needs_attention=0, running=0, cost_microusd=0, model_calls=0))
        item['sessions'] += 1
        item['cost_microusd'] += row.get('cost', 0)
        item['model_calls'] += row.get('model_calls', 0)
        if row['state'] in ('delivered', 'verified'):
            item['delivered'] += 1
        if row['state'] == 'verified':
            item['verified'] += 1
        if row['state'] == 'needs_attention':
            item['needs_attention'] += 1
        if row['state'] in ('running', 'paused'):
            item['running'] += 1
    for item in report.values():
        finished = item['delivered'] + item['needs_attention']
        item['delivery_rate'] = round(item['delivered'] / finished, 4) if finished else None
        item['avg_cost_microusd'] = round(item['cost_microusd'] / item['sessions']) if item['sessions'] else None
        item['avg_model_calls'] = round(item['model_calls'] / item['sessions'], 2) if item['sessions'] else None
    return sorted(report.values(), key=lambda r: r['topology'])


# ----- FR-027 ---------------------------------------------------------------
def agreement(samples):
    """Fraction of samples agreeing with the most common canonical answer."""
    if not samples:
        raise InvalidInput('at least one sample is required')
    counts = {}
    for sample in samples:
        key = canonical(sample)
        counts[key] = counts.get(key, 0) + 1
    top = max(counts.values())
    return dict(samples=len(samples), distinct=len(counts), agreement=round(top / len(samples), 4))


def case_conflicts(model_cases, user_examples):
    """Same input, different expected output between the tester model and the
    user: the model misunderstood the requirement, so confidence is low."""
    expected = {canonical(e['input']): canonical(e['expected']) for e in user_examples or []}
    return [case['name'] for case in model_cases
            if canonical(case['input']) in expected and canonical(case['expected']) != expected[canonical(case['input'])]]


# ----- FR-032 ---------------------------------------------------------------
def repair_plan(definition, operation):
    """Rewire / Bypass / InsertPrereq on a plan definition; returns a validated copy."""
    if not isinstance(operation, dict) or operation.get('op') not in {'rewire', 'bypass', 'insert_prereq'}:
        raise InvalidInput('op must be rewire, bypass or insert_prereq')
    plan = deepcopy(definition)
    nodes = {n['id']: n for n in plan['nodes']}
    target = operation.get('node_id')
    if target not in nodes:
        raise InvalidInput('node_id is not in the plan')
    if operation['op'] == 'rewire':
        depends = operation.get('depends')
        if not isinstance(depends, list):
            raise InvalidInput('rewire needs a depends list')
        nodes[target]['depends'] = depends
    elif operation['op'] == 'bypass':
        if any(item['source_node'] == target for n in plan['nodes'] for item in n.get('input_artifacts') or []):
            raise Conflict('cannot bypass a node whose output another node reads; rewire that input first')
        if len(plan['nodes']) == 1:
            raise Conflict('cannot bypass the only node')
        inherited = nodes[target].get('depends', [])
        plan['nodes'] = [n for n in plan['nodes'] if n['id'] != target]
        for node in plan['nodes']:
            if target in node.get('depends', []):
                node['depends'] = [d for d in node['depends'] if d != target] + [d for d in inherited if d not in node['depends']]
    else:
        new = operation.get('node')
        if not isinstance(new, dict) or new.get('id') in nodes:
            raise InvalidInput('insert_prereq needs a new node with a unique id')
        plan['nodes'].insert([n['id'] for n in plan['nodes']].index(target), new)
        nodes[target]['depends'] = list(dict.fromkeys(nodes[target].get('depends', []) + [new['id']]))
    try:
        return WorkflowDefinition.model_validate(plan).model_dump(mode='json')
    except (ValidationError, TypeError, ValueError) as error:
        raise InvalidInput('repaired plan is invalid: ' + str(error).splitlines()[0]) from None


# ----- FR-054 ---------------------------------------------------------------
def sampled(subject_id, rate, risk='normal'):
    """Deterministic risk-based sampling: high risk is always reviewed."""
    if type(rate) not in (int, float) or not 0 <= rate <= 1:
        raise InvalidInput('sampling rate must be between 0 and 1')
    if risk == 'high':
        return True
    bucket = int(sha256(str(subject_id).encode()).hexdigest()[:8], 16) / 0xFFFFFFFF
    return bucket < rate


def deviation_bounds(reviewed, deviations, confidence=0.95):
    """Observed deviation rate with a one-sided upper bound.

    Zero deviations: exact 1-(1-c)^(1/n). Otherwise Wilson score upper bound.
    """
    if type(reviewed) is not int or type(deviations) is not int or not 0 <= deviations <= reviewed:
        raise InvalidInput('invalid sample counts')
    if reviewed == 0:
        return dict(reviewed=0, deviations=0, rate=None, upper_bound=None, samples_needed_for_2pct=149)
    rate = deviations / reviewed
    if deviations == 0:
        upper = 1 - (1 - confidence) ** (1 / reviewed)
    else:
        z = 1.6448536269514722  # one-sided 95%
        centre = rate + z * z / (2 * reviewed)
        margin = z * math.sqrt(rate * (1 - rate) / reviewed + z * z / (4 * reviewed * reviewed))
        upper = min(1.0, (centre + margin) / (1 + z * z / reviewed))
    needed = math.ceil(math.log(1 - confidence) / math.log(0.98))
    return dict(reviewed=reviewed, deviations=deviations, rate=round(rate, 4), upper_bound=round(upper, 4),
                confidence=confidence, samples_needed_for_2pct=needed,
                meets_2pct_bound=upper <= 0.02)
