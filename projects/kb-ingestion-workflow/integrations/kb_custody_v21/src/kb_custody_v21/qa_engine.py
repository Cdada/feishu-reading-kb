"""Run a queued question in the single heavy lane; no network in retrieval."""
import re

from .contracts import ContractError
from .daily_store import Deferred
from .qa_time import choose, describe, prompt as time_prompt
from .qa_model import comparison_coverage, render_answer
from .qa_query import HELP, parse, render_search, render_weekly


class QAEngine:
    def __init__(self, store, sources, index, model):
        self.store, self.sources, self.index, self.model = store, sources, index, model

    def run(self, job):
        context = self.store.context(job['conversation'])
        plan = job['plan']
        if plan is None:
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
            if query['mode']=='weekly':
                text = render_weekly(self.index.materials(), query['question'], job['created'])
                self.store.complete(job['id'], dict(status='local', text=text, hits=[]))
                return
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
        selected = query['selected']
        if selected and not set(selected) <= {h['material_id'] for h in hits}:
            text = '所选资料中有材料未找到相关原文，暂不生成比较结论。请换用关键词或重新查文献。\n\n'+render_search(hits)
            status = 'partial_retrieval'
        elif not hits or query['mode'] in ('search', 'source', 'version'):
            text, status = render_search(hits, version=query['mode']=='version'), 'retrieval'
        else:
            try:
                output = self.model.run(job, plan)
            except Deferred as error:
                labels = {'model_limit':'累计模型额度已用完', 'qa_model_approval_required':'本次问答模型尚未启用',
                          'qa_model_grant_expired':'本次问答授权时间已到'}
                reason = labels.get(error.reason, '本次模型结果未通过核验或状态不明，已停止自动重试')
                text = reason+'。以下保留原文检索结果，未生成综合回答。\n\n'+render_search(hits)
                status = error.reason
            else:
                output = comparison_coverage(output, hits, selected if query['mode']=='compare' else ())
                text, status = render_answer(output, hits, selected if query['mode']=='compare' else ()), output['status']
        text=describe(text,query)
        self.sources.verify(hits)
        materials = list(dict.fromkeys(h['material_id'] for h in hits))[:8]
        topic = context.get('question', query['question']) if query['mode']=='followup' else query['question']
        if query['mode']=='compare':
            topic = (context.get('question', '')+' '+query['question']).strip()[:2000]
        new_context = dict(question=topic,
                           selected=selected, materials=materials, year_from=query.get('year_from'), year_to=query.get('year_to'),time_scope=query.get('time_scope'))
        self.store.complete(job['id'], dict(status=status, text=text, hits=hits), new_context)


    def clarify(self, job, plan, context):
        query=plan['query']
        self.store.complete(job['id'],dict(status='clarification',text=time_prompt(query),hits=[],menu='time'),
                            dict(context,pending_time=query))
