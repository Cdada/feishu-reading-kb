import test from 'node:test';
import assert from 'node:assert/strict';
import { createHmac } from 'node:crypto';
import { CurationFlow, buildBodyPrompt } from '../src/curation-flow.mjs';

const now = 1788965000;
const key = Buffer.from('synthetic-only-approval-key-32-bytes');
const run = { run_id: `run_${'a'.repeat(32)}`, plan_id: 'plan_1', consent_id: 'consent_1', run_token: 'b'.repeat(64), purposes: ['organize'], target_language: null };
const preview = { kind: 'publish', write_id: `write_${'c'.repeat(32)}`, payload_hash: 'd'.repeat(64), publication_id: `pub_${'c'.repeat(32)}`,
  run_id: run.run_id, plan_id: run.plan_id, title: '合成资料', root_identity: { path: '/synthetic/kb' }, relative_directory: 'source-group',
  files: { '来源卡片.md': { sha256: 'e'.repeat(64) } }, source_sha256: 'f'.repeat(64), expires_at: now + 1800,
  source_completeness: 'full', validation_status: 'human_reviewed_structure', quality_warnings: {}, preview_markdown: '# 合成测试预览',
  preview_pdf_base64: Buffer.from('%PDF-1.4\nsynthetic format fixture\n%%EOF').toString('base64') };
const message = (content, extra = {}) => ({ senderId: 'owner_1', chatType: 'p2p', chatId: 'chat_1', messageId: 'message_1',
  rawContentType: 'text', createTime: now * 1000, resources: [], content, ...extra });

function fixture({ dispatch = async () => {}, status = 'review_ready' } = {}) {
  const calls = [], sends = [], snapshots = [];
  const state = { pendingCuration: { planId: run.plan_id, sourceTaskId: 'task_1' } };
  const flow = new CurationFlow({ signingKey: key, ownerId: 'owner_1', now: () => now,
    save: async () => snapshots.push(JSON.stringify(state)),
    send: async (...args) => { sends.push(args); return { messageId: `sent_${sends.length}` }; },
    sendPreview: async () => ({ messageId: 'preview_file_1' }), dispatch,
    stop: async () => calls.push(['stop']),
    client: async (op, data) => {
      calls.push([op, data]);
      if (op === 'start' || op === 'resume') return run;
      if (op === 'run-status') return { run_id: run.run_id, plan_id: run.plan_id, state: status, completed_chunks: 1, questions: ['请说明来源。'] };
      if (op === 'final-preview') return preview;
      if (op === 'rollback-preview') return { ...preview, kind: 'rollback', source: 'source-group', destination: 'archive/source-group' };
      if (op === 'write-proof') {
        if (data.proof.kind.endsWith('_presented')) return { write_id: preview.write_id, state: 'presented' };
        if (data.proof.kind.startsWith('deny_')) return { state: 'denied' };
        return { state: data.proof.kind === 'approve_write' ? 'committed' : 'withdrawn', publication_id: preview.publication_id, root: '/synthetic/kb', relative_directory: 'source-group', file_count: 4 };
      }
      return {};
    },
  });
  const start = () => flow.approved({ message: message('允许本次正文外发'), state,
    pending: { planId: run.plan_id, sourceTaskId: 'task_1', consent: { consent_id: run.consent_id } } });
  return { flow, state, start, calls, sends, snapshots };
}

