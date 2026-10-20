import test from 'node:test';
import assert from 'node:assert/strict';
import {
  buildCurationFollowUpPrompt,
  buildCurationPrompt,
  isCurationCancel,
  resolveCurationIntent,
} from '../src/curation-intent.mjs';

test('natural language resolves knowledge-base postprocessing operations', () => {
  assert.deepEqual(resolveCurationIntent('把刚才的资料整理入库'), {
    operations: ['archive'], sourceTaskId: null, archiveTargetKind: 'undecided',
  });
  assert.deepEqual(resolveCurationIntent('翻译成中文并生成重点简报'), {
    operations: ['translate', 'brief'], sourceTaskId: null, archiveTargetKind: 'undecided',
  });
  assert.deepEqual(resolveCurationIntent('归档到个人知识库'), {
    operations: ['archive'], sourceTaskId: null, archiveTargetKind: 'personal_reading',
  });
});

test('curation can name a conversion task and ordinary chat is ignored', () => {
  const taskId = `task_${'a'.repeat(32)}`;
  assert.equal(resolveCurationIntent(`请把 ${taskId} 入库`)?.sourceTaskId, taskId);
  assert.equal(resolveCurationIntent('这份文件按英文扫描件处理'), null);
});

test('curation prompt keeps cloud use unauthorized', () => {
  const prompt = buildCurationPrompt({
    text: '翻译成中文', requestId: 'feishu-message-1', sourceTaskId: `task_${'a'.repeat(32)}`,
    intent: { operations: ['translate'], archiveTargetKind: 'undecided' },
  });
  assert.match(prompt, /cloud_policy 必须使用 not_authorized/u);
  assert.match(prompt, /只允许调用 kb_curation MCP/u);
});

test('curation follow-up remains in the same Codex plan', () => {
  const prompt = buildCurationFollowUpPrompt({
    text: '个人知识库',
    requestId: 'feishu-follow-up-1',
    pending: {
      planId: `plan_${'a'.repeat(32)}`,
      sourceTaskId: `task_${'b'.repeat(32)}`,
      requiredConfirmations: ['archive_target_confirmation'],
    },
  });
  assert.match(prompt, /set_archive_target/u);
  assert.match(prompt, /不得把这条补充回答交给 DeepSeek Harness/u);
  assert.equal(isCurationCancel('取消知识库整理'), true);
  for (const text of ['不要取消知识库整理', '原文说停止入库流程', '取消入库流程是什么意思？', '请不要停止后处理']) {
    assert.equal(isCurationCancel(text), false);
  }
});
