"""Pure regression planning from reviewed source manifests and explicit clocks."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .qa_quality import digest, require, sha, text, verify_fixture

ZONE = ZoneInfo('Asia/Shanghai')


def snapshot(value):
    require(isinstance(value, dict) and set(value) == {'corpus_id', 'sources'}
        and text(value['corpus_id']) and isinstance(value['sources'], list), 'explicit corpus manifest required')
    ids = set()
    sources = []
    for source in value['sources']:
        require(isinstance(source, dict) and set(source) == {'id', 'canonical_id', 'body_sha256', 'metadata_sha256', 'critical'}
            and text(source['id']) and source['id'] not in ids and text(source['canonical_id'])
            and sha(source['body_sha256']) and sha(source['metadata_sha256']) and type(source['critical']) is bool,
            'reviewed independent source identity and hashes required')
        ids.add(source['id']); sources.append(dict(source))
    return dict(corpus_id=value['corpus_id'], sources=sorted(sources, key=lambda s: s['id']))


def changes(previous, current):
    current = snapshot(current)
    if previous is None:
        return dict(initial=True, major=True, changed_ids=[s['id'] for s in current['sources']],
            changed_families=len({s['canonical_id'] for s in current['sources']}), reason='initial_baseline')
    previous = snapshot(previous)
    require(previous['corpus_id'] == current['corpus_id'], 'different libraries require separate baselines')
    old, new = ({s['id']: s for s in m['sources']} for m in (previous, current))
    changed = sorted(k for k in set(old) | set(new) if old.get(k) != new.get(k))
    families = {s['canonical_id'] for k in changed for s in (old.get(k), new.get(k)) if s}
    denominator = max(1, len({s['canonical_id'] for s in previous['sources']}))
    critical = any(s['critical'] for k in changed for s in (old.get(k), new.get(k)) if s)
    major = bool(changed) and (critical or len(families) >= 20 or len(families)/denominator >= .05)
    return dict(initial=False, major=major, changed_ids=changed, changed_families=len(families),
        changed_fraction=len(families)/denominator, critical_changed=critical,
        reason='critical_or_large_update' if major else 'minor_update' if changed else 'unchanged')


def periods(now):
    require(isinstance(now, datetime) and now.tzinfo is not None and now.utcoffset() is not None,
        'timezone-aware evaluation clock required')
    local = now.astimezone(ZONE)
    week = local.replace(hour=20, minute=0, second=0, microsecond=0)-timedelta(days=(local.weekday()-6) % 7)
    if week > local:
        week -= timedelta(days=7)
    first = local.replace(day=1, hour=20, minute=0, second=0, microsecond=0)
    month = first+timedelta(days=(6-first.weekday()) % 7)
    if month > local:
        first = (first-timedelta(days=1)).replace(day=1)
        month = first+timedelta(days=(6-first.weekday()) % 7)
    return dict(weekly=week.isoformat(), monthly=month.isoformat())


def _sample(cases, count, seed):
    """Spread a bounded sample across source/question families before filling."""
    ordered = sorted(cases, key=lambda c: digest([seed, c['id']]))
    groups = {}
    for case in ordered:
        groups.setdefault(case.get('parent_group', case['id']), []).append(case['id'])
    chosen = []
    while groups and len(chosen) < count:
        for key in list(groups):
            chosen.append(groups[key].pop(0))
            if not groups[key]:
                del groups[key]
            if len(chosen) >= count:
                break
    return chosen


def plan_regression(previous, current, cases, *, now, last_completed=None,
                    enabled=False, system_fingerprint, core_ids=(), include_heldout=False):
    require(type(enabled) is bool and type(include_heldout) is bool and sha(system_fingerprint), 'explicit regression settings required')
    require(isinstance(cases, list) and cases, 'test cases required')
    by_id = {}
    for case in cases:
        verify_fixture(case)
        require(case['id'] not in by_id, 'duplicate regression case')
        by_id[case['id']] = case
    require(isinstance(core_ids, (list, tuple)) and len(core_ids) == len(set(core_ids))
        and all(k in by_id for k in core_ids), 'explicit known core cases required')
    eligible = {k: v for k, v in by_id.items() if include_heldout or v.get('split') != 'heldout'}
    require(eligible and all(k in eligible for k in core_ids), 'core cases cannot silently expose heldout data')
    current = snapshot(current)
    require(all('corpus_id' not in c or c['corpus_id'] == current['corpus_id'] for c in cases),
        'regression cases belong to a different library')
    old_hash = digest(snapshot(previous)) if previous else None
    new_hash = digest(current)
    delta = changes(previous, current)
    due = periods(now)
    last = {} if last_completed is None else dict(last_completed)
    require(set(last) <= {'weekly', 'monthly', 'update_snapshot', 'system_fingerprint'}, 'unknown regression watermark')
    for field in ('weekly', 'monthly'):
        if field in last:
            try:
                saved = datetime.fromisoformat(last[field])
            except (TypeError, ValueError) as error:
                raise ValueError('invalid completed period') from error
            require(saved.tzinfo is not None and saved <= now, 'completed period is in the future or lacks a zone')
    reasons = []
    if last.get('weekly') != due['weekly']:
        reasons.append('weekly')
    if last.get('monthly') != due['monthly']:
        reasons.append('monthly')
    update_due = old_hash != new_hash and last.get('update_snapshot') != new_hash
    if update_due:
        reasons.append(delta['reason'])
    system_changed = last.get('system_fingerprint') != system_fingerprint
    if system_changed:
        reasons.append('system_changed')
    known_sources = {s['id'] for s in current['sources']}
    affected = sorted(k for k, c in eligible.items() if set(c['evidence_bindings']) & set(delta['changed_ids']))
    unknown_dependencies = any(set(c['evidence_bindings'])-known_sources-set(delta['changed_ids']) for c in eligible.values())
    full = system_changed or 'weekly' in reasons or (update_due and (delta['major'] or unknown_dependencies))
    offline_ids = sorted(eligible) if full else sorted(set(affected if update_due else ()) | set(core_ids)) if reasons else []
    if update_due and not affected and delta['changed_ids']:
        # New independent evidence can change old answers without a prior dependency edge.
        offline_ids = sorted(eligible)
        full = True
    semantic = set()
    if 'monthly' in reasons:
        fixed = list(core_ids)[:10]
        semantic.update(fixed)
        semantic.update(_sample([c for k, c in eligible.items() if k not in semantic], 20-len(fixed), due['monthly']))
    if system_changed or update_due:
        semantic.update(offline_ids if full else affected)
        semantic.update(core_ids)
    # Changed source hashes invalidate old frozen-context expectations until reviewed.
    by_source = {s['id']: s for s in current['sources']}
    stale = sorted(k for k, c in eligible.items() if any(i not in by_source or by_source[i]['body_sha256'] != h
        or (previous is not None and i in delta['changed_ids'])
        for i, h in c['evidence_bindings'].items()))
    content = dict(version='qa-regression-plan-v1', corpus_id=current['corpus_id'],
        previous_snapshot_sha256=old_hash, snapshot_sha256=new_hash, system_fingerprint=system_fingerprint,
        case_set_sha256=digest(sorted((k, c['fixture_sha256']) for k, c in eligible.items())),
        periods=due, reasons=reasons, changes=delta, offline_case_ids=offline_ids,
        semantic_case_ids=sorted(semantic), stale_fixture_ids=stale, unknown_dependencies=unknown_dependencies,
        max_semantic_calls=2*len(semantic), automatic_model_execution=False,
        include_heldout=include_heldout,
        offline_completion_watermarks=dict(weekly=due['weekly'], update_snapshot=new_hash, system_fingerprint=system_fingerprint),
        semantic_completion_watermarks=dict(monthly=due['monthly']))
    content['task_key'] = digest(content)
    return dict(content, enabled=enabled, state='disabled' if not enabled else 'ready' if reasons else 'not_due',
        new_model_calls=0, service_start=False, schedules_installed=False)