test('approved body produces full preview but never automatically publishes', async () => {
  const f = fixture(); await f.start(); await Promise.all([...f.flow.jobs]);
  assert.equal(f.state.pendingPublication.phase, 'awaiting');
  assert.equal(f.calls.filter(([op, data]) => op === 'write-proof' && data.proof.kind === 'approve_write').length, 0);
  assert.ok(f.snapshots.every((snapshot) => !snapshot.includes(run.run_token) && !snapshot.includes(preview.preview_markdown)));
  assert.ok(f.snapshots.every((snapshot) => !snapshot.includes(preview.preview_pdf_base64)));
  const notice = f.sends.at(-1)[1].text;
  for (const hidden of [preview.plan_id, preview.payload_hash, preview.source_sha256, preview.publication_id, 'preview_file_1', 'SHA-256', 'human_reviewed', '{}']) {
    assert.ok(!notice.includes(hidden), hidden);
  }
  for (const visible of ['合成资料', '完整材料', '数量未提供', '确认本次入库', '北京时间']) assert.ok(notice.includes(visible), visible);
  assert.equal(f.state.pendingPublication.preview.source_sha256, preview.source_sha256);
  assert.equal(f.state.pendingPublication.delivery.consent_hash, preview.payload_hash);
});

test('only explicit owner reply to final preview commits and carries signed binding', async () => {
  const f = fixture(); await f.start(); await Promise.all([...f.flow.jobs]);
  const parent = f.state.pendingPublication.delivery.prompt_message_id;
  await f.flow.handle(message('好的', { replyToMessageId: parent }), f.state);
  await f.flow.handle(message('确认本次入库', { replyToMessageId: 'other' }), f.state);
  await f.flow.handle(message('确认本次入库', { replyToMessageId: parent, senderId: 'other' }), f.state);
  assert.equal(f.state.lastPublication, undefined);
  await f.flow.handle(message('确认本次入库', { replyToMessageId: parent }), f.state);
  assert.equal(f.state.lastPublication.state, 'committed');
  assert.ok(!f.sends.at(-1)[1].text.includes(preview.publication_id));
  const proof = f.calls.find(([op, data]) => op === 'write-proof' && data.proof.kind === 'approve_write')[1].proof;
  const fields = ['kind', 'consent_id', 'consent_hash', 'actor_id', 'chat_id', 'prompt_message_id', 'message_id', 'issued_at'];
  assert.equal(proof.signature, createHmac('sha256', key).update(JSON.stringify(fields.map((name) => proof[name]))).digest('hex'));
  await f.flow.handle(message('确认本次入库', { replyToMessageId: parent }), f.state);
  assert.equal(f.calls.filter(([op, data]) => op === 'write-proof' && data.proof.kind === 'approve_write').length, 1);
});
test('expired final confirmation explains expiry and never publishes',async()=>{
  const f=fixture();await f.start();await Promise.all([...f.flow.jobs]);
  f.state.pendingPublication.preview={...f.state.pendingPublication.preview,expires_at:now-1};
  await f.flow.handle(message('确认本次入库',{replyToMessageId:f.state.pendingPublication.delivery.prompt_message_id}),f.state);
  assert.match(f.sends.at(-1)[1].text,/过期/);assert.match(f.sends.at(-1)[1].text,/查看最终入库预览/);
  assert.ok(!f.calls.some(([op,d])=>op==='write-proof'&&d.proof.kind==='approve_write'));
});
test('body dispatch timeout is reported as timeout',async()=>{
  const f=fixture({dispatch:async()=>{throw Error('Codex task timeout');}});await f.start();await Promise.all([...f.flow.jobs]);
  assert.match(f.sends.at(-1)[1].text,/正文整理调用超时/);assert.equal(f.state.activeCurationRun.phase,'needs_attention');
});

test('background dispatch does not block cancellation or publish stale result', async () => {
  let finish;
  const f = fixture({ dispatch: () => new Promise((resolve) => { finish = resolve; }) });
  await f.start();
  assert.equal(f.state.activeCurationRun.phase, 'running');
  await f.flow.handle(message('取消'), f.state);
  finish(); await Promise.all([...f.flow.jobs]);
  assert.equal(f.state.activeCurationRun.phase, 'cancelled');
  assert.ok(f.calls.some(([op]) => op === 'invalidate'));
  assert.ok(f.calls.some(([op]) => op === 'stop'));
  assert.ok(!f.calls.some(([op]) => op === 'final-preview'));
});

