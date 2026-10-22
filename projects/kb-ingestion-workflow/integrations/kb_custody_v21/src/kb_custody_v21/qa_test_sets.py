"""Keep related QA cases together and make changed expectations reviewable."""
from collections import Counter
from copy import deepcopy
import hashlib
import json
import re

from .contracts import ContractError

VARIANT_KINDS = frozenset({'paraphrase','time_scope','source_scope','insufficient_evidence',
    'source_authority','follow_up','untrusted_instruction','capability_boundary'})


def _text(value):
    return isinstance(value,str) and bool(value.strip())


def _validate(cases):
    if not isinstance(cases,list) or not 1<=len(cases)<=2000:
        raise ContractError('test sets require 1–2000 explicit cases')
    ids=set()
    for c in cases:
        if (not isinstance(c,dict) or any(not _text(c.get(k)) for k in ('id','parent_group','corpus_id'))
                or c['id'] in ids or not isinstance(c.get('evidence_bindings'),dict)
                or any(not _text(k) or not isinstance(v,str) or not re.fullmatch('[0-9a-f]{64}',v)
                       for k,v in c['evidence_bindings'].items())
                or type(c.get('seen_in_development',False)) is not bool
                or not isinstance(c.get('source_lineage',[]),list)
                or any(not _text(v) for v in c.get('source_lineage',[]))):
            raise ContractError('invalid case identity, evidence digest or exposure')
        ids.add(c['id'])


def make_variant(base, *, id, kind, question, expected_behavior, changed_dimensions):
    """Create a candidate; even a claimed paraphrase must be reviewed again."""
    _validate([base])
    if (not _text(id) or id==base['id'] or not isinstance(kind,str) or kind not in VARIANT_KINDS
            or not _text(question) or not _text(expected_behavior)
            or not isinstance(changed_dimensions,list) or not changed_dimensions
            or any(not _text(d) for d in changed_dimensions)):
        raise ContractError('explicit variant identity and expected behavior required')
    # Whitelist inherited fields: never accidentally copy gold claims, scores,
    # context or a completed answer into a question whose scope has changed.
    result={k:deepcopy(base[k]) for k in ('corpus_id','parent_group','evidence_bindings')}
    result['source_lineage']=deepcopy(base.get('source_lineage',[]))
    result.update(id=id,variant_of=base['id'],variant_kind=kind,question=question,
        proposed_expected_behavior=expected_behavior,changed_dimensions=list(changed_dimensions),
        source_question_type=base.get('question_type',base.get('type')),
        evidence_binding_status='candidate_requires_recheck',gold_status='unreviewed',
        answerability=None,split=None,seen_in_development=base.get('seen_in_development',False))
    return result


def partition_cases(cases, *, seed, proportions=(60,20,20)):
    """Group families AND identical source text before dev/regression/heldout.

    This is an initial split proposal, not a substitute for reviewing near-copy
    sources or tracking which heldout cases were later used for development.
    """
    _validate(cases)
    if (not _text(seed) or not isinstance(proportions,(tuple,list)) or len(proportions)!=3
            or any(type(n) is not int or n<0 for n in proportions) or sum(proportions)!=100
            or proportions[0]==0):
        raise ContractError('explicit split seed and valid percentages required')
    by_id={c['id']:c for c in cases}; parent={key:key for key in by_id}
    def find(key):
        while parent[key]!=key:
            parent[key]=parent[parent[key]];key=parent[key]
        return key
    def union(a,b):
        a,b=find(a),find(b)
        if a!=b:parent[max(a,b)]=min(a,b)
    owners={}
    for c in sorted(cases,key=lambda x:x['id']):
        links=[('family',c['corpus_id'],c['parent_group'])]
        links.extend(('source_text',digest) for digest in c['evidence_bindings'].values())
        links.extend(('source_lineage',c['corpus_id'],root) for root in c.get('source_lineage',[]))
        for link in links:
            if link in owners:union(c['id'],owners[link])
            else:owners[link]=c['id']
    groups={}
    for key in by_id:groups.setdefault(find(key),[]).append(key)
    partitions=('development','regression','heldout')
    counts=Counter({p:0 for p in partitions}); assignments={}; report=[]
    targets={p:len(cases)*n/100 for p,n in zip(partitions,proportions)}
    def stable(keys):return hashlib.sha256((seed+'\0'+'\0'.join(sorted(keys))).encode()).hexdigest()
    ordered=sorted(groups.values(),key=lambda ks:(not any(by_id[k].get('seen_in_development',False) for k in ks),-len(ks),stable(ks)))
    for keys in ordered:
        exposed=any(by_id[k].get('seen_in_development',False) for k in keys)
        destination='development' if exposed else max((p for p,n in zip(partitions,proportions) if n),key=lambda p:targets[p]-counts[p])
        counts[destination]+=len(keys)
        assignments.update({k:destination for k in keys})
        report.append(dict(group_id=stable(keys)[:20],case_ids=sorted(keys),split=destination,pinned_by_development_exposure=exposed))
    no_evidence=sorted(c['id'] for c in cases if not c['evidence_bindings'])
    unreviewed=sorted(c['id'] for c in cases if c.get('gold_status')!='reviewed')
    return dict(assignments=dict(sorted(assignments.items())),groups=report,counts=dict(counts),
        requested_percentages=dict(zip(partitions,proportions)),actual_percentages={p:100*counts[p]/len(cases) for p in partitions},
        missing_evidence_cases=no_evidence,not_gold_ready=unreviewed,
        evaluated_cases=0,model_calls=0,
        dataset_sha256=hashlib.sha256(json.dumps(cases,ensure_ascii=False,sort_keys=True).encode()).hexdigest(),
        limitation='同题家族、完全相同文字和显式提供的同源关系不跨分区；未登记的近重复及派生关系仍需复核。比例是目标，不能为凑比例拆组；本工具不核定金标准或回答质量。')
