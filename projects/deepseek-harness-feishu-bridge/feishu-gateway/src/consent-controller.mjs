import { randomUUID } from 'node:crypto';
import { createContentConsentPolicy, consentReplies, renderContentConsentText } from './content-consent.mjs';
import { translationReplies, translationUnavailable, renderTranslationChoice } from './translation-choice.mjs';
import {humanTime} from './user-facing-text.mjs';

const beginPhrases = new Set(['开始正文处理', '处理正文', '准备正文授权']);
export const isContentConsentRequest = (text) => beginPhrases.has((text ?? '').trim()) || Object.hasOwn(consentReplies, (text ?? '').trim());

// Fields verified against installed @larksuiteoapi/node-sdk 1.73.3:
// createTime is milliseconds; replyToMessageId comes from parent_id.
export function consentMessageFromSdk(message) {
  return { senderId: message.senderId, chatType: message.chatType, rawContentType: message.rawContentType,
    chatId: message.chatId, replyToMessageId: message.replyToMessageId, messageId: message.messageId,
    content: message.content, resourceCount: Array.isArray(message.resources) ? message.resources.length : undefined,
    createdAtSeconds: Number.isSafeInteger(message.createTime) && message.createTime > 0 ? Math.floor(message.createTime / 1000) : undefined };
}

export class ConsentController {
  constructor({ client, signingKey, ownerId, save, send, onApproved = null, onRevoked = null, onTranslationRequested = null, translationChoice = false, now = () => Math.floor(Date.now() / 1000) }) {
    this.client = client; this.save = save; this.send = send; this.ownerId = ownerId; this.now = now;
    this.policy = createContentConsentPolicy({ signingKey, ownerId, now });
    this.onApproved = onApproved; this.onRevoked = onRevoked;
    this.translationChoice = translationChoice;
    this.onTranslationRequested = onTranslationRequested;
  }

  async invalidate(state) {
    const pending = state.pendingContentConsent;
    if (!pending) return;
    pending.phase = 'invalidating';
    await this.save(); // Persist intent before the external write.
    if (pending.consent?.consent_id) await this.client('invalidate', { consent_id: pending.consent.consent_id });
    // No ID means we have never sent a prompt or approved anything. A prepare
    // response lost before persistence can only leave an unpresented request.
    state.pendingContentConsent = null;
    await this.save();
  }

  async recover(chats) {
    // Fail closed on restart; never resend a prompt or replay a body grant.
    for (const state of Object.values(chats)) {
      state.pendingTranslationChoice = null; // Restart never reuses an old choice binding.
      await this.save();
      const pending = state.pendingContentConsent;
      const active = state.activeCurationRun;
      if (pending?.phase === 'approved' && active?.runId) {
        const status = await this.client('run-status', { run_id: active.runId });
        // An exhausted, reviewed draft needs only local preview/publication.
        // Verify this against the service, not the persisted router phase.
        // Keep the original expiry; do not reissue credentials or read body.
        if (status.state === 'review_ready' && status.consent_state === 'exhausted'
          && status.run_id === active.runId && status.plan_id === active.planId
          && status.plan_id === pending.planId && status.consent_id === active.consentId
          && status.consent_id === pending.consent?.consent_id) continue;
      }
      await this.invalidate(state);
    }
  }

