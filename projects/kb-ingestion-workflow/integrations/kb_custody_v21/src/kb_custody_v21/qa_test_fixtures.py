"""Freeze provided-context QA tests without mixing references into model input.

This module does not retrieve, call a model or judge semantic correctness.
Answerability describes the supplied context, never the entire source corpus.
"""
from copy import deepcopy
import hashlib
import json

from .contracts import ContractError
from .qa_test_design import ROLES

ANSWERABILITY = frozenset({'supported', 'partial', 'missing_in_fixture',
    'needs_clarification', 'unsupported_modality_or_computation'})


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':')).encode()).hexdigest()


def compile_fixture(case, sources):
    """Validate explicit source spans and separate inputs from private rubrics.

    Sources are already-loaded text records keyed by material id, containing
    title, role and text. No path is read or inferred from a title. Callers must
    supply only authorized source records, including for permission tests.
    """
    required = {'id', 'question', 'selectors', 'constraints', 'conversation', 'expected'}
    if not isinstance(case, dict) or set(case) != required or not isinstance(sources, dict):
        raise ContractError('fixture requires an explicit case and source map')
    if any(not _text(case[k]) for k in ('id', 'question')):
        raise ContractError('fixture id and question required')
    selectors, constraints, conversation, expected = (case[k] for k in
        ('selectors', 'constraints', 'conversation', 'expected'))
    if (not isinstance(selectors, list) or len(selectors) > 64
            or not isinstance(constraints, list) or not constraints
            or any(not _text(v) for v in constraints)
            or not isinstance(conversation, list) or len(conversation) > 20):
        raise ContractError('explicit context, constraints and bounded conversation required')
    for turn in conversation:
        if (not isinstance(turn, dict) or set(turn) != {'role', 'content'}
                or turn['role'] not in ('user', 'assistant') or not _text(turn['content'])):
            raise ContractError('invalid conversation turn')
    if (not isinstance(expected, dict) or set(expected) !=
            {'answerability', 'required_claims', 'forbidden_claims'}
            or not isinstance(expected['answerability'], str)
            or expected['answerability'] not in ANSWERABILITY
            or any(not isinstance(expected[k], list) or not expected[k]
                or any(not _text(v) for v in expected[k])
                for k in ('required_claims', 'forbidden_claims'))):
        raise ContractError('reviewed expectations and boundaries required')
    if not selectors and expected['answerability'] == 'supported':
        raise ContractError('supported source question requires evidence')
    context, bindings, seen = [], {}, set()
    for selector in selectors:
        if not isinstance(selector, dict) or set(selector) != {'material_id', 'body_sha256', 'start', 'end'}:
            raise ContractError('explicit source span and digest required')
        material_id, start, end = (selector[k] for k in ('material_id', 'start', 'end'))
        if (not _text(material_id) or material_id not in sources
                or type(start) is not int or type(end) is not int):
            raise ContractError('missing authorized source or invalid offsets')
        source = sources[material_id]
        if (not isinstance(source, dict) or not _text(source.get('title'))
                or not isinstance(source.get('role'), str) or source['role'] not in ROLES
                or not _text(source.get('text'))):
            raise ContractError('source title, role and text required')
        body = source['text']
        digest = hashlib.sha256(body.encode()).hexdigest()
        if selector['body_sha256'] != digest:
            raise ContractError('source changed; review fixture again')
        if not 0 <= start < end <= len(body) or (material_id, start, end) in seen:
            raise ContractError('invalid or duplicate source span')
        seen.add((material_id, start, end))
        bindings[material_id] = digest
        context.append(dict(material_id=material_id, title=source['title'],
            source_role=source['role'], start=start, end=end, text=body[start:end]))
    model_input = dict(evaluation_scope='provided_context', question=case['question'],
        constraints=deepcopy(constraints), conversation=deepcopy(conversation), documents=context)
    reference = deepcopy(expected)
    # No scores, expected claims or other unknown caller fields enter model_input.
    return dict(id=case['id'], model_input=model_input, expected=reference,
        evidence_bindings=bindings, context_characters=sum(len(x['text']) for x in context),
        input_sha256=_digest(model_input), reference_sha256=_digest(reference),
        fixture_sha256=_digest(dict(input=model_input, expected=reference, bindings=bindings)),
        corpus_answerability='not_assessed', semantic_quality_tested=False, model_calls=0)
