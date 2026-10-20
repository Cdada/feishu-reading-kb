import test from 'node:test';
import assert from 'node:assert/strict';
import { CodexAppServerClient } from '../src/codex-app-server-client.mjs';
import { translationModel, curationModel, dshTranslationModel } from '../src/workflow-models.mjs';
import { translationPatch } from '../src/dsh-translation-client.mjs';

const makeClient = () => {
  const client = new CodexAppServerClient({ cwd: '/tmp' });
  client.start = async () => {};
  client.close = async () => {};
  return client;
};
const complete = (client, turnId = 'turn', threadId = 'thread') => {
  client.handleMessage({ method: 'item/completed', params: {
    turnId, threadId, item: { type: 'agentMessage', phase: 'final_answer', text: 'synthetic result' },
  } });
  client.handleMessage({ method: 'turn/completed', params: { threadId, turn: { id: turnId, status: 'completed' } } });
};

test('user-approved workflow profiles preserve enabled DSH reasoning', () => {
  assert.deepEqual(translationModel, {model:'gpt-5.6-sol',effort:'high'});
  assert.deepEqual(curationModel, {model:'gpt-6-astra',effort:'medium'});
  assert.equal(dshTranslationModel.reasoningEffort,'high');
  const patch=translationPatch.find(x=>x.id==='llm-deepseek').config;
  assert.equal(patch.thinking,'enabled');
  assert.equal(patch.reasoningEffort,dshTranslationModel.reasoningEffort);
});

for (const profile of [translationModel, curationModel]) {
  test(`${profile.model}: pin new/resumed threads and every turn`, async () => {
    const client=makeClient(); Object.assign(client.options,profile);
    client.restrictedConfig=client.buildRestrictedConfig({});
    const calls=[];
    client.request=async (method,params)=>{
      calls.push({method,params});
      if(method==='turn/start'){complete(client);return {turn:{id:'turn'}};}
      return {thread:{id:'thread'},model:profile.model,reasoningEffort:profile.effort};
    };
    await client.ensureThread(); await client.ensureThread('thread');
    await client.prompt('thread','synthetic');
    assert.deepEqual(calls.map(x=>x.method),['thread/start','thread/resume','turn/start']);
    for(const {params} of calls)assert.equal(params.model,profile.model);
    for(const {params} of calls.slice(0,2)){
      assert.equal(params.config.model,profile.model);
      assert.equal(params.config.model_reasoning_effort,profile.effort);
    }
    assert.equal(calls[2].params.effort,profile.effort);
    await assert.rejects(client.prompt('thread','synthetic',{effort:'low'}),/固定设置/);
    assert.equal(calls.length,3);
  });
}

test('mismatched returned profile stops before processing source text',async()=>{
  for(const actual of [{model:'other',reasoningEffort:'high'},{model:translationModel.model,reasoningEffort:'low'},{}]){
    const client=makeClient(); Object.assign(client.options,translationModel); let stopped=false;
    client.close=async()=>{stopped=true;};
    client.request=async()=>({thread:{id:'thread'},...actual});
    await assert.rejects(client.ensureThread('thread'),/设置不一致/);
    assert.equal(stopped,true);
    assert.equal(client.threadIds.size,0);
  }
});

test('model rerouting rejects both active and early turns',async()=>{
  for(const early of [false,true]){
    const client=makeClient(); Object.assign(client.options,translationModel);let stopped=false;
    client.close=async()=>{stopped=true;};
    const reroute=()=>client.handleMessage({method:'model/rerouted',params:{threadId:'thread',turnId:'turn'}});
    client.request=async()=>{if(early)reroute();return {turn:{id:'turn'}};};
    const result=client.prompt('thread','synthetic');
    const rejected=assert.rejects(result,/指定模型|执行端在启动回合时退出/);
    if(!early){await new Promise(resolve=>setImmediate(resolve));reroute();}
    await rejected;assert.equal(stopped,true);assert.equal(client.activeTurns.size,0);
  }
});

test('MCP and built-in tools are explicitly restricted', () => {
  const client = new CodexAppServerClient({ allowedMcpServers: ['kb_curation'], autoApprovedMcpServers: ['kb_curation'] });
  const config = client.buildRestrictedConfig({ mcp_servers: { unknown: {}, local_conversion: {} }, plugins: { unsafe: {} } });
  assert.equal(config.mcp_servers.unknown.enabled, false);
  assert.equal(config.mcp_servers.local_conversion.enabled, false);
  assert.equal(config.mcp_servers.kb_curation.enabled_tools.length, 14);
  assert.ok(config.mcp_servers.kb_curation.enabled_tools.includes('read_next_curation_chunk'));
  assert.ok(!config.mcp_servers.kb_curation.enabled_tools.some((name) => /approve|publish|rollback|proof/u.test(name)));
  assert.ok(!config.mcp_servers.kb_curation.enabled_tools.includes('read_body'));
  assert.equal(config.plugins.unsafe.enabled, false);
  assert.equal(config.features.shell_tool, false);
  assert.equal(config.features.unified_exec, false);
  assert.equal(config.agents.enabled, false);
  assert.equal(config.web_search, 'disabled');
  assert.throws(() => new CodexAppServerClient({ allowedMcpServers: ['new_server'] }).buildRestrictedConfig({}), /白名单/);
});

