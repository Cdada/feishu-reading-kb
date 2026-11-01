"""One durable message per newly admitted material, driven by local facts.

The scheduler alone writes cards. Heavy work only updates existing local state;
failed/unknown writes retain their exact intent and never restart business work.
"""
import hashlib
import json
from pathlib import Path
from .contracts import ContractError, stable_key
from .daily_cards import render_card
from .daily_card_io import read_card
from .daily_card_readback import card_matches
from .daily_scheduler import MaterialProblem
from .daily_store import Deferred
from .f01 import PREFIX
from .f05 import file_sha
from .registry import encoded
from .archive_handoff import ArchiveHandoff
from .archive_usage import attributed


LABELS = {
    'registered':'已收到资料，正在排队并核对运行条件。',
    'original_saved':'原件已保存，正在准备解析和核验转换结果。',
    'converted':'转换结果已核验，正在准备导读。',
    'generating':'正在生成导读，并核对引用、阅读覆盖和输出格式。',
    'model_unknown':'模型结果尚不明确，等待核查；不会自动重复生成。',
    'guide_ready':'导读已生成，正在制作预览 PDF。',
    'preview_ready':'正在发送并核验导读 PDF。',
    'send_unknown':'正在核查导读 PDF 的发送结果。',
    'awaiting_images':'等待缺图处理选择，请先查看对应缺图说明。',
    'awaiting_confirmation':'导读 PDF 已发送。请先阅读对应版本，再选择是否入库。',
    'confirmed':'已记录你的选择，正在保存材料包；无需再次点击。',
    'reading':'材料包已保存，正在进行知识整理；结果将按现有流程通知。',
    'reading_wait':'材料包已保存，完整整理等待补充信息。',
    'published':'材料包已入库，后续结果请查看完成通知。',
    'notified':'处理已完成，请查看对应完成通知。',
    'rejected':'已按你的选择暂不入库，资料和导读保留。',
    'duplicate':'同一原件已有处理记录，本次不重复生成。',
    'held':'本篇暂停，已有成果保留，等待后续处理。',
    'needs_upload':'需要补传完整原件，已有记录保留。',
}
TERMINAL = {'notified','rejected','duplicate','held','needs_upload'}
READING_LABELS = {
    'approved':'材料包已保存，正在准备知识整理。',
    'organizing':'正在按知识库规范整理笔记、适用的知识卡片和主题入口。',
    'reviewing':'正在进行独立复核；复核通过前不会发布整理成果。',
    'ready':'独立复核及本机成果校验已通过，等待发布。',
    'publishing':'正在发布知识整理成果。',
    'completed':'知识整理成果已发布，正在核验完成状态。',
    'needs_user_input':'材料包已保存，完整整理等待你补充来源信息。',
    'failed':'独立复核未通过或整理暂停，尚未标记整理完成。',
}