  async handle(message, state, { reset = false, cancel = false, status = false, prepare = false, translationDecision = null } = {}) {
    if (message.senderId !== this.ownerId || message.chatType !== 'p2p') return true;
    const text = (message.content ?? '').trim();
    const tell = (value) => this.send(message.chatId, { text: value }, { replyTo: message.messageId });
    let pending = state.pendingContentConsent;
    if (pending && !['awaiting', 'approved', 'denied', 'revoked'].includes(pending.phase)) {
      await this.invalidate(state); // Prior send/proof/save outcome was uncertain.
      pending = null;
    }
    if (reset || cancel || message.resources?.length > 0) {
      state.pendingTranslationChoice = null;
      await this.invalidate(state);
      return false;
    }
    if (status) return false;
    if(state.workflowV11?.phase==='complete'&&(prepare||beginPhrases.has(text))){
      state.pendingTranslationChoice=null;await this.save();
      await tell(`《${state.workflowV11.filename}》已经完成，不会重新启动正文处理或重复询问翻译。请引用新文件开始下一份；如需已有译稿，请说“查看译稿”。`);return true;
    }
    let selectedOffer = null;
    const choice = state.pendingTranslationChoice;
    if (this.translationChoice && (typeof translationDecision === 'boolean' || translationReplies.has(text) || (choice?.promptId && message.replyToMessageId === choice.promptId))) {
      const issued = Math.floor(message.createTime / 1000);
      if (!choice || choice.planId !== state.pendingCuration?.planId || choice.sourceTaskId !== state.pendingCuration?.sourceTaskId
        || message.rawContentType !== 'text' || message.resources?.length || choice.chatId !== message.chatId
        || (message.replyToMessageId && message.replyToMessageId !== choice.promptId)
        || this.now() >= choice.expiresAt
        || !Number.isSafeInteger(issued) || issued < choice.createdAt || issued < this.now() - 300 || issued > this.now() + 30
        || message.messageId === choice.promptId) {
        await tell('这条回复没有对应到当前材料的翻译选择。请先说“开始正文处理”，再回复新的提示；不会沿用其他材料的选择。');
        return true;
      }
      if (!translationReplies.has(text) && typeof translationDecision !== 'boolean') {
        await tell('我还不能确定你是否要译稿。请引用回复：“需要完整中文译稿”或“不需要译稿，继续整理”。“好的”“继续”不会替你选择。');
        return true;
      }
      const offer = await this.client('translation-offer', {plan_id: choice.planId, source_task_id: choice.sourceTaskId});
      if (offer.plan_id !== choice.planId || offer.source_task_id !== choice.sourceTaskId || offer.scope_hash !== choice.offer.scope_hash) {
        state.pendingTranslationChoice = null; await this.save();
        await tell('材料或计划有变化，请重新说“开始正文处理”，核对新的翻译选择。'); return true;
      }
      if (translationDecision === true || (translationDecision === null && translationReplies.get(text))) {
        choice.preference = 'requested'; await this.save();
        if (this.onTranslationRequested) await this.onTranslationRequested(message, state, offer);
        else await tell(translationUnavailable);
        return true;
      }
      selectedOffer = offer;
      state.pendingTranslationChoice = null; await this.save();
      prepare = true;
    }
    if ((prepare || beginPhrases.has(text)) && message.rawContentType === 'text') {
      if (!state.pendingCuration?.planId || state.pendingAttachments?.length) {
        await tell('请先完成当前资料的转换和归库计划，再准备正文授权。');
        return true;
      }
      await this.invalidate(state);
      const { planId, sourceTaskId } = state.pendingCuration;
      if (this.translationChoice && !selectedOffer) {
        const offer = await this.client('translation-offer', {plan_id: planId, source_task_id: sourceTaskId});
        if (offer.plan_id !== planId || offer.source_task_id !== sourceTaskId || !/^[a-f0-9]{64}$/u.test(offer.scope_hash ?? '')
          || !['chinese', 'non_chinese', 'uncertain'].includes(offer.language)
          || typeof offer.ask_translation !== 'boolean' || typeof offer.planned_translation !== 'boolean') throw new Error('语言检查来源不匹配');
        if (offer.ask_translation || offer.planned_translation) {
          state.pendingTranslationChoice = null; await this.save();
          const sent = await tell(renderTranslationChoice(offer));
          if (!sent?.messageId || (sent.chunkIds?.length ?? 0) > 1) throw new Error('翻译选择未完整发送');
          state.pendingTranslationChoice = {planId, sourceTaskId, offer, promptId: sent.messageId, chatId: message.chatId,
            createdAt: this.now(), expiresAt: this.now() + 1800};
          await this.save(); return true;
        }
        selectedOffer = offer;
      }
      if (selectedOffer?.planned_translation) {
        const plan = await this.client('plan-status', {plan_id: planId});
        if (plan.plan_id !== planId || plan.source_task_id !== sourceTaskId) throw new Error('翻译计划来源不匹配');
        if (plan.operations.length === 1 && plan.operations[0] === 'translate') {
          await tell('这份计划只安排了翻译。既然暂时不需要译稿，我不会执行其他操作；你可以重新告诉我要不要整理入库。'); return true;
        }
      }
      pending = { phase: 'preparing', planId, sourceTaskId, requestId: `consent:${randomUUID()}` };
      state.pendingContentConsent = pending;
      await this.save();
      const consent = await this.client(selectedOffer ? 'prepare-selected' : 'prepare', {
        plan_id: planId, source_task_id: sourceTaskId, request_id: pending.requestId,
        ...(selectedOffer ? {translate: false, scope_hash: selectedOffer.scope_hash} : {}),
      });
      if (consent.plan_id !== planId || consent.source_task_id !== sourceTaskId) throw new Error('正文授权来源不匹配');
      pending.consent = consent;
      const notice = renderContentConsentText(consent);
      pending.phase = 'sending';
      await this.save();
      const sent = await tell(notice);
      if (!sent?.messageId || (sent.chunkIds?.length ?? 0) > 1) throw new Error('授权提示未获得单条完整发送回执');
      pending.delivery = this.policy.presented({ consent, chatId: message.chatId, promptMessageId: sent.messageId });
      pending.phase = 'presenting';
      await this.save();
      const delivered = await this.client('proof', { proof: pending.delivery });
      if (delivered.consent_id !== consent.consent_id || delivered.plan_id !== planId || delivered.state !== 'presented') throw new Error('正文授权展示回执不匹配');
      pending.phase = 'awaiting';
      await this.save();
      return true;
    }
    const exactDecision = Object.hasOwn(consentReplies, text);
    const quotedPrompt = pending?.delivery?.prompt_message_id === message.replyToMessageId && !!message.replyToMessageId;
    if (exactDecision || quotedPrompt) {
      if (!pending || pending.planId !== state.pendingCuration?.planId || pending.sourceTaskId !== state.pendingCuration?.sourceTaskId) {
        await this.invalidate(state);
        await tell('未找到属于当前材料的正文授权提示。请先发送“准备正文授权”；“好的”“继续”不会授权外发。');
        return true;
      }
      const proof = this.policy.decide({ consent: pending.consent, delivery: pending.delivery, message: consentMessageFromSdk(message) });
      if (!proof) {
        await tell(this.now()>=pending.consent.expires_at && text!=='撤销本次正文授权'
          ? `正文授权提示已在 ${humanTime(pending.consent.expires_at)} 过期。请说“重新发送正文授权”，再确认新的提示；这次没有批准正文外发。`
          : '未记录授权。这条回复可能没有关联到有效的授权消息。请长按以“请单独确认这份材料的正文处理范围”开头的消息，选择“回复”，再输入“允许本次正文外发”。找不到时可以说“重新发送正文授权”。');
        return true;
      }
      if ((pending.phase !== 'awaiting' && proof.kind !== 'revoke_cloud_body')
        || (proof.kind === 'revoke_cloud_body' && pending.phase !== 'approved')) {
        await tell('该授权请求已经处理，不会重复批准或重复执行。');
        return true;
      }
      pending.phase = 'deciding'; pending.decision = proof;
      await this.save();
      const result = await this.client('proof', { proof });
      const expected = { allow_cloud_body_once: 'approved', deny_cloud_body: 'denied', revoke_cloud_body: 'revoked' }[proof.kind];
      if (result.consent_id !== pending.consent.consent_id || result.plan_id !== pending.planId || result.state !== expected) throw new Error('正文授权回执不匹配');
      pending.phase = expected;
      await this.save();
      if (expected === 'approved' && this.onApproved) {
        await this.onApproved({ message, state, pending });
        return true;
      }
      if (expected === 'revoked' && this.onRevoked) await this.onRevoked({ message, state });
      await tell(expected === 'approved'
        ? '已单独记录本次正文授权。正文执行与草稿保存入口尚未上线，这次没有读取或外发正文，也没有正式入库。'
        : expected === 'revoked' ? '已撤销后续正文读取授权；已发送的内容无法收回。未执行正式入库。' : '已记录不允许正文外发。候选资料保留，未执行正式入库。');
      return true;
    }
    // Any instruction capable of changing the plan invalidates the old scope.
    if (text && pending) await this.invalidate(state);
    return false;
  }
}