test('failed resume never starts a replacement task', async () => {
  const client = makeClient();
  const methods = [];
  client.request = async (method) => { methods.push(method); throw new Error('temporary failure'); };
  await assert.rejects(client.ensureThread('persisted'), /temporary failure/);
  assert.deepEqual(methods, ['thread/resume']);
});

test('repeated curation failures stop the dedicated executor instead of looping', async () => {
  const client = makeClient(); let stopped = 0;
  client.close = async () => { stopped += 1; };
  client.request = async () => ({ turn: { id: 'turn' } });
  const result = client.prompt('thread', 'synthetic', { maxCurationErrors: 2 });
  await new Promise((resolve) => setImmediate(resolve));
  for (let index = 0; index < 2; index += 1) client.handleMessage({ method: 'item/completed', params: {
    threadId: 'thread', turnId: 'turn', item: { type: 'mcpToolCall', server: 'kb_curation', tool: 'submit_curation_draft', status: 'completed',
      result: { structuredContent: { schema_version: 'kb-curation/v1', ok: false, status: 'failed', error_code: 'INVALID_DRAFT_SCHEMA', request_id: `failure_${index}` } } },
  } });
  await assert.rejects(result, /停止盲目重试/);
  assert.equal(stopped, 1);
  assert.equal(client.activeTurns.size, 0);
});

test('early completion notifications are retained and unrelated turns ignored', async () => {
  const client = makeClient();
  client.request = async () => {
    complete(client, 'other');
    complete(client);
    return { turn: { id: 'turn' } };
  };
  const result = await client.prompt('thread', 'synthetic');
  assert.equal(result.text, 'synthetic result');
  assert.equal(client.getStatus().activeTurnCount, 0);
  assert.equal(client.activeTurns.size, 0);
});

test('concurrent prompt is rejected before a second turn starts', async () => {
  const client = makeClient();
  let started;
  const ready = new Promise((resolve) => { started = resolve; });
  let calls = 0;
  client.request = async () => { calls += 1; started(); return { turn: { id: 'turn' } }; };
  const first = client.prompt('thread', 'first');
  await ready;
  await assert.rejects(client.prompt('thread', 'second'), /仍在执行/);
  complete(client);
  await first;
  assert.equal(calls, 1);
});

test('timeout interrupts and closes before permitting another turn', async () => {
  const client = makeClient();
  const methods = [];
  client.request = async (method, params) => {
    methods.push(method);
    if (method === 'turn/interrupt') assert.deepEqual(params, { threadId: 'thread', turnId: 'turn' });
    return { turn: { id: 'turn' } };
  };
  client.close = async () => {
    methods.push('close');
    assert.equal(client.busyThreads.has('thread'), true);
    complete(client); // Late success must not turn a timeout into success.
  };
  await assert.rejects(client.prompt('thread', 'synthetic', { timeoutMs: 5 }), /任务超时/);
  assert.deepEqual(methods, ['turn/start', 'turn/interrupt', 'close']);
  assert.equal(client.busyThreads.size, 0);
});

test('uncertain turn start stops process and is not retried', async () => {
  const client = makeClient();
  let requests = 0;
  let closed = false;
  client.request = async () => { requests += 1; throw new Error('start timeout'); };
  client.close = async () => { closed = true; };
  await assert.rejects(client.prompt('thread', 'synthetic'), /start timeout/);
  assert.equal(requests, 1);
  assert.equal(closed, true);
  assert.equal(client.startingTurns.size, 0);
});

test('start waits for initialization even if process has already spawned', async () => {
  const client = new CodexAppServerClient({});
  client.child = { exitCode: null };
  let finish;
  client.startPromise = new Promise((resolve) => { finish = resolve; });
  let ready = false;
  const started = client.start().then(() => { ready = true; });
  await Promise.resolve();
  assert.equal(ready, false);
  finish();
  await started;
  assert.equal(ready, true);
});

test('process exit between turn response and continuation cannot leave a phantom active turn', async () => {
  const client = makeClient();
  client.request = async () => {
    client.processGeneration += 1;
    return { turn: { id: 'turn' } };
  };
  await assert.rejects(client.prompt('thread', 'synthetic'), /执行端在启动回合时退出/);
  assert.equal(client.activeTurns.size, 0);
  assert.equal(client.busyThreads.size, 0);
});
