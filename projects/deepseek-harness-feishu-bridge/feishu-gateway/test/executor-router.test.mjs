import test from 'node:test';
import assert from 'node:assert/strict';
import {
  isExecutorSelectionOnly,
  isFallbackAcceptance,
  isResetRequest,
  isStatusRequest,
  resolveDuplicateAction,
  resolveExecutor,
  resolveRoutingMode,
} from '../src/executor-router.mjs';

test('natural language selects Codex or DSH', () => {
  assert.deepEqual(resolveExecutor('请用 Codex 处理刚才的文件', 'dsh'), { kind: 'explicit', executor: 'codex' });
  assert.deepEqual(resolveExecutor('还是交给 DSH 执行', 'codex'), { kind: 'explicit', executor: 'dsh' });
  assert.deepEqual(resolveExecutor('直接执行', 'codex'), { kind: 'default', executor: 'codex' });
  assert.deepEqual(resolveExecutor('/deepseek 按英文执行', 'codex'), { kind: 'explicit', executor: 'dsh' });
  assert.deepEqual(resolveExecutor('用codex按英文执行', 'dsh'), { kind: 'explicit', executor: 'codex' });
});

test('natural language switches between testing and operational modes', () => {
  assert.equal(resolveRoutingMode('进入测试模式'), 'testing');
  assert.equal(resolveRoutingMode('切换到正式运行模式'), 'operational');
  assert.equal(resolveRoutingMode('用 Codex 处理'), null);
});

test('ambiguous executor selection requires clarification', () => {
  assert.deepEqual(resolveExecutor('比较 DSH 和 Codex 后处理', 'dsh'), { kind: 'ambiguous', executor: null });
});

test('control phrases remain natural', () => {
  assert.equal(isFallbackAcceptance('好的，改用 Codex 处理'), true);
  assert.equal(isResetRequest('新建会话'), true);
  assert.equal(isStatusRequest('查看状态'), true);
  assert.equal(isExecutorSelectionOnly('默认改用 Codex'), true);
  assert.equal(isExecutorSelectionOnly('用 Codex 处理这份文件'), false);
});

test('duplicate decisions use narrow natural-language choices', () => {
  assert.equal(resolveDuplicateAction('复用已有成果'), 'reuse_existing');
  assert.equal(resolveDuplicateAction('请作为新版本转换'), 'convert_as_new_version');
  assert.equal(resolveDuplicateAction('取消本次转换'), 'cancel');
  assert.equal(resolveDuplicateAction('帮我看看该怎么办'), null);
});
