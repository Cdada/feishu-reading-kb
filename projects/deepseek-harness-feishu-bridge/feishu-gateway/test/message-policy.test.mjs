import test from 'node:test';
import assert from 'node:assert/strict';
import { attachmentAction, hasExplicitInstruction, isConversionParameterReply } from '../src/message-policy.mjs';

test('file and image placeholders are not treated as user instructions', () => {
  assert.equal(hasExplicitInstruction({ rawContentType: 'file', content: '<file key="abc" name="a.pdf"/>' }), false);
  assert.equal(hasExplicitInstruction({ rawContentType: 'image', content: '![image](abc)' }), false);
  assert.equal(hasExplicitInstruction({ rawContentType: 'text', content: '用 Codex 处理' }), true);
});

test('attachment-only behavior follows routing mode', () => {
  assert.equal(attachmentAction({ routingMode: 'testing', explicitInstruction: false }), 'stage-for-selection');
  assert.equal(attachmentAction({ routingMode: 'operational', explicitInstruction: false }), 'stage-for-parameters');
  assert.equal(attachmentAction({ routingMode: 'operational', explicitInstruction: true }), 'follow-instruction');
});

test('operational preflight accepts defaults or explicit recognition parameters', () => {
  assert.equal(isConversionParameterReply('直接执行'), true);
  assert.equal(isConversionParameterReply('按默认参数处理'), true);
  assert.equal(isConversionParameterReply('这是一份英文扫描件'), true);
  assert.equal(isConversionParameterReply('日文电子文本'), true);
  assert.equal(isConversionParameterReply('用 DSH 处理'), true);
  assert.equal(isConversionParameterReply('帮我看看这份文件'), false);
});
