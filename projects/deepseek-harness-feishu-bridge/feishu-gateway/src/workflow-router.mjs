import { createHash } from 'node:crypto';
import { consentReplies } from './content-consent.mjs';
import { validateIntent } from './workflow-intent.mjs';
import { curationIntent } from './curation-language.mjs';
import { translationReplies } from './translation-choice.mjs';
import {intentFailureText, failureKind, isWorkflowHelp, workflowHelp, localWorkflowRequest} from './workflow-feedback.mjs';

const controls = new Set([...Object.keys(consentReplies), '确认本次入库', '暂不入库', '确认撤回本次入库', '保留本次入库', '撤回本次入库', '撤回上次入库', '开始正文处理', '处理正文', '准备正文授权']);
const stamp = state => createHash('sha256').update(JSON.stringify([state.pendingCuration, state.lastCandidateTask,
  state.activeCurationRun, state.pendingContentConsent?.consent?.consent_id, state.pendingContentConsent?.phase,
  state.lastPublication, state.pendingPublication?.preview?.write_id,state.workflowV11?.id,state.workflowV11?.phase])).digest('hex');

export class WorkflowRouter {
  constructor({ classify, ownerId, save, send, now = () => Math.floor(Date.now() / 1000) }) {
    Object.assign(this, { classify, ownerId, save, send, now });
  }
  async handle(message, state, { bypass = false } = {}) {
    const text = (message.content ?? '').trim();
    const tell = text => this.send(message.chatId, { text }, { replyTo: message.messageId });
    if (message.senderId !== this.ownerId || message.chatType !== 'p2p') return { handled: true };
    if (bypass || message.resources?.length) {
      state.pendingWorkflowAction = null; await this.save(); return {};
    }
    if (message.rawContentType !== 'text' || !text) return {};
    if (controls.has(text) || translationReplies.has(text)) return {}; // Preserve original reply binding.
    if(isWorkflowHelp(text)){await tell(workflowHelp(state,{now:this.now(),text}));return {handled:true};}
    const pending = state.pendingWorkflowAction;
    if (text === '确认执行这项操作' || text === '不执行这项操作') {
      const issued = Math.floor(message.createTime / 1000);
      if (!pending || pending.chatId !== message.chatId || message.replyToMessageId !== pending.promptId
        || this.now() >= pending.expiresAt || !Number.isSafeInteger(issued) || issued < pending.createdAt
        || issued > this.now() + 30 || this.now() - issued > 300 || pending.binding !== stamp(state)) {
        await tell('这项操作的确认已失效或没有对应到当前提示。请重新说明想做什么；不会沿用旧确认。');
        return { handled: true };
      }
      state.pendingWorkflowAction = null; await this.save(); // Consume before dispatch; no replay.
      if (text === '不执行这项操作') { await tell('好的，不执行这项操作，已有结果保留。'); return { handled: true }; }
      return { intent: pending.intent, operations: pending.operations, answer: pending.original, confirmed: true };
    }
    let proposal;
    // Established unambiguous reads and stop requests need no extra model round trip.
    const request=localWorkflowRequest(text);
    const local = curationIntent(text);
    if(request)proposal={intent:request,scope:'current',operations:[]};
    else if (['preview', 'status'].includes(local)) proposal = { intent: local, scope: 'current', operations: [] };
    else if (['先停一下', '暂停整理', '不要继续处理', '别处理了'].includes(text)) proposal = { intent: 'cancel', scope: 'current', operations: [] };
    else {
      const before=stamp(state);
      const progress = setTimeout(() => { void tell('正在识别你这条消息希望执行的操作，尚未开始处理资料。后续回复会引用这条消息；如果你接着发送其他要求，我会分别处理。').catch(() => {}); }, 2000);
      try { proposal = validateIntent(await this.classify(message, state)); }
      catch(error) { console.warn(JSON.stringify({event:'workflow_intent_failure',kind:failureKind(error),at:new Date().toISOString()}));await tell(intentFailureText(error)); return { handled: true }; }
      finally { clearTimeout(progress); }
      if(before!==stamp(state)){
        await tell('识别消息期间，当前资料或处理阶段发生了变化。这次没有执行操作，请引用对应文件或最新提示重新说明。');return {handled:true};
      }
    }
    const operations = proposal.operations;
    const intent = proposal.intent === 'plan' && state.pendingCuration ? 'update_plan' : proposal.intent;
    if (proposal.scope !== 'current') {
      await tell('你说的是当前正在整理的这份资料，还是另一份？请先说明，我不会把不同材料混在一起处理。'); return { handled: true };
    }
    if(intent==='help'){await tell(workflowHelp(state,{now:this.now(),text}));return {handled:true};}
    if (intent === 'unknown' || intent === 'unsupported') {
      await tell(intent === 'unsupported'
        ? '这涉及当前入库流程以外的操作。这里可以准备计划、整理、查看预览、确认入库或撤回；删除、覆盖、跨资料合并和向其他渠道发送不会自动执行。'
        : '我还不确定你想查看结果、询问原因，还是要求修改。请再说明一下；询问和评价不会触发重新整理。');
      return { handled: true };
    }
    if (intent === 'answer' && !state.pendingCuration && state.activeCurationRun?.phase !== 'needs_user_input') {
      await tell('当前没有等待这项补充的问题。请说明你想修改的内容。'); return { handled: true };
    }
    if (['revise', 'answer', 'update_plan'].includes(intent)) {
      if (intent === 'revise' && (!state.activeCurationRun || ['published', 'cancelled', 'superseded'].includes(state.activeCurationRun.phase))) {
        await tell('当前没有可继续修改的活动草稿。已入库内容不会直接覆盖；请先核对状态，再决定重新准备整理计划或撤回入库。'); return { handled: true };
      }
      if (intent === 'answer' && state.activeCurationRun && state.activeCurationRun.phase !== 'needs_user_input') {
        await tell('当前正文整理没有等待这项补充。请说明是要修改草稿，还是更改入库计划；我不会自动重做。'); return { handled: true };
      }
      if (state.activeCurationRun?.phase === 'running') { await tell('当前资料仍在处理中。请等这轮结束后修改，或先告诉我停止处理。'); return { handled: true }; }
      if (intent === 'update_plan' && state.activeCurationRun && state.activeCurationRun.phase !== 'cancelled') {
        await tell('更换用途、语言或归档目标会改变原来的授权范围。请先停止当前入库流程，再为同一资料准备新计划；已有草稿保留，不会自动重做。'); return { handled: true };
      }
      if (state.workflowV11 && ['revise','answer'].includes(intent) && state.activeCurationRun) {
        state.pendingWorkflowAction=null;await this.save();
        return {intent,operations,answer:text,confirmed:true};
      }
      const action = { intent, operations, original: text, binding: stamp(state), chatId: message.chatId,
        createdAt: this.now(), expiresAt: this.now() + 600, promptId: null };
      state.pendingWorkflowAction = null; await this.save();
      const body = !!state.activeCurationRun && intent !== 'update_plan';
      const sent = await tell(`我理解你希望${intent === 'answer' ? '按这项补充继续' : '修改'}${body ? '当前资料的整理草稿' : '当前资料的入库计划'}。\n你的要求：${text}\n${body ? '确认后会让 Codex 按这个要求继续处理，仍受本份正文授权限制；授权失效时会停下来。修改会使旧入库确认失效。' : '确认后只更新计划信息，不读取正文、不正式入库。'}\n请引用回复这条消息：“确认执行这项操作”或“不执行这项操作”。这不是正文外发或正式入库授权。`);
      if (sent?.messageId && (sent.chunkIds?.length ?? 0) <= 1) {
        action.promptId = sent.messageId; state.pendingWorkflowAction = action; await this.save();
      }
      return { handled: true };
    }
    return { intent, operations };
  }
}
