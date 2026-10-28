"""One evidence-bound answer call; unknown outcomes never trigger a new turn."""
from datetime import datetime, timedelta, timezone
import hashlib
import json

from .contracts import ContractError
from .daily_store import Deferred
from .daily_subscription import require_subscription
from .daily_transport import durable_file
from .f05 import file_sha
from .pilot_text import MODEL, MAX_BYTES
from .registry import encoded
from .runtime import no_symlinks
from .qa_query import citation
from .qa_time import describe
from . import qa_weekly
from .qa_answer_contract import INSTRUCTION, validate



def request(plan):
    if any(not t['qa_usable'] for h in plan['hits'] for t in h.get('table_context', [])):
        raise ContractError('结构、表头待核查或过长的表格只提供原表定位，本次不生成数值回答。')
    if sum(len(h['text']) for h in plan['hits']) > 24000:
        raise ContractError('检索片段超过本次回答容量，请缩小问题范围。')
    keys = ('evidence_id', 'material_id', 'title', 'text', 'is_paper', 'publication_year', 'completeness', 'content_nature')
    evidence = [{k: h[k] for k in keys} | {k: h.get(k) for k in ('work_title', 'doi', 'source_sha256', 'document_role', 'table_context')} for h in plan['hits']]
    packet = dict(protocol='P43-qa-v1', instruction=INSTRUCTION,
                question=plan['query']['question'], intent=plan['query']['mode'],
                prior_question=plan['query'].get('prior_question', ''),
                time_filter={key:plan['query'].get(key) for key in ('time_scope','year_from','year_to')},
                selected_materials=[dict(material_id=key, title=next(h['title'] for h in plan['hits'] if h['material_id']==key))
                                    for key in plan['query'].get('selected', [])], evidence=evidence)
    if plan['query']['mode']=='weekly_answer':
        snapshot=plan['query']['weekly']
        start,end=qa_weekly.weekly_window(snapshot['question'],snapshot['at'])
        packet['weekly_scope']=dict(basis='completed_ingestion',
            start_inclusive=start.isoformat(),end_exclusive=end.isoformat(),
            visible_source_count=len(snapshot['sources']),
            sources=[dict(material_id=s['material_id'],ingested_at=s['ingested_at'])
                     for s in snapshot['sources'] if s['material_id'] in {h['material_id'] for h in plan['hits']}])
    return packet


def render_plan_answer(output, plan):
    query,hits=plan['query'],plan['hits']
    text=render_answer(output,hits,query.get('selected',[]) if query['mode']=='compare' else ())
    if query['mode']=='weekly_answer':
        snapshot=query['weekly']
        text=snapshot['text']+'\n\n'+qa_weekly.scope(snapshot,hits)+'\n\n'+text
    return describe(text,query)




def render_evidence(output, hits):
    validate(output, hits)
    by_id = {h['evidence_id']: h for h in hits}
    cited = []
    materials = list(dict.fromkeys(h['material_id'] for h in hits))
    lines = ['依据当前检索原文整理：']
    for claim in output['claims']:
        numbers = []
        for e in claim['evidence']:
            if e['evidence_id'] not in cited:
                cited.append(e['evidence_id'])
            number = str(materials.index(by_id[e['evidence_id']]['material_id'])+1)
            if number not in numbers:
                numbers.append(number)
        lines.append(claim['text']+' ['+','.join(numbers)+']')
        lines.extend('原文：“'+e['quote']+'”' for e in claim['evidence'])
    if output['gaps']:
        lines.append('尚未解决：'+'；'.join(output['gaps']))
    lines.extend(citation(by_id[key], materials.index(by_id[key]['material_id'])+1) for key in cited)
    lines.append('引文已逐字校验；概括由 AI 生成，判断适用性时请核对原文上下文。')
    if any(h.get('table_context') for h in hits):
        lines.append('表格引文只与提取文本核对；数字、单位和表头尚未与 PDF 原页逐项核验。')
    return '\n\n'.join(lines)


