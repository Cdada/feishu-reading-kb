import test from 'node:test';
import assert from 'node:assert/strict';
import { buildConversionPrompt, buildDuplicateConfirmationPrompt } from '../src/conversion-prompt.mjs';

const attachment = { fileName: 'Lani.pdf', sourceRelpath: '2026-09-08/id/01-Lani.pdf', bytes: 1234 };

test('direct execution uses Chinese general text defaults and exact idempotency key', () => {
  const prompt = buildConversionPrompt({ text: '直接执行', attachments: [attachment], requestId: 'feishu-msg-1', executor: 'codex' });
  assert.match(prompt, /document_language=ch/);
  assert.match(prompt, /document_profile=general_text/);
  assert.match(prompt, /feishu-msg-1/);
  assert.match(prompt, /2026-09-08\/id\/01-Lani\.pdf/);
  assert.doesNotMatch(prompt, /\/Users\//);
});

test('explicit language and scanned profile are captured before conversion', () => {
  const prompt = buildConversionPrompt({ text: '用 DSH 按英文扫描件执行', attachments: [attachment], requestId: 'feishu-msg-2', executor: 'dsh' });
  assert.match(prompt, /document_language=en/);
  assert.match(prompt, /document_profile=scanned_text/);
});

test('duplicate confirmation uses controlled source and atomic tool without an inspection identifier', () => {
  const prompt = buildDuplicateConfirmationPrompt({
    text: '复用已有成果',
    executor: 'codex',
    duplicateAction: 'reuse_existing',
    pending: {
      idempotencyKey: 'feishu-msg-1',
      documentLanguage: 'ch',
      documentProfile: 'general_text',
      attachments: [attachment],
    },
  });
  assert.match(prompt, /confirm_duplicate_submission/);
  assert.match(prompt, /2026-09-08\/id\/01-Lani\.pdf/);
  assert.match(prompt, /"idempotency_key": "feishu-msg-1"/);
  assert.match(prompt, /duplicate_action=reuse_existing/);
  assert.doesNotMatch(prompt, /inspect_[0-9a-f]+/u);
});

test('batch attachments receive distinct stable idempotency keys', () => {
  const prompt = buildConversionPrompt({
    text: '直接执行',
    attachments: [attachment, { ...attachment, fileName: 'B.pdf', sourceRelpath: '2026-09-08/id/02-B.pdf' }],
    requestId: 'feishu-msg-batch',
    executor: 'dsh',
  });
  assert.match(prompt, /feishu-msg-batch-1/);
  assert.match(prompt, /feishu-msg-batch-2/);
});
