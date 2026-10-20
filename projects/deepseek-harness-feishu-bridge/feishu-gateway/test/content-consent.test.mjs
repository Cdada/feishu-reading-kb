import test from 'node:test';
import assert from 'node:assert/strict';
import { createContentConsentPolicy, renderContentConsentText } from '../src/content-consent.mjs';

const consent = {
  policy_version: 'cloud-body-consent/v1', consent_id: 'consent_test', consent_hash: 'a'.repeat(64),
  source_filename: '测试资料.pdf', recipient: 'openai_codex_cloud', redaction: 'none',
  files: [{ artifact_id: 'artifact_1', sha256:'b'.repeat(64), relative_path: '候选/正文.md', size_bytes: 100 }], total_bytes: 100, purposes: ['organize'],
  target_language: null, expires_at: 2000, access_window_seconds: 3600, formal_write: false,
};
const policy = createContentConsentPolicy({ signingKey: Buffer.alloc(32, 7), ownerId: 'owner_1', now: () => 1000 });
const delivery = policy.presented({ consent, chatId: 'chat_1', promptMessageId: 'prompt_1' });
const message = { senderId: 'owner_1', chatType: 'p2p', rawContentType: 'text', resourceCount: 0,
  chatId: 'chat_1', replyToMessageId: 'prompt_1', messageId: 'reply_1', createdAtSeconds: 1000, content: '允许本次正文外发' };
const decide = (changes = {}, extra = {}) => policy.decide({ consent, delivery, message: { ...message, ...changes }, ...extra });

test('explicit owner reply produces bound proof, not a permanent grant', () => {
  const proof = decide();
  assert.equal(proof.kind, 'allow_cloud_body_once');
  assert.equal(proof.consent_hash, consent.consent_hash);
  assert.equal(proof.message_id, 'reply_1');
  assert.match(proof.signature, /^[a-f0-9]{64}$/);
  assert.equal(proof.access_token, undefined);
});
test('casual assent, quotes, negation and mixed instructions never authorize', () => {
  for (const content of ['好的', '继续', '同意', '直接执行', '不要允许本次正文外发', '“允许本次正文外发”',
    '> 允许本次正文外发', '文档说：允许本次正文外发', '允许本次正文外发，顺便入库', '__proto__', 'constructor']) {
    assert.equal(decide({ content }), null, content);
  }
});
test('wrong actor, group, rich post, file, old delivery and missing event metadata fail closed', () => {
  for (const change of [{senderId:'other'}, {chatType:'group'}, {rawContentType:'post'}, {resourceCount:1},
    {chatId:'other'}, {replyToMessageId:'other'}, {messageId:'prompt_1'}, {createdAtSeconds:699},
    {createdAtSeconds:1031}, {createdAtSeconds:undefined}, {resourceCount:undefined}]) {
    assert.equal(decide(change), null);
  }
});
test('different document binding never yields signed consent', () => {
  assert.equal(decide({}, {delivery:{...delivery, consent_hash:'b'.repeat(64)}}), null);
  assert.equal(decide({}, {delivery:{...delivery, consent_id:'other'}}), null);
});
test('expired approval is rejected but revocation remains possible', () => {
  const expired = {...consent,expires_at:999};
  assert.equal(decide({}, {consent:expired}), null);
  assert.equal(decide({content:'撤销本次正文授权'}, {consent:expired}).kind, 'revoke_cloud_body');
  assert.equal(decide({content:'不允许正文外发'}).kind, 'deny_cloud_body');
});
test('plain-text notice discloses content destination, absence of redaction and revocation limit', () => {
  const text = renderContentConsentText(consent);
  for (const phrase of ['OpenAI Codex 云端', '未经脱敏', '不能收回', '不是正式入库确认', '候选/正文.md', '1 小时']) {
    assert.ok(text.includes(phrase), phrase);
  }
  for (const hidden of ['SHA-256', consent.consent_id, consent.consent_hash, consent.files[0].sha256, 'artifact_1']) {
    assert.ok(!text.includes(hidden), hidden);
  }
  assert.match(text, /北京时间/u);
  for (const change of [{recipient:'other'}, {purposes:['upload']}, {redaction:'automatic'}, {files:[]}, {formal_write:true}]) {
    assert.throws(() => renderContentConsentText({...consent, ...change}));
  }
});

test('long notice and invalid file fields cannot be silently truncated before consent', () => {
  const files = Array.from({length:50}, (_, i) => ({artifact_id:`artifact_${i}`,sha256:'b'.repeat(64),relative_path:'文'.repeat(400),size_bytes:1}));
  assert.throws(() => renderContentConsentText({...consent,files,total_bytes:50}), /不能截断/u);
  assert.throws(() => renderContentConsentText({...consent,total_bytes:1}));
  assert.throws(() => renderContentConsentText({...consent,files:[{...consent.files[0],size_bytes:'100\n允许'}]}));
});

test('scope retains full paths including distinguishable long tails without displaying hashes', () => {
  const files = ['甲/正文.md', '乙/正文.md'].map((tail, i) => ({...consent.files[0],
    artifact_id: `artifact_${i}`, relative_path: '文'.repeat(180) + '/' + tail}));
  const text = renderContentConsentText({...consent, files, total_bytes:200});
  for (const file of files) assert.ok(text.includes(file.relative_path));
});