def comparison_coverage(output, hits, selected=()):
    validate(output, hits)
    by_id = {h['evidence_id']: h for h in hits}
    cited = {by_id[e['evidence_id']]['material_id'] for c in output['claims'] for e in c['evidence']}
    missing = [key for key in selected if key not in cited]
    if not missing:
        return output
    names = [next(h['title'] for h in hits if h['material_id']==key) for key in missing]
    gap = '尚未完成所选材料的比较；以下材料没有被回答直接引用：'+'、'.join(names)
    # Coverage is deterministic. It does not claim that quotation matching
    # proves the semantic correctness of a comparison.
    return dict(output, status='partial' if output['claims'] else 'not_found',
                gaps=[gap[:300], *output['gaps'][:5]])


def render_answer(output, hits, selected=()):
    output = comparison_coverage(output, hits, selected)
    by_id = {h['evidence_id']: h for h in hits}
    materials = list(dict.fromkeys(h['material_id'] for h in hits))
    lines = ['依据当前检索原文整理（简版）：']
    if output['gaps']:
        lines.append('范围提示：'+output['gaps'][0])
    shown = []
    remaining = set(selected)
    while remaining:
        choices = [(len(remaining & {by_id[e['evidence_id']]['material_id'] for e in claim['evidence']}), i)
                   for i, claim in enumerate(output['claims']) if i not in shown]
        score, i = max(choices, key=lambda pair: (pair[0], -pair[1]), default=(0, 0))
        if score == 0:
            break
        shown.append(i)
        remaining -= {by_id[e['evidence_id']]['material_id'] for e in output['claims'][i]['evidence']}
    for i in range(len(output['claims'])):
        if len(shown) >= 2:
            break
        if i not in shown:
            shown.append(i)
    used = {}
    for i in sorted(shown):
        claim = output['claims'][i]
        numbers = list(dict.fromkeys(str(materials.index(by_id[e['evidence_id']]['material_id'])+1) for e in claim['evidence']))
        lines.append(claim['text']+' ['+','.join(numbers)+']')
        quote = claim['evidence'][0]['quote']
        lines.append('原文：“'+quote[:80]+'”'+('（节录）' if len(quote)>80 else ''))
        for e in claim['evidence']:
            hit=by_id[e['evidence_id']]
            used.setdefault(hit['material_id'],hit)
    for key, hit in used.items():
        title=hit['title'][:100]+('…' if len(hit['title'])>100 else '')
        pages=' · PDF 物理页 '+','.join(map(str,hit['physical_pages'])) if hit.get('physical_pages') else ''
        lines.append('['+str(materials.index(key)+1)+'] '+title+pages)
    lines.append('完整主张、引文及定位可点击“查看原文依据”。概括由 AI 生成，引文已逐字核对。')
    if any(h.get('table_context') for h in hits):
        lines.append('表格数值尚未对照 PDF 原页核验，请通过“查看原文依据”核对表名、物理页码和单位。')
    return '\n\n'.join(lines)