test('new attachment invalidates final approval before normal routing', async () => {
  const f = fixture(); await f.start(); await Promise.all([...f.flow.jobs]);
  assert.equal(await f.flow.handle(message('', { resources: [{}] }), f.state), false);
  assert.equal(f.state.pendingPublication, null);
  assert.equal(f.state.activeCurationRun, null);
  assert.ok(f.calls.some(([op]) => op === 'invalidate-write'));
});

test('questions keep draft pending and never prepare formal preview', async () => {
  const f = fixture({ status: 'needs_user_input' }); await f.start(); await Promise.all([...f.flow.jobs]);
  assert.equal(f.state.activeCurationRun.phase, 'needs_user_input');
  assert.ok(!f.calls.some(([op]) => op === 'final-preview'));
  assert.ok(f.sends.some(([, value]) => value.text.includes('请说明来源')));
});

test('failed preview file delivery cannot produce an approval prompt', async () => {
  const f = fixture(); f.flow.sendPreview = async () => { throw new Error('failed'); };
  await f.start(); await Promise.all([...f.flow.jobs]);
  assert.ok(!f.calls.some(([op]) => op === 'write-proof'));
  assert.equal(f.state.pendingPublication.phase, 'sending');
});

test('missing readable PDF cannot approve through a legacy Markdown response', async () => {
  const f = fixture();
  const client = f.flow.client;
  f.flow.client = async (op, data) => op === 'final-preview' ? {...preview, preview_pdf_base64:undefined} : client(op, data);
  await f.start(); await Promise.all([...f.flow.jobs]);
  assert.ok(!f.calls.some(([op]) => op === 'write-proof'));
  assert.equal(f.state.activeCurationRun.phase, 'needs_attention');
});

test('body prompt confines evidence, tools and write authority', () => {
  const value = buildBodyPrompt(run, '补充合成资料来源');
  assert.ok(value.includes('read_next_curation_chunk'));
  assert.ok(value.includes('不得使用历史材料'));
  assert.ok(value.includes('不批准写入'));
});

test('withdrawal uses separate final confirmation', async () => {
  const f = fixture(); f.state.lastPublication = { publication_id: preview.publication_id };
  await f.flow.handle(message('撤回本次入库'), f.state);
  const parent = f.state.pendingPublication.delivery.prompt_message_id;
  await f.flow.handle(message('确认本次入库', { replyToMessageId: parent }), f.state);
  assert.equal(f.state.pendingPublication.phase, 'awaiting');
  await f.flow.handle(message('确认撤回本次入库', { replyToMessageId: parent }), f.state);
  assert.equal(f.state.lastPublication.state, 'withdrawn');
});

test('restart invalidates old write prompt but keeps verified ready draft without dispatch', async () => {
  const f=fixture();
  f.state.activeCurationRun={runId:run.run_id,planId:run.plan_id,consentId:run.consent_id,phase:'review_ready'};
  f.state.pendingPublication={phase:'awaiting',preview};
  const original=f.flow.client;
  f.flow.client=async(op,data)=>op==='run-status' ? {run_id:run.run_id,plan_id:run.plan_id,consent_id:run.consent_id,consent_state:'exhausted',state:'review_ready'} : original(op,data);
  f.flow.dispatch=async()=>assert.fail('restart must not call model');
  await f.flow.recover({chat:f.state});
  assert.equal(f.state.activeCurationRun.phase,'review_ready');
  assert.equal(f.state.pendingPublication,null);
  assert.ok(f.calls.some(([op])=>op==='invalidate-write'));
  assert.equal(f.sends.length,0);
});

