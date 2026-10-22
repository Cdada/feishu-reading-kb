"""Offline retrieval evaluation against separately labelled, caller-owned cases.

This measures retrieval and clarification, never semantic answer correctness.
No network, model invocation, library writes, or automatic ground-truth labels.
"""
from .contracts import ContractError
from .qa_query import parse


def evaluate(index, cases, *, now):
    if not isinstance(cases,list) or not 1<=len(cases)<=1000:
        raise ContractError('评测需要 1–1000 条独立标注的问题。')
    results=[];seen=set();expected_total=recalled=positive=positive_hits=negative=negative_correct=0
    for case in cases:
        if (not isinstance(case,dict) or not {'id','question','expected_mode','expected_materials'}<=set(case)
                or not isinstance(case['id'],str) or not case['id'] or case['id'] in seen
                or not isinstance(case['expected_materials'],list)
                or any(not isinstance(key,str) or not key for key in case['expected_materials'])):
            raise ContractError('评测问题标识或预期来源无效。')
        seen.add(case['id'])
        query=parse(case['question'],case.get('context'),now=now)
        mode=query['mode'];hits=[]
        if mode in ('search','source','answer','tools','compare','followup','version'):
            hits=index.search(query.get('query',query['question']),material_ids=query['selected'],
                year_from=query.get('year_from'),year_to=query.get('year_to'),time_scope=query.get('time_scope'))
            if mode=='source':
                import re
                quote=re.search(r'[“「"](.+)[”」"]',query['question'])
                if quote:hits=[h for h in hits if quote[1] in h['text']]
        elif mode!='clarify':
            raise ContractError('本评测只测检索和时间澄清；周报、原件与模型回答另行验证。')
        ids=list(dict.fromkeys(h['material_id'] for h in hits))[:5]
        expected=set(case['expected_materials'])
        matched=len(expected & set(ids))
        passed=mode==case['expected_mode'] and (expected<=set(ids) if expected else not ids)
        results.append(dict(id=case['id'],passed=passed,mode=mode,retrieved=ids,expected=sorted(expected)))
        if case['expected_mode']!='clarify':
            if expected:
                expected_total+=len(expected);recalled+=matched;positive+=1;positive_hits+=int(bool(matched))
            else:
                negative+=1;negative_correct+=int(not ids)
    return dict(cases=len(results),passed=sum(r['passed'] for r in results),
        positive_cases=positive,positive_hit_at_5=positive_hits/positive if positive else None,
        expected_source_recall_at_5=recalled/expected_total if expected_total else None,
        negative_cases=negative,negative_correct=negative_correct,
        clarification_cases=sum(c['expected_mode']=='clarify' for c in cases),results=results,
        semantic_answer_accuracy=None,model_calls=0,
        limitation='仅衡量给定标注集的检索与澄清；不能证明综合回答的语义准确率或全库覆盖率。')
