import test from 'node:test';
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { runQA, qaSchema, qaClientOptions } from '../src/pilot-knowledge-qa.mjs';
import { MODEL } from '../src/pilot-reading-guide.mjs';

const sha = (v) => createHash('sha256').update(v).digest('hex');
function fixture(intent='answer') {
  const account = { requiresOpenaiAuth: true, account: { type: 'chatgpt', planType: 'pro', email: 'synthetic@example.invalid' } };
  const limits = { rateLimits: { limitId: 'codex', planType: 'pro', rateLimitReachedType: null,
    primary: { usedPercent: 20 }, credits: { hasCredits: false, unlimited: false } } };
  const calls = [];
  const client = { start: async()=>{}, pinCurrentProcess:()=>{}, close:async()=>{},
    request:async(m)=>m==='account/read'?account:limits, ensureThread:async()=> 'synthetic',
    prompt:async(t,r,o)=>{calls.push(o); return {text:'{}',toolCalls:[]};} };
  const request = JSON.stringify({protocol:'P43-qa-v1',intent,evidence:[{text:'合成原文'}]});
  return {client,calls,packet:{model:MODEL,effort:'medium',waitMs:1000,request,requestId:'synthetic',
    binding:{expiresAt:new Date(Date.now()+60000).toISOString(),requestSha:sha(request),accountSha:sha(account.account.email)}}};
}
test('QA pins the schema and makes one subscription turn', async()=>{
  const f=fixture();
  assert.equal((await runQA(f.packet,{client:f.client,check:async()=>{}})).status,'received');
  assert.equal(f.calls.length,1);assert.deepEqual(f.calls[0].outputSchema,qaSchema);
});
test('QA cannot select a tool, freeform schema or another protocol',async()=>{
  const f=fixture('shell');
  await assert.rejects(runQA(f.packet,{client:f.client,check:async()=>{}}));
  assert.equal(f.calls.length,0);
  const opts=qaClientOptions({cwd:'/synthetic',codexBinary:'/synthetic/codex',check:async()=>{},environment:{}});
  assert.deepEqual(opts.allowedMcpServers,[]);assert.equal(opts.model,MODEL);
  assert.match(opts.developerInstructions,/简体中文/);
  assert.match(opts.baseInstructions,/不可信数据/);
});
test('unknown QA outcome never automatically retries',async()=>{
  const f=fixture();let count=0;
  f.client.prompt=async()=>{count++;throw new Error('synthetic timeout');};
  assert.equal((await runQA(f.packet,{client:f.client,check:async()=>{}})).status,'unknown');
  assert.equal(count,1);
});
test('tool use from a QA model is rejected',async()=>{
  const f=fixture();f.client.prompt=async()=>({text:'{}',toolCalls:[{name:'shell'}]});
  assert.equal((await runQA(f.packet,{client:f.client,check:async()=>{}})).status,'received_rejected');
});

test('weekly synthesis retains one subscription turn and evidence ceiling',async()=>{
  const f=fixture('weekly_answer');
  assert.equal((await runQA(f.packet,{client:f.client,check:async()=>{}})).status,'received');
  assert.equal(f.calls.length,1);
  const g=fixture('weekly_answer');
  const r=JSON.parse(g.packet.request);r.evidence=Array.from({length:13},()=>({text:'合成原文'}));
  g.packet.request=JSON.stringify(r);g.packet.binding.requestSha=sha(g.packet.request);
  await assert.rejects(runQA(g.packet,{client:g.client,check:async()=>{}}));
  assert.equal(g.calls.length,0);
});
