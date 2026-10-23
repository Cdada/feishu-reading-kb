"""Optional one-case model orchestration. Disabled unless adapters and grant exist."""
from .contracts import ContractError
from .daily_store import Deferred
from .qa_answer_contract import INSTRUCTION, validate
from .qa_quality import (answer_packet, evidence_hits, judge_packet, bind_judgment,
    digest, require, validate_grade, verify_fixture)
from .qa_quality_report import make_report


def run_case(fixture, run, *, calls=None, grant=None, checked_access=None,
             answer_adapter=None, judge_adapter=None, instruction=INSTRUCTION):
    verify_fixture(fixture)
    make_report([fixture], [], run)  # Validate immutable run identity before any reservation.
    require(run['prompt_sha256'] == digest(instruction), 'run prompt hash differs from actual instruction')
    result = dict(id=fixture['id'], fixture_sha256=fixture['fixture_sha256'], model_calls=0, cached=False)
    if any(v is None for v in (calls, grant, checked_access, answer_adapter, judge_adapter)):
        return dict(result, state='skipped', reason='evaluation_execution_not_configured')
    try:
        response = calls.execute(run_id=run['run_id'], case_id=fixture['id'], purpose='answer',
            model_fingerprint=digest(dict(model=run['model'], prompt_sha256=run['prompt_sha256'], parameters_sha256=run['parameters_sha256'])),
            request=answer_packet(fixture, instruction=instruction), grant=grant, checked_access=checked_access,
            adapter=answer_adapter, validator=lambda a: validate(a, evidence_hits(fixture)))
        result['model_calls'] += response['new_model_calls']
        result['cached'] = response['cached']
        result['answer_cached'] = response['cached']
        result['usage'] = dict(answer=response['usage'])
        answer = response['output']
        judged = calls.execute(run_id=run['run_id'], case_id=fixture['id'], purpose='judge',
            model_fingerprint=run['judge_fingerprint'], request=judge_packet(fixture, answer),
            grant=grant, checked_access=checked_access, adapter=judge_adapter,
            validator=lambda g: validate_grade(g, fixture, answer))
        result['model_calls'] += judged['new_model_calls']
        result.update(state='completed', answer=answer,
            judgment=bind_judgment(fixture, answer, judged['output'], judge_fingerprint=run['judge_fingerprint']),
            cached=response['cached'] or judged['cached'], answer_cached=response['cached'], judge_cached=judged['cached'],
            usage=dict(answer=response['usage'], judge=judged['usage']),
            provider_models=dict(answer=response['provider_model'], judge=judged['provider_model']))
    except Deferred as error:
        result['model_calls'] += getattr(error, 'new_model_calls', 0)
        result.update(state='unknown' if 'unknown' in error.reason or 'unresolved' in error.reason else 'skipped', reason=error.reason)
    except ContractError as error:
        result['model_calls'] += getattr(error, 'new_model_calls', 0)
        result.update(state='error', reason=str(error))
    return result


def blind_pair(fixture, left, right, *, seed):
    """Return anonymous judging data plus a separate mapping; reverse order to audit bias."""
    require(isinstance(seed, str) and seed, 'explicit pairwise seed required')
    base = judge_packet(fixture, left)
    judge_packet(fixture, right)
    swapped = int(digest(dict(seed=seed, id=fixture['id']))[-1], 16) % 2 == 1
    candidates = dict(A=right if swapped else left, B=left if swapped else right)
    base.pop('candidate')
    base['protocol'] = 'qa-quality-pair-v1'
    base['candidates'] = candidates
    base['response_contract'] = dict(winner='A|B|tie|uncertain', reason='brief evidence-based reason')
    base['instruction'] += '\n比较A与B对问题的回答质量，忽略篇幅和顺序；不能判断时返回uncertain。'
    return dict(judge_input=base, private_mapping=dict(A='right' if swapped else 'left', B='left' if swapped else 'right'))