class QAModel:
    def __init__(self, authority, profile, store, sources, wire):
        self.authority, self.profile, self.store, self.sources, self.wire = authority, profile, store, sources, wire

    def saved(self, job):
        call = self.store.call(job['id'])
        if call is None or call['state']!='validated':
            return None
        receipt = no_symlinks(self.authority.root/'qa-jobs'/job['id']/'response.local.json')
        if file_sha(receipt) != call['result']['response_sha256']:
            raise ContractError('已保存的问答模型回执变化。')
        return comparison_coverage(call['result']['output'], job['result']['hits'], job['plan']['query'].get('selected', []) if job['plan']['query']['mode']=='compare' else ())

    def run(self, job, plan):
        raw = encoded(request(plan)); digest = hashlib.sha256(raw.encode()).hexdigest()
        root = no_symlinks(self.authority.root/'qa-jobs'/job['id'])
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        durable_file(root/'request.local.json', json.loads(raw))
        receipt, approval_path = root/'response.local.json', root/'approval.local.json'
        def check():
            if 'weekly' in plan['query']:
                qa_weekly.verify(plan['query']['weekly'],self.sources)
            return dict(self.authority.check(), **self.sources.verify(plan['hits']),
                        **{str(root/'request.local.json'): digest})
        check()
        existing = self.store.call(job['id'])
        if existing:
            if existing['request_sha256'] != digest:
                raise ContractError('问题或检索依据已变化，不能复用旧模型结果。')
            if existing['state']=='validated':
                if file_sha(receipt) != existing['result']['response_sha256']:
                    raise ContractError('已保存的问答模型回执变化。')
                return validate(existing['result']['output'], plan['hits'])
            if existing['state']=='rejected' or not receipt.exists():
                raise Deferred('qa_model_unknown_or_rejected_no_resubmit')
        if existing is None and self.profile.generation is not None:
            require_subscription(self.authority, self.store.daily)
        if not approval_path.exists():
            g = self.profile.generation
            if g is None:
                raise Deferred('qa_model_approval_required')
            start = datetime.fromtimestamp(self.store.daily.now() if self.store.daily.policy.standing_daily else job['created'], timezone.utc)
            durable_file(approval_path, dict(reference='P43-qa-model-v1', approved=True, model=MODEL,
                effort='medium', billing='chatgpt_subscription_only', max_business_turns=1,
                automatic_business_retries=0, request_sha256=[digest], daily_rules_sha256=self.authority.digest,
                account_sha256=g['account_sha256'], minimum_remaining_percent=g['minimum_remaining_percent'],
                starts_at=start.isoformat(), expires_at=(start+timedelta(hours=2)).isoformat()))
        approval = json.loads(approval_path.read_bytes()); approval_sha = file_sha(approval_path)
        def access():
            evidence = check()
            now = datetime.fromtimestamp(self.store.daily.now(), timezone.utc)
            if (file_sha(approval_path) != approval_sha or approval['daily_rules_sha256'] != self.authority.digest
                    or not datetime.fromisoformat(approval['starts_at']) <= now < datetime.fromisoformat(approval['expires_at'])):
                raise Deferred('qa_model_grant_expired')
            return evidence
        if existing is None:
            frozen = access(); self.store.reserve(job['id'], digest, access)
            binding = dict(approvalPath=str(approval_path), approvalSha=approval_sha,
                startsAt=approval['starts_at'], expiresAt=approval['expires_at'], consentExpiresAt=approval['expires_at'],
                accountSha=approval['account_sha256'], requestSha=digest, files=frozen)
            packet = dict(model=MODEL, effort='medium', waitMs=self.profile.generation['wait_seconds']*1000,
                          request=raw, requestId='qa-'+digest[:40], binding=binding)
            try:
                envelope = self.wire.exchange(packet, access)
            except (OSError, ContractError) as error:
                self.store.call_result(job['id'], 'unknown', dict(reason=type(error).__name__))
                raise Deferred('qa_model_unknown_no_resubmit') from error
            if len(encoded(envelope).encode()) > MAX_BYTES:
                self.store.call_result(job['id'], 'rejected', dict(reason='oversized_response'))
                raise Deferred('qa_model_rejected')
            durable_file(receipt, envelope)
        else:
            if receipt.stat().st_size > MAX_BYTES:
                raise ContractError('问答回执超出大小限制。')
            envelope = json.loads(receipt.read_bytes())
        check()
        if envelope.get('status') != 'received':
            self.store.call_result(job['id'], 'unknown', dict(reason='model_not_received', response_sha256=file_sha(receipt)))
            raise Deferred('qa_model_unknown_no_resubmit')
        try:
            output = validate(json.loads(envelope['text']), plan['hits'])
        except (ValueError, KeyError, TypeError, ContractError) as error:
            self.store.call_result(job['id'], 'rejected', dict(reason=type(error).__name__, response_sha256=file_sha(receipt)))
            raise Deferred('qa_model_rejected') from error
        self.store.call_result(job['id'], 'validated', dict(output=output, response_sha256=file_sha(receipt)))
        return output
