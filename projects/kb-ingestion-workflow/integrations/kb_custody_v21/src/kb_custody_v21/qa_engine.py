"""Run a queued question in the single heavy lane; no network in retrieval."""
import re

from .contracts import ContractError
from .daily_store import Deferred
from .qa_time import choose, describe, prompt as time_prompt
from .qa_model import comparison_coverage, render_answer, render_plan_answer
from . import qa_weekly
from .qa_query import HELP, parse, render_search, render_weekly


class QAEngine:
    def __init__(self, store, sources, index, model):
        self.store, self.sources, self.index, self.model = store, sources, index, model

    def run(self, job):
        context = self.store.context(job['conversation'])
        plan = job['plan']
        if plan is None:
            original_parent=job['proof'].get('qa_original_parent')
            if original_parent:
                parent=self.store.get(original_parent)
                if (not parent or parent['state']!='sent' or parent['conversation']!=job['conversation']
                        or self.store.daily.now()-parent['created']>6*3600):
                    raise ContractError('原件选项已过期，请重新查文献。')
                hits=parent['result']['hits']
                self.sources.verify(hits)
                plan=dict(query=dict(mode='original',question='取阅原件',selected=[]),hits=hits)
                self.store.prepare(job['id'],plan)
                self.store.complete(job['id'],dict(status='original',text='准备原件入口。',hits=hits))
                return
            choice = job['proof'].get('qa_time_choice')
            if choice:
                parent = self.store.get(choice['job_id'])
                if (not parent or parent['state']!='sent' or not parent['plan']
                        or self.store.daily.now()-parent['created']>6*3600
                        or parent['conversation']!=job['conversation']):
                    raise ContractError('时间选项已过期，请重新提问。')
                query = choose(parent['plan']['query'],choice['command'])
            else:
                query = parse(job['text'], context, now=job['created'])
            if query['mode']=='clarify':
                plan=dict(query=query,hits=[])
                self.store.prepare(job['id'],plan)
                self.clarify(job,plan,context)
                return
            if query['mode'] in ('help', 'reset'):
                self.store.complete(job['id'], dict(status='local', text=HELP if query['mode']=='help' else '已开始新问题，之前的问答上下文已清除。', hits=[]),
                                    {} if query['mode']=='reset' else None)
                return
            summary = self.index.sync(self.sources)
            if query['mode']=='original':
                source=self.sources.get(query['selected'][0])
                hits=self.index.search(source['title'],material_ids=query['selected'],limit=1,expand=False)
            elif query['mode'] in ('weekly','weekly_answer'):
                anchor=job['created']
                parent_id=job['proof'].get('qa_weekly_parent')
                if parent_id:
                    parent=self.store.get(parent_id)
                    if (not parent or parent['state']!='sent' or not parent['plan']
                            or parent['conversation']!=job['conversation']
                            or self.store.daily.now()-parent['created']>6*3600
                            or 'weekly' not in parent['plan']['query']):
                        raise ContractError('周报选项已过期，请重新提问。')
                    snapshot=parent['plan']['query']['weekly']
                    query['question'],anchor=snapshot['question'],snapshot['at']
                query['weekly']=qa_weekly.plan(self.index.materials(),query['question'],anchor)
                hits=qa_weekly.evidence(query['weekly'],self.index) if query['mode']=='weekly_answer' else []
            else:
                hits = self.index.search(query.get('query', query['question']), material_ids=query['selected'],
                                         year_from=query.get('year_from'), year_to=query.get('year_to'), time_scope=query.get('time_scope'))
            if query['mode']=='source':
                quote = re.search(r'[“「"](.+)[”」"]', query['question'])
                if quote:
                    hits = [h for h in hits if quote[1] in h['text']]
            plan = dict(query=query, hits=hits, index_summary=summary)
            self.store.prepare(job['id'], plan)
        if plan['query']['mode']=='clarify':
            self.clarify(job,plan,context)
            return
        query, hits = plan['query'], plan['hits']
        self.sources.verify(hits)
        if 'weekly' in query:
            qa_weekly.verify(query['weekly'],self.sources)
        if query['mode']=='original':
            self.store.complete(job['id'],dict(status='original',text='准备原件入口。' if hits else '未找到该资料的可用原文，请重新查询。',hits=hits))
            return
        if query['mode']=='weekly':
            self.store.complete(job['id'],dict(status='local',text=query['weekly']['text'],hits=[],menu='weekly'))
            return
        selected = query['selected']
        if selected and not set(selected) <= {h['material_id'] for h in hits}:
            text = '所选资料中有材料未找到相关原文，暂不生成比较结论。请换用关键词或重新查文献。\n\n'+render_search(hits)
            status = 'partial_retrieval'
        elif not hits or query['mode'] in ('search', 'source', 'version'):
            text, status = render_search(hits, version=query['mode']=='version'), 'retrieval'
        elif (any(not t['qa_usable'] for h in hits for t in h.get('table_context', []))
              or sum(len(h['text']) for h in hits) > 24000):
            text = '检索命中待核查、过长的表格，或超出本次回答容量。请先对照原表或缩小范围；本次未调用模型。\n\n'+render_search(hits)
            status = 'table_review_required'
        else:
            try:
                output = self.model.run(job, plan)
            except Deferred as error:
                if error.reason.startswith('subscription_'):
                    raise  # Unsent work stays queued, with no consumed model call.
                labels = {'model_limit':'累计模型额度已用完', 'qa_model_approval_required':'本次问答模型尚未启用',
                          'qa_model_grant_expired':'本次问答授权时间已到'}
                reason = labels.get(error.reason, '本次模型结果未通过核验或状态不明，已停止自动重试')
                text = reason+'。以下保留原文检索结果，未生成综合回答。\n\n'+render_search(hits)
                status = error.reason
            else:
                output = comparison_coverage(output, hits, selected if query['mode']=='compare' else ())
                text, status = render_plan_answer(output, plan), output['status']
        if query['mode']=='weekly_answer' and status not in ('grounded','partial','not_found'):
            text=query['weekly']['text']+'\n\n'+qa_weekly.scope(query['weekly'],hits)+'\n\n'+text
        if query['mode']=='weekly_answer' and not hits:
            text=query['weekly']['text']+'\n\n没有可用于观点整理的原文，本次未调用模型。'
        if status not in ('grounded','partial','not_found'):
            text=describe(text,query)
        if 'weekly' in query:
            qa_weekly.verify(query['weekly'],self.sources)
        self.sources.verify(hits)
        materials = list(dict.fromkeys(h['material_id'] for h in hits))[:8]
        topic = context.get('question', query['question']) if query['mode']=='followup' else query['question']
        if query['mode']=='compare':
            topic = (context.get('question', '')+' '+query['question']).strip()[:2000]
        new_context = dict(question=topic,
                           selected=selected, materials=materials, year_from=query.get('year_from'), year_to=query.get('year_to'),time_scope=query.get('time_scope'))
        self.store.complete(job['id'], dict(status=status, text=text, hits=hits, **({'menu':'result'} if hits else {})), None if 'weekly' in query else new_context)


    def clarify(self, job, plan, context):
        query=plan['query']
        self.store.complete(job['id'],dict(status='clarification',text=time_prompt(query),hits=[],menu='time'),
                            dict(context,pending_time=query))
