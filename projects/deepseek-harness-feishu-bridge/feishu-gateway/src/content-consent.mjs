// Pure policy/signing component, wired behind an explicit gateway feature flag.
// Call only with transport-authenticated messages, never model output, quoted
// content, a document's fields, or user-supplied "sender" JSON.
import { createHmac } from 'node:crypto';
import { humanSize, humanTime } from './user-facing-text.mjs';

const fields = ['kind', 'consent_id', 'consent_hash', 'actor_id', 'chat_id',
  'prompt_message_id', 'message_id', 'issued_at'];
export const consentReplies = Object.freeze({
  '允许本次正文外发': 'allow_cloud_body_once',
  '不允许正文外发': 'deny_cloud_body',
  '撤销本次正文授权': 'revoke_cloud_body',
});
const safeId = (value) => typeof value === 'string' && /^[A-Za-z0-9_.:-]{1,200}$/u.test(value);

function sign(key, data) {
  return { ...data, signature: createHmac('sha256', key)
    .update(JSON.stringify(fields.map((name) => data[name])), 'utf8').digest('hex') };
}

export function createContentConsentPolicy({ signingKey, ownerId, now = () => Math.floor(Date.now() / 1000), replies = consentReplies, presentedKind = 'presented' }) {
  if (!Buffer.isBuffer(signingKey) || signingKey.length < 32 || !safeId(ownerId)) {
    throw new Error('正文授权需要独立网关签名密钥和本人身份');
  }
  const key = Buffer.from(signingKey);
  function base({ consent, chatId, promptMessageId, messageId, kind }) {
    if (![consent?.consent_id, chatId, promptMessageId, messageId].every(safeId)
      || !/^[a-f0-9]{64}$/u.test(consent?.consent_hash ?? '')) throw new Error('正文授权绑定信息无效');
    return { kind, consent_id: consent.consent_id, consent_hash: consent.consent_hash,
      actor_id: ownerId, chat_id: chatId, prompt_message_id: promptMessageId,
      message_id: messageId, issued_at: now() };
  }
  return {
    // Invoke only after channel.send has succeeded and returned this message ID.
    presented({ consent, chatId, promptMessageId }) {
      if (!Number.isSafeInteger(consent?.expires_at) || now() >= consent.expires_at) throw new Error('正文授权清单已过期');
      return sign(key, base({ consent, chatId, promptMessageId, messageId: promptMessageId, kind: presentedKind }));
    },
    // Adapter contract: these fields MUST come from authenticated Feishu events.
    // Reply binding avoids applying "yes" for one document to another document.
    decide({ consent, delivery, message }) {
      if (message?.senderId !== ownerId || message?.chatType !== 'p2p'
        || message?.rawContentType !== 'text' || message?.resourceCount !== 0
        || message?.chatId !== delivery?.chat_id || message?.replyToMessageId !== delivery?.prompt_message_id
        || delivery?.actor_id !== ownerId || delivery?.consent_id !== consent?.consent_id
        || delivery?.consent_hash !== consent?.consent_hash
        || message?.messageId === delivery?.prompt_message_id
        || typeof message?.content !== 'string'
        || !Number.isSafeInteger(message?.createdAtSeconds)
        || message.createdAtSeconds < now() - 300 || message.createdAtSeconds > now() + 30) return null;
      const reply = message.content.trim();
      const kind = Object.hasOwn(replies, reply) ? replies[reply] : null;
      if (!kind) return null; // "好的", "继续", quoted instructions never approve.
      if (kind !== 'revoke_cloud_body' && now() >= consent.expires_at) return null;
      return sign(key, base({ consent, chatId: message.chatId,
        promptMessageId: delivery.prompt_message_id, messageId: message.messageId, kind }));
    },
  };
}

// This string is for a plain-text message, not a Markdown/HTML card body.
export function renderContentConsentText(consent) {
  if (consent?.policy_version !== 'cloud-body-consent/v1'
    || consent.recipient !== 'openai_codex_cloud' || consent.redaction !== 'none'
    || !Array.isArray(consent.files) || !consent.files.length || consent.files.length > 50
    || !Array.isArray(consent.purposes) || !consent.purposes.length
    || consent.purposes.some((x) => !['organize', 'translate', 'brief'].includes(x))
    || !Number.isSafeInteger(consent.expires_at) || !Number.isSafeInteger(consent.total_bytes)
    || consent.access_window_seconds !== 3600 || consent.formal_write !== false
    || consent.files.some((x) => !safeId(x.artifact_id) || !/^[a-f0-9]{64}$/u.test(x.sha256 ?? '')
      || typeof x.relative_path !== 'string' || !Number.isSafeInteger(x.size_bytes) || x.size_bytes < 0)
    || consent.files.reduce((sum, x) => sum + x.size_bytes, 0) !== consent.total_bytes) {
    throw new Error('正文授权清单不完整');
  }
  const label = (value) => JSON.stringify(String(value).replace(/[\u0000-\u001f\u007f-\u009f\u202a-\u202e\u2066-\u2069]/gu, ' '));
  const names = { organize: '内容整理与质量复核', translate: '翻译', brief: '简报草稿（不推送）' };
  const text = [
    '请单独确认这份材料的正文处理范围（不是正式入库确认）',
    `来源：${label(consent.source_filename)}`,
    `用途：${consent.purposes.map((x) => names[x]).join('、')}`,
    `目标语言：${label(consent.target_language ?? '不适用')}`,
    `共 ${consent.files.length} 个文件，${humanSize(consent.total_bytes)}：`,
    ...consent.files.map((x) => `- ${label(x.relative_path)}（${humanSize(x.size_bytes)}）`),
    '所列转换文本的完整正文将发送给 OpenAI Codex 云端处理，未经脱敏；内容可能保留在 Codex 任务记录中。原始二进制文件及未列出的附件不发送。',
    '授权后 1 小时内按分段读取一次。撤销只阻止后续读取，不能收回已经发送的内容。正式入库、删除和简报推送仍需另行确认。',
    '整理结果和待确认预览会回传到本飞书私聊；流程辅助记录与原始二进制文件不发送给模型。',
    `本提示有效至：${humanTime(consent.expires_at)}`,
    '请引用回复本条提示：“允许本次正文外发”或“不允许正文外发”。需要停止后续读取时，引用回复“撤销本次正文授权”。',
  ].join('\n');
  if (text.length > 12000) throw new Error('授权清单过长，必须拆分或完整展示，不能截断后请求确认');
  return text;
}