class MainCards:
    def __init__(self, backend):
        self.b, self.store = backend, backend.store
        self.cards, self.delivery = backend.card_flow.cards, backend.card_flow.delivery
        self.handoff=ArchiveHandoff(backend)

    def handles(self, mid):
        return self.store.document(mid)['data'].get('archive_main') is True

    def state(self, mid):
        return self.store.get('archive-main:'+mid)

    def activity(self, document, label):
        if self.handles(document['message_id']):
            self.store.put('archive-main-active:'+document['message_id'],
                dict(phase=document['phase'],label=label))

    def ensure(self, mid):
        state = self.state(mid)
        if state is None:
            document = self.store.document(mid)
            if not self.handles(mid):
                raise ContractError('historical tasks keep their original cards')
            item, target = self.b.collector.observed(mid)
            binding = dict(purpose='archive_main', document_id=mid, target_message_id=mid,
                source_sha256=hashlib.sha256(encoded(item).encode()).hexdigest(),
                details=LABELS['registered'], decision=None)
            card = self.cards.prepare(binding, document['filename'])
            state = dict(card_id=card['card_id'], target=target, sequence=0, last_at=0)
            self.store.put('archive-main:'+mid, state)
        card = self.cards.get(state['card_id'])
        if card['receipt'] is None:
            # Existing card-send reconciliation checks the physical result
            # before any second POST; the UUID and initial body stay fixed.
            card = self.delivery.send(card, state['target'])
        if 'receipt' not in state:
            state.update(receipt=card['receipt'], binding=card['binding'], last_at=self.store.now())
            self.store.put('archive-main:'+mid, state)
            if self.store.timing is not None:
                self.store.timing.feedback(mid)
        return state

    def desired(self, document, state):
        mid, phase = document['message_id'], document['phase']
        detail = LABELS[phase]
        active=self.store.get('archive-main-active:'+mid)
        if active and active['phase']==phase:
            detail=active['label']
        material_id=document['data'].get('reading_material_id')
        if material_id:
            rows=self.store.rows('SELECT state FROM reading_materials WHERE material_id=?',(material_id,))
            if rows and phase in ('reading','reading_wait','held'):
                detail=READING_LABELS[rows[0]['state']]
                if active and active.get('reading_detail'):
                    detail=active['label']
        if phase in ('published','notified'):
            if document['data'].get('reading_completed'):
                detail='归档与知识整理已完成。原件、导读、笔记及适用的卡片和主题已保存。'
            else:
                detail='材料包已入库；本任务未启用完整知识整理。'
            detail+='\n归档流程未核验问答索引；下次提问时按既有规则刷新。'
        snapshot = self.store.get('pdf-progress:'+mid)
        if phase=='original_saved' and snapshot:
            if snapshot['source_sha'] != document['source_sha']:
                raise ContractError('main card progress source changed')
            done, total = snapshot['completed_pages'], snapshot['total_pages']
            if type(done) is not int or type(total) is not int or not 0<=done<=total<=100 or total<1:
                raise ContractError('invalid main card page count')
            label = {'converting':'正在解析页面','retrying':'正在尝试既有 OCR 后备解析',
                     'merging':'正在校验和合并页面','converted':'页面已转换，正在整理结果'}[snapshot['stage']]
            detail = f'{label}：{done}/{total} 页成功。\n页面转换完成后，还需生成导读并由你确认入库。'
        mineru=self.store.get('mineru_status')
        if phase=='original_saved' and mineru and mineru['kind']=='starting':
            detail='正在启动 MinerU；服务就绪后，首批页面仍可能需要加载模型。'
        deferred = self.store.get('defer:'+mid) or {}
        reason = deferred.get('reason', '')
        if phase not in TERMINAL and reason and (deferred.get('until') or 0)>self.store.now():
            if reason in ('paused_resources','paused_capacity','stopping'):
                detail += '\n资源不足或服务暂停，已有成果保留，恢复条件后继续。'
            elif reason=='storage_unavailable':
                detail += '\n资料盘暂不可用，等待恢复。'
            elif reason=='model_limit' or reason.startswith('subscription_'):
                detail += '\n模型使用条件暂不满足，保留排队。'
        decision = None
        if phase in ('awaiting_confirmation','awaiting_images'):
            listener=getattr(self.b,'card_listener',None)
            if listener is None or not listener.ready:
                return dict(state['binding'],details=detail+'\n确认按钮正在等待连接恢复；仍可回复对应 PDF 或缺图说明。',decision=None)
            binding, target = self.b.card_flow.material(document)
            action = self.cards.prepare(binding, document['filename'])
            self.store.put('archive-main-action:'+action['card_id'], mid)
            if self.cards.choice(action['card_id']) is None:
                decision = dict(card_id=action['card_id'], binding=binding)
                if phase=='awaiting_confirmation':
                    detail += f"\n对应导读第 {binding['preview_version']} 版（聊天中的导读 PDF 文件）。"
            else:
                detail = '已收到你的选择，正在核对并执行；无需再次点击。'
        handoff=self.store.get('archive-handoff:'+mid)
        if handoff and handoff['facts']['category'] in ('codex','user','resource'):
            identity=handoff['facts']['id']
            label={'codex':'需要 Codex 协助','user':'等待你补充信息','resource':'等待运行条件恢复'}[handoff['facts']['category']]
            detail+=f'\n{label}。处理编号：{identity}\n本机《待处理材料与接续说明》→ {identity}'
        if self.store.get('archive-handoff-error'):
            detail+='\n本机接续说明更新失败，请由 Codex 先查看服务状态。'
        return dict(state['binding'], details=detail, decision=decision)

    def _write(self, mid, state):
        pending = state['pending']
        receipt = state['receipt']
        t = self.b.transport
        for name in ('sent','readback','target'):
            t._checked_result(receipt[name])
        card = self.cards.get(state['card_id'])
        desired = dict(card, binding=pending['binding'])
        content = encoded(render_card(desired))
        contract = dict(receipt['contract'], binding=pending['binding'], content=content,
            result='', previous_content=receipt['contract']['content'], previous_binding=state['binding'])
        evidence = dict(receipt['sent']['evidence'], **receipt['readback']['evidence'], **receipt['target']['evidence'])
        decision = pending['binding']['decision']
        if decision:
            target = pending['decision_target']
            t._checked_result(target)
            contract['decision_receipt'] = target['receipt']
            evidence.update(target['evidence'])
        else:
            contract.pop('decision_receipt', None)
        operation = 'archive-main-update:'+mid+':'+str(pending['sequence'])
        written = t.request(operation, 'send', PREFIX+'/im/v1/messages/'+card['message_id'],
            mode='card_update', body=encoded(dict(content=content)), card=contract,
            evidence=evidence, confirmation=True)
        actual, checked = read_card(t, operation+':readback', contract, dict(evidence, **written['evidence']))
        if not card_matches(actual, render_card(desired)):
            raise MaterialProblem('主卡更新后的内容无法核对，资料处理记录保留。')
        state.update(binding=pending['binding'], sequence=pending['sequence'], last_at=self.store.now(),
            display_key=pending.get('display_key'), observed=pending.get('observed'),
            receipt=dict(receipt, readback=checked, contract=contract))
        state.pop('pending')
        self.store.put('archive-main:'+mid, state)

    @attributed('id')
    def report_one(self, mid, *, force=False):
        state = self.state(mid) or {}
        if state.get('disabled'):
            self.fallback(mid, state)
            return
        wait = self.store.get('archive-main-wait:'+mid) or {}
        if wait.get('until',0)>self.store.now():
            return
        try:
            state = self.ensure(mid)
            document = self.store.document(mid)
            observed=self.observation(document)
            if not state.get('pending'):
                desired = self.desired(document, state)
                if desired == state['binding']:
                    if state.get('observed')!=observed:
                        state['observed']=observed
                        self.store.put('archive-main:'+mid,state)
                    return
                snapshot=self.store.get('pdf-progress:'+mid)
                display_key=desired
                if document['phase']=='original_saved' and snapshot:
                    # At most ten ordinary page milestones, leaving capacity
                    # for decisions, interruptions, review and publication.
                    display_key=dict(stage=snapshot['stage'],bucket=snapshot['completed_pages']*10//snapshot['total_pages'],
                        defer=self.store.get('defer:'+mid))
                if state.get('display_key')==display_key:
                    return
                if not force and document['phase'] not in TERMINAL|{'awaiting_confirmation','awaiting_images','confirmed'} and self.store.now()-state['last_at']<15:
                    return
                state['pending'] = dict(binding=desired, sequence=state['sequence']+1,display_key=display_key,observed=observed)
                if desired['decision']:
                    _, target = self.b.card_flow.material(document, desired['decision']['binding']['purpose'])
                    state['pending']['decision_target'] = target
                self.store.put('archive-main:'+mid, state)
            self._write(mid, state)
            self.store.put('archive-main-wait:'+mid, None)
        except Deferred as error:
            self.store.put('archive-main-wait:'+mid, dict(reason=error.reason, until=error.until or self.store.now()+15))
            if error.reason in ('card_update_limit','attempts_exhausted'):
                state.update(disabled=True, error=error.reason)
                self.store.put('archive-main:'+mid, state)
        except MaterialProblem as error:
            state.update(disabled=True, error=error.reason)
            self.store.put('archive-main:'+mid, state)

    def fallback(self, mid, state):
        if state.get('fallback'):
            return
        self.record_handoffs()
        saved=self.store.rows('SELECT payload FROM daily_operations WHERE op_key=?',('notice:archive-main-fallback:'+mid,))
        handoff=self.store.get('archive-handoff:'+mid)
        identity=handoff['facts']['id'] if handoff else '待生成'
        text=f'这份资料的主卡暂时无法更新，已有决定与成果保留。处理编号：{identity}；请由 Codex 查看本机《待处理材料与接续说明》。'
        if self.store.get('archive-handoff-error'):
            text=f'这份资料的主卡暂时无法更新，已有决定与成果保留。处理编号：{identity}；接续说明写入失败，请由 Codex 先查看本机服务状态。'
        if saved:text=json.loads(saved[0]['payload'])['notice_plan']['payload']
        proof = self.b.notice('archive-main-fallback:'+mid,text,confirmation=True)
        state['fallback'] = proof
        self.store.put('archive-main:'+mid, state)

    def report(self):
        # Round robin bounds display work; no new scheduler/thread or polling.
        rows = self.store.rows('SELECT message_id FROM daily_documents ORDER BY created,message_id')
        mids=[]
        for row in rows:
            mid=row['message_id']
            if not self.handles(mid):continue
            document=self.store.document(mid);state=self.state(mid) or {}
            if state.get('disabled') and state.get('fallback'):continue
            if document['phase'] in TERMINAL and not state.get('pending') and state.get('observed')==self.observation(document):
                continue
            mids.append(mid)
        if not mids:
            return
        offset = self.store.get('archive-main-round-robin') or 0
        self.store.put('archive-main-round-robin', offset+1)
        self.report_one(mids[offset % len(mids)])

    def observation(self, document):
        handoff=self.store.get('archive-handoff:'+document['message_id']) or {}
        return dict(phase=document['phase'],updated=document['updated'],handoff_at=handoff.get('updated'),
                    handoff_error=self.store.get('archive-handoff-error'))

    def record_handoffs(self):
        try:
            for row in self.store.rows('SELECT message_id FROM daily_documents ORDER BY created,message_id'):
                if self.handles(row['message_id']):
                    self.handoff.observe(self.store.document(row['message_id']),flush=False)
            self.handoff.render()
            self.store.put('archive-handoff-error',None)
        except (OSError,ContractError) as error:
            # Display-document failure must be visible, without altering the
            # archive state, retrying a model, or overwriting human notes.
            self.store.put('archive-handoff-error',dict(kind=type(error).__name__,
                reason='接续说明无法更新，请检查文件可写性、机器状态标记和现有临时文件。'))

    def completion(self, document):
        self.record_handoffs()
        self.report_one(document['message_id'],force=True)
        state=self.state(document['message_id'])
        if state and state.get('disabled'):
            self.record_handoffs()
            self.fallback(document['message_id'],state)
            return dict(kind='main_card_unavailable',handoff=self.handoff.path.name,
                        notice=self.state(document['message_id']).get('fallback'))
        if not state or state.get('pending') or state.get('binding')!=self.desired(document,state):
            raise Deferred('archive_main_completion_pending')
        return dict(kind='main_card',message_id=self.cards.get(state['card_id'])['message_id'],
                    receipt=state['receipt']['readback'])

    def action_state(self, card):
        mid = self.store.get('archive-main-action:'+card['card_id'])
        return self.state(mid) if mid else None

    @attributed('document_id')
    def read_event(self, card, event, proof):
        state = self.action_state(card)
        # An interrupted PATCH must be reconciled before admitting a click.
        if state.get('pending') or not state.get('receipt'):
            return None
        action = state['binding']['decision']
        if not action or action['card_id'] != card['card_id']:
            return 'stale'
        receipt = state['receipt']
        evidence = dict(receipt['sent']['evidence'], **receipt['target']['evidence'], **receipt['readback']['evidence'])
        binding, target = self.b.card_flow.material(self.store.document(card['document_id']), card['purpose'])
        if binding != action['binding']:
            return 'stale'
        # Each guarded HTTP child needs the decision's PDF/image receipt as
        # well as the original attachment and main-card receipts. A previous
        # PATCH having checked that evidence does not grant a later GET access.
        self.b.transport._checked_result(target)
        evidence.update(target['evidence'])
        actual, checked = read_card(self.b.transport, 'main-event-read:'+stable_key(event['event_id'], target['receipt']),
                                   receipt['contract'], evidence)
        desired = dict(self.cards.get(state['card_id']), binding=state['binding'])
        if not card_matches(actual, render_card(desired)):
            raise ContractError('callback main card differs from its verified definition')
        event['card_content'] = encoded(actual)
        proof['receipts'].update(checked['evidence'])
        return self.b.card_flow.receive(encoded(event), proof)

    def receive(self, card, event, proof):
        state = self.action_state(card)
        action = state['binding'].get('decision') if state else None
        if not action or action['card_id']!=card['card_id']:
            return 'stale'
        root = self.cards.get(state['card_id'])
        if event['message_id']!=root['message_id']:
            raise ContractError('main card callback physical message differs')
        document = self.store.document(card['document_id'])
        expected = 'awaiting_confirmation' if card['purpose']=='preview' else 'awaiting_images'
        if document['phase']!=expected:
            return 'stale'
        binding, target = self.b.card_flow.material(document, card['purpose'])
        evidence = dict(proof, reference='P40-owner-card-v1', rules_sha256=self.b.authority.digest,
            event_id=event['event_id'], card_id=card['card_id'], card_message_id=root['message_id'],
            message_id='card-event:'+event['event_id'], reply_to=binding['target_message_id'],
            command=json.loads(event['action_value'])['command'])
        def access():
            self.b._check_material(document)
            if any(file_sha(Path(p))!=digest for p,digest in proof['receipts'].items()):
                raise ContractError('durable main card callback evidence changed')
            self.b.transport._checked_result(target)
            self.b.transport._checked_result(state['receipt']['readback'])
        return self.cards.accept_event(encoded(event), evidence, app_id=self.store.policy.app_id,
            current_binding=binding, checked_access=access, main_message_id=root['message_id'])