test('natural preview requests never resume an expired body grant, even replying to approval prompt', async () => {
  for (const text of ['请再帮我提供一下这份文件的入库预览', '请重新生成预览', '我想看看整理结果']) {
    const f=fixture(); await f.start(); await Promise.all([...f.flow.jobs]);
    const parent=f.state.pendingPublication.delivery.prompt_message_id;
    f.calls.length=0;
    const client=f.flow.client;
    f.flow.client=async(op,data)=> { assert.notEqual(op,'resume'); return client(op,data); };
    await f.flow.handle(message(text,{replyToMessageId:parent}),f.state);
    assert.ok(f.calls.some(([op])=>op==='final-preview'));
    assert.equal(f.state.pendingPublication.phase,'awaiting');
    assert.ok(!f.calls.some(([op,data])=>op==='write-proof' && data.proof.kind==='approve_write'));
  }
});

test('natural status and unclear requests preserve current approval without cloud work', async () => {
  const f=fixture(); await f.start(); await Promise.all([...f.flow.jobs]);
  const pending=f.state.pendingPublication;
  f.calls.length=0;
  await f.flow.handle(message('状态查看'),f.state);
  await f.flow.handle(message('这是什么意思？'),f.state);
  assert.equal(f.state.pendingPublication,pending);
  assert.ok(!f.calls.some(([op])=>['resume','invalidate-write','final-preview'].includes(op)));
  assert.match(f.sends.at(-1)[1].text,/不确定/u);
});

test('expired explicit revision explains boundary and does not invalidate a readable preview', async () => {
  const f=fixture(); await f.start(); await Promise.all([...f.flow.jobs]);
  const pending=f.state.pendingPublication;
  const client=f.flow.client;
  f.flow.client=async(op,data)=>{ if(op==='resume') throw Error('正文授权控制请求未完成（RUN_NOT_AUTHORIZED）'); return client(op,data); };
  await f.flow.handle(message('确认执行这项操作'),f.state, { workflow: { intent: 'revise', confirmed: true, answer: '请修改标题' } });
  assert.equal(f.state.pendingPublication,pending);
  assert.match(f.sends.at(-1)[1].text,/已有草稿保留/u);
});

test('revision or source answer without workflow confirmation never resumes body', async () => {
  const f = fixture(); await f.start(); await Promise.all([...f.flow.jobs]);
  for (const phase of ['review_ready', 'needs_user_input']) {
    f.state.activeCurationRun.phase = phase;
    await f.flow.handle(message('这是完整的访谈，请重新整理'), f.state);
    await f.flow.handle(message('修改第三点'), f.state, { workflow: { intent: 'revise' } });
  }
  assert.ok(!f.calls.some(([op]) => op === 'resume'));
});

test('confirmed workflow passes original change request, not confirmation words', async () => {
  let request;
  const f = fixture({ dispatch: async x => { request = x; } }); await f.start(); await Promise.all([...f.flow.jobs]);
  await f.flow.handle(message('确认执行这项操作'), f.state, { workflow: { intent: 'revise', confirmed: true, answer: '按精读规则补充前提和例外' } });
  await Promise.all([...f.flow.jobs]);
  assert.ok(request.prompt.includes('按精读规则补充前提和例外'));
  assert.ok(!request.prompt.includes('用户补充（不扩张正文授权范围，不批准写入）：确认执行'));
});

test('old summary can never produce a new publish approval', async () => {
  const f = fixture(); f.state.activeCurationRun = { runId: run.run_id, planId: run.plan_id, phase: 'review_ready' };
  const client = f.flow.client;
  f.flow.client = async (op, data) => op === 'run-status' ? { run_id: run.run_id, plan_id: run.plan_id, state: 'review_ready', quality_upgrade_required: true }
    : op === 'draft-preview' ? { run_id: run.run_id, plan_id: run.plan_id, read_only: true, preview_id: 'view_' + 'e'.repeat(32), preview_pdf_base64: preview.preview_pdf_base64 } : client(op, data);
  await f.flow.handle(message('让我看看预览'), f.state, { workflow: { intent: 'preview' } });
  assert.ok(!f.calls.some(([op]) => op === 'final-preview'));
  assert.match(f.sends.at(-1)[1].text, /已发送旧版草稿/);
});
