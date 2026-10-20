"""P42 optional owner choices after complete reading publication.

Tasks and choices share the existing SQLite journal. Exact publication plans
are saved before any library write; a restart resumes that plan first. This
component never reserves a model turn and never backfills historical intake.
"""
import json
import os
from pathlib import Path

from .contracts import ContractError, stable_key
from .daily_scheduler import MaterialProblem
from .daily_store import Deferred
from .f05 import file_sha
from .reading_contract import sha
from .reading_publish import prepare_plan, publish_plan
from .registry import encoded
from .relation_catalog import Catalog, RelationProblem, candidates, need
from .relation_cards import RELATION_COMMANDS, selection
from .relation_library import build_bundle, graph_at, pair_id, KINDS, GRAPH
from .safe_publish import open_root, root_identity

TASK = 'relation-task:'
CONTEXT = 'relation-context:'


class RelationFlow:
    def __init__(self, backend):
        self.b, self.store = backend, backend.store
        self.profile = backend.authority.reading
        self.catalog = Catalog(self.profile, self.store)
        self.cards = backend.card_flow.cards
        self.delivery = backend.card_flow.delivery

    def _save_new(self, key, value):
        old = self.store.get(key)
        if old is not None:
            need(old == value, '已保存的关联步骤不能替换。')
        else:
            self.store.put(key, value)

    def start(self, document):
        self.b.check_authorization()
        mid = document['data'].get('reading_material_id')
        need(document['phase'] in ('published','notified') and document['data'].get('reading_completed') and mid,
             '完整入库后才能检索关联。')
        material = self.catalog.reading.get(mid)
        need(material['state']=='completed' and material['document_id']==document['message_id']
             and material['binding']['source_sha256']==document['source_sha'], '完整整理记录与原件不匹配。')
        task_id = stable_key('P42-start', mid)
        if self.store.get(TASK+task_id) is None:
            self.store.put(TASK+task_id, dict(task_id=task_id, kind='scan', state='active',
                document_id=document['message_id'], material_id=mid, data={}))

    def _graph(self):
        if not self.profile.root.exists():
            raise RelationProblem('测试库尚未建立。')
        fd = open_root(self.profile.root, root_identity(self.profile.root))
        try:
            graph, raw = graph_at(fd)
            previous=self.store.get('relation-graph-sha256')
            pending=self.store.get('relation-write-pending') or {}
            if raw is not None:
                need(sha(graph) in (previous,pending.get('graph_sha256')), '关联记录已发生未登记的变化，请先核查。')
            else:
                need(previous is None, '已登记的关联记录缺失。')
            return graph
        finally:
            os.close(fd)

    @staticmethod
    def _edge_hash(graph, pid):
        edge = graph['edges'].get(pid)
        return sha(edge) if edge is not None else None

    def _target(self, task):
        document = self.store.document(task['document_id'])
        need(document['phase'] in ('published','notified') and document['data'].get('reading_completed')
             and document['data'].get('reading_material_id')==task['material_id'], '归档完成记录不再匹配。')
        material=self.catalog.reading.get(task['material_id'])
        need(material['state']=='completed' and material['document_id']==document['message_id']
             and material['binding']['source_sha256']==document['source_sha'], '关联原件身份不再匹配。')
        original, target = self.b.card_flow.material(document, 'preview')
        return document, original, target

    def _task(self, parent, kind, data, identity):
        task_id = stable_key('P42-task', parent['material_id'], kind, identity)
        return dict(task_id=task_id, kind=kind, state='active', document_id=parent['document_id'],
                    material_id=parent['material_id'], data=data)

    def _card(self, task):
        document, original, target = self._target(task)
        data = task['data']; current = data['current']
        detail = '本篇：'+current['title']+'\n两份资料均已完成入库。\n\n'
        if task['kind']=='choose':
            for i, item in enumerate(data['candidates'],1):
                detail += f"候选 {i}：{item['title']}\n依据："+'；'.join(item['evidence'])+'\n\n'
            detail += ('相似信息仅供判断；选择后还需确认关系。可直接结束，不作关联。'
                       if data['candidates'] else '未找到足够相关的资料，本次关联检查已结束，不影响入库。')
            extra = dict(candidate_count=len(data['candidates']))
            purpose = 'relation_candidates'
        else:
            detail += '候选：'+data['other']['title']+'\n\n'
            extra = dict(pair_id=pair_id(current['material_id'],data['other']['material_id']))
            purpose = 'relation_'+task['kind']
            if task['kind']=='type':
                detail += '请确认类型和方向。选择后将保存两篇资料的双向链接；版本关系必须属于同一作品。取消不会改动已有关系。'
            else:
                edge = data['edge']
                label = KINDS[edge['kind']]
                current_role = label[1 if current['material_id']==edge['left'] else 0]
                other_role = label[0 if current['material_id']==edge['left'] else 1]
                detail += '已确认：本篇为'+current_role+'；候选为'+other_role+'。\n双向链接和关联目录已核验。可更正或撤销，原件与独立导读继续保留。'
        snapshot = dict(task_id=task['task_id'], material_id=task['material_id'], data=data)
        binding = dict(purpose=purpose, document_id=document['message_id'], source_sha256=document['source_sha'],
            target_message_id=original['target_message_id'], material_id=task['material_id'],
            snapshot_sha256=sha(snapshot), revision=data.get('revision',0), details=detail, **extra)
        card = self.cards.prepare(binding, '入库后的资料关联')
        context = dict(task_id=task['task_id'], snapshot=snapshot, binding=binding)
        self._save_new(CONTEXT+card['card_id'], context)
        return card, target

    def current(self, card):
        context = self.store.get(CONTEXT+card['card_id'])
        need(context and context['binding']==card['binding'] and sha(context['snapshot'])==card['binding']['snapshot_sha256'],
             '关联卡片的本地绑定已变化。')
        task = self.store.get(TASK+context['task_id'])
        _, _, target = self._target(task)
        valid = task['state'] in ('active','waiting') and task['data']==context['snapshot']['data']
        if valid:
            try:
                current = self.catalog.get(task['material_id'])
                valid = current == task['data']['current']
                if task['kind'] in ('type','manage'):
                    other = self.catalog.get(task['data']['other']['material_id'])
                    pid = card['binding']['pair_id']
                    valid = valid and other==task['data']['other'] and self._edge_hash(self._graph(),pid)==task['data']['expected_edge']
            except RelationProblem as error:
                self.store.put('relation-wait',dict(task_id=task['task_id'],reason=str(error)))
                valid=False
        return (card['binding'] if valid else dict(card['binding'],snapshot_sha256='0'*64)), target

    def _complete(self, card, text, tasks=()):
        """Commit UI outcome and continuation tasks in one SQLite transaction."""
        self.b.check_authorization()
        context = self.store.get(CONTEXT+card['card_id'])
        with self.store.transaction() as db:
            choice = db.execute('SELECT result FROM daily_card_choices WHERE card_id=?',(card['card_id'],)).fetchone()
            need(choice is not None, '关联缺少本人选择。')
            if choice['result'] is not None:
                need(json.loads(choice['result'])['text']==text, '关联结果不能替换。')
                return
            task = json.loads(db.execute('SELECT value FROM daily_meta WHERE key=?',(TASK+context['task_id'],)).fetchone()[0])
            task['state']='done'
            for value in (task,*tasks):
                key=TASK+value['task_id']
                db.execute('INSERT INTO daily_meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                           (key,encoded(value)))
            db.execute('UPDATE daily_card_choices SET result=? WHERE card_id=?',(encoded(dict(text=text)),card['card_id']))
            db.execute("UPDATE daily_cards SET state='completed' WHERE card_id=?",(card['card_id'],))
            pending=db.execute('SELECT value FROM daily_meta WHERE key=?',('relation-write-pending',)).fetchone()
            pending=json.loads(pending[0]) if pending else None
            if pending:
                need(pending['card_id']==card['card_id'], '待恢复关联身份不符。')
                db.execute('INSERT INTO daily_meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                           ('relation-graph-sha256',encoded(pending['graph_sha256'])))
            db.execute('INSERT INTO daily_meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                       ('relation-write-pending','null'))

    def _remaining(self, task, card):
        remaining = task['data'].get('remaining',[])
        if not remaining:
            return []
        return [self._task(task,'choose',dict(current=task['data']['current'],candidates=remaining),card['card_id'])]

    def _apply(self, card):
        context = self.store.get(CONTEXT+card['card_id'])
        task = self.store.get(TASK+context['task_id']); data=task['data']
        choice = self.cards.choice(card['card_id']); command=choice['command']
        evidence=choice['evidence']
        self.b.check_authorization()
        need(all(file_sha(Path(p))==h for p,h in evidence['receipts'].items()), '关联选择的回执已变化。')
        pending = self.store.get('relation-write-pending')
        if pending is None:
            binding, _ = self.current(card)
            if binding != card['binding']:
                self._complete(card,'此卡片已过期，未修改关联。请查看最新关联卡片。'); return
        if task['kind']=='choose':
            if command=='结束关联':
                self._complete(card,'本次关联检查已结束，资料已保留。'); return
            selected = data['candidates'][int(command[-1])-1]
            other=self.catalog.get(selected['material_id'])
            pid=pair_id(task['material_id'],other['material_id'])
            next_data=dict(current=data['current'],other=other,
                expected_edge=self._edge_hash(self._graph(),pid),
                remaining=[v for v in data['candidates'] if v['material_id']!=other['material_id']])
            next_task=self._task(task,'type',next_data,card['card_id'])
            self._complete(card,'已选择候选，请在下一张卡片确认类型和方向。',[next_task]); return
        if task['kind']=='manage' and command=='更正关联':
            next_task=self._task(task,'type',dict(data,remaining=[]),card['card_id'])
            self._complete(card,'请在下一张卡片重新选择关系；确认前保留原关联。',[next_task]); return
        if command=='取消本次关联':
            continuations=self._remaining(task,card)
            edge=self._graph()['edges'].get(card['binding']['pair_id'])
            if edge and edge['active']:
                continuations.append(self._task(task,'manage',dict(data,edge=edge,expected_edge=sha(edge)),card['card_id']))
            self._complete(card,'已取消本次选择，已有关系和资料均保留。',continuations); return
        remove=command=='撤销关联'
        selected=(dict(left=data['edge']['left'],right=data['edge']['right'],kind=data['edge']['kind'])
                  if remove else selection(command,task['material_id'],data['other']['material_id']))
        job=self.b.authority.root/'relation-jobs'/card['card_id']
        if pending is None:
            bundle=build_bundle(self.profile,self.catalog,operation_id=card['card_id'],**selected,remove=remove,
                expected_edge=data['expected_edge'],evidence=evidence,checked_access=self.b.check_authorization)
            plan_sha=prepare_plan(self.profile,bundle,job,checked_access=self.b.check_authorization)
            pending=dict(card_id=card['card_id'],plan_sha256=plan_sha,graph_sha256=sha(json.loads(bundle['files'][GRAPH])))
            self.store.put('relation-write-pending',pending)
        need(pending['card_id']==card['card_id'], '另一项关联写入尚待恢复。')
        publish_plan(self.profile,job,pending['plan_sha256'],checked_access=self.b.check_authorization)
        edge=self._graph()['edges'][card['binding']['pair_id']]
        need(edge['operation_id']==card['card_id'], '关联结果与本次选择不一致。')
        continuations=self._remaining(task,card) if task['kind']=='type' else []
        if not remove:
            next_data=dict(current=data['current'],other=data['other'],edge=edge,expected_edge=sha(edge),revision=edge['revision'])
            continuations.append(self._task(task,'manage',next_data,card['card_id']))
        self._complete(card,'已撤销关联，双方链接和目录已更新，原件与导读保留。' if remove
                       else '关联完成：双方链接和关联目录均已核验。',continuations)

    def _drive(self, task):
        if task['kind']=='scan':
            all_materials=self.catalog.all()
            current=all_materials[task['material_id']]
            graph=self._graph()
            rows=[v for v in candidates(current,all_materials)
                  if not graph['edges'].get(pair_id(current['material_id'],v['material_id']),{}).get('active')]
            task=dict(task,kind='choose',data=dict(current=current,candidates=rows))
            self.store.put(TASK+task['task_id'],task)
        card,target=self._card(task)
        if card['state']=='prepared':
            if not self.b.card_listener.ready:
                raise Deferred('card_listener_not_ready')
            self.delivery.send(card,target)
        task['state']='done' if task['kind']=='choose' and not task['data']['candidates'] else 'waiting'
        self.store.put(TASK+task['task_id'],task)

    def _display(self):
        for row in self.store.rows("SELECT key,value FROM daily_meta WHERE key LIKE 'relation-stale:%' AND value!='null' ORDER BY rowid LIMIT 1"):
            stale=json.loads(row['value']);card=self.cards.get(stale['card_id'])
            self._show(card,'此关联卡片已过期或资料记录已变化，未修改关联。请查看最新卡片；资料已入库。')
            self.store.put(row['key'],None)
            return
        rows=self.store.rows("SELECT c.card_id FROM daily_cards c JOIN daily_card_choices q USING(card_id) WHERE c.purpose LIKE 'relation_%' AND q.result IS NOT NULL ORDER BY c.created")
        for row in rows:
            cid=row['card_id']; key='card-display:'+cid; display=self.store.get(key) or {}
            if display.get('status') in ('verified','fallback_verified'):
                continue
            card=self.cards.get(cid);text=self.cards.choice(cid)['result']['text']
            self._show(card,text)
            return

    def _show(self, card, text):
        cid=card['card_id'];key='card-display:'+cid;display=self.store.get(key) or {}
        if display.get('status') in ('verified','fallback_verified'):
            return
        try:
            if display.get('status')=='fallback_pending':
                self.b.card_flow._fallback(cid,text,display['reason'])
            else:
                proof=self.delivery.update(card,text)
                self.store.put(key,dict(status='verified',proof=proof))
        except MaterialProblem as error:
            self.b.card_flow._fallback(cid,text,error.reason)
        except Deferred as error:
            if error.reason in ('card_update_limit','attempts_exhausted'):
                self.b.card_flow._fallback(cid,text,error.reason)
            else:
                raise

    def stale(self, card):
        self.b.check_authorization()
        context=self.store.get(CONTEXT+card['card_id'])
        task=self.store.get(TASK+context['task_id']);task['state']='done'
        with self.store.transaction() as db:
            db.execute("UPDATE daily_cards SET state='superseded' WHERE card_id=? AND state='sent'",(card['card_id'],))
            for key,value in ((TASK+task['task_id'],task),('relation-stale:'+card['card_id'],dict(card_id=card['card_id']))):
                db.execute('INSERT INTO daily_meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(key,encoded(value)))

    def report(self):
        # Local association trouble is visible but cannot stop unrelated intake.
        self.b.check_authorization()
        task_id=None
        pending_notice=self.store.get('relation-problem-notice-pending')
        if pending_notice:
            self._problem_notice(pending_notice)
        try:
            choices=self.store.rows("SELECT c.card_id FROM daily_cards c JOIN daily_card_choices q USING(card_id) WHERE c.purpose LIKE 'relation_%' AND q.result IS NULL ORDER BY q.created,c.card_id")
            pending=self.store.get('relation-write-pending')
            if pending:
                choices.sort(key=lambda v:v['card_id']!=pending['card_id'])
            if choices:
                card=self.cards.get(choices[0]['card_id'])
                task_id=self.store.get(CONTEXT+card['card_id'])['task_id']
                self._apply(card)
            else:
                active=self.store.rows("SELECT value FROM daily_meta WHERE key LIKE 'relation-task:%' AND json_extract(value,'$.state')='active' ORDER BY rowid LIMIT 1")
                if active:
                    task=json.loads(active[0]['value']);task_id=task['task_id'];self._drive(task)
            self._display()
            self.store.put('relation-wait',None)
        except Deferred as error:
            self.store.put('relation-wait',dict(task_id=task_id,reason=error.reason,until=error.until))
        except (ContractError, OSError) as error:
            self.b.check_authorization()  # Revocation/configuration errors still stop execution.
            detail=dict(task_id=task_id,reason=str(error))
            self.store.put('relation-wait',detail)
            if task_id and not self.store.get('relation-write-pending') and not choices:
                task=self.store.get(TASK+task_id);task.update(state='blocked',error=str(error));self.store.put(TASK+task_id,task)
            self._problem_notice(detail)

    def _problem_notice(self, detail):
        self.store.put('relation-problem-notice-pending',detail)
        try:
            self.b.notice('relation-problem:'+sha(detail),
                '资料已完成入库，关联处理暂缓：需要检查本地关联记录。其他资料可以继续归档。',purpose='service',confirmation=True)
        except (Deferred,MaterialProblem):
            return  # Durable notice remains pending and is retried separately.
        self.store.put('relation-problem-notice-pending',None)
