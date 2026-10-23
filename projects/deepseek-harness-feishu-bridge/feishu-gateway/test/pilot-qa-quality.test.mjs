import test from 'node:test';
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { runQuality, judgeSchema, tokenReceipt, qualityClientOptions, EvaluationClient } from '../src/pilot-qa-quality.mjs';
import { MODEL } from '../src/pilot-reading-guide.mjs';
import { qaSchema } from '../src/pilot-knowledge-qa.mjs';

const sha = v => createHash('sha256').update(v).digest('hex');
function fixture(judge = false) {
  const calls = [];
  const account = { requiresOpenaiAuth: true, account: { type: 'chatgpt', planType: 'pro', email: 'synthetic@example.invalid' } };
  const limits = { rateLimits: { limitId: 'codex', planType: 'pro', rateLimitReachedType: null,
    primary: { usedPercent: 20 }, credits: { hasCredits: false, unlimited: false } } };
  const client = { start: async()=>{}, pinCurrentProcess:()=>{}, close:async()=>{},
    request:async(m)=>m==='account/read'?account:limits, ensureThread:async()=> 'synthetic',
    prompt:async(t,r,o)=>{calls.push(o);return {text:'{}',toolCalls:[]};},
    receipt:{provider_model:MODEL,usage:{input_tokens:19,output_tokens:7}} };
  const request = JSON.stringify({ protocol:judge?'qa-quality-judge-v1':'qa-quality-answer-v1',
    scope:'provided_context',instruction:'合成测试',evidence:[], ...(judge?{reference:{},candidate:{},rubric:{}}:{}) });
  return {client,calls,packet:{model:MODEL,effort:'medium',waitMs:1000,request,requestId:'synthetic',
    binding:{expiresAt:new Date(Date.now()+60000).toISOString(),requestSha:sha(request),accountSha:sha(account.account.email)}}};
}
test('evaluation routes answer and judge to distinct fixed schemas with real receipts', async()=>{
  for (const judging of [false,true]) {
    const f=fixture(judging);
    const r=await runQuality(f.packet,{client:f.client,check:async()=>{}});
    assert.equal(r.status,'received'); assert.equal(f.calls.length,1);
    assert.deepEqual(f.calls[0].outputSchema,judging?judgeSchema:qaSchema);
    assert.equal(r.provider_model,MODEL); assert.equal(r.usage.input_tokens,19);
  }
});
test('answer input rejects leaked references before starting a turn', async()=>{
  const f=fixture();const r=JSON.parse(f.packet.request);r.reference={};f.packet.request=JSON.stringify(r);
  f.packet.binding.requestSha=sha(f.packet.request);
  await assert.rejects(runQuality(f.packet,{client:f.client,check:async()=>{}}));
  assert.equal(f.calls.length,0);
});
test('evaluation preserves missing usage and fails on missing model acknowledgement', async()=>{
  assert.deepEqual(tokenReceipt(null),{input_tokens:null,output_tokens:null});
  assert.deepEqual(tokenReceipt({last:{inputTokens:10,outputTokens:-1}}),{input_tokens:10,output_tokens:null});
  const f=fixture(true);delete f.client.receipt;
  assert.equal((await runQuality(f.packet,{client:f.client,check:async()=>{}})).status,'received_rejected');
});
test('usage belongs to the current thread and terminal turn; tools are visible', ()=>{
  const client=new EvaluationClient({});
  client.evaluation={threadId:'one',usage:new Map(),unexpectedItems:new Set()};
  const notification=(threadId,turnId,n)=>({method:'thread/tokenUsage/updated',params:{threadId,turnId,tokenUsage:{last:{inputTokens:n,outputTokens:2}}}});
  client.handleMessage(notification('other','t',900));
  client.handleMessage(notification('one','t',12));
  assert.equal(client.evaluation.usage.size,1); assert.equal(client.evaluation.usage.get('t').input_tokens,12);
  client.handleMessage({method:'item/started',params:{threadId:'one',turnId:'t',item:{type:'commandExecution'}}});
  assert.ok(client.evaluation.unexpectedItems.has('commandExecution'));
});
test('tool attempts and unknown outcomes never trigger an automatic second call', async()=>{
  const f=fixture(true);let calls=0;
  f.client.prompt=async()=>{calls++;throw new Error('synthetic timeout');};
  assert.equal((await runQuality(f.packet,{client:f.client,check:async()=>{}})).status,'unknown');assert.equal(calls,1);
  const g=fixture(true);g.client.prompt=async()=>({text:'{}',toolCalls:[{type:'webSearch'}]});
  assert.equal((await runQuality(g.packet,{client:g.client,check:async()=>{}})).status,'received_rejected');
});
test('subscription checks stop paid-credit and low-quota cases before inference',async()=>{
  const f=fixture(true);const old=f.client.request;
  f.client.request=async(m)=>{const v=await old(m);if(m==='account/rateLimits/read')v.rateLimits.credits.hasCredits=true;return v;};
  assert.equal((await runQuality(f.packet,{client:f.client,check:async()=>{}})).status,'blocked_before_turn');assert.equal(f.calls.length,0);
  const g=fixture(true);const original=g.client.request;
  g.client.request=async(m)=>{const v=await original(m);if(m==='account/rateLimits/read')v.rateLimits.primary.usedPercent=100;return v;};
  assert.equal((await runQuality(g.packet,{client:g.client,check:async()=>{}})).status,'blocked_before_turn');assert.equal(g.calls.length,0);
  const opts=qualityClientOptions({cwd:'/synthetic',codexBinary:'/synthetic/codex',check:async()=>{},environment:{OPENAI_API_KEY:'excluded'}});
  assert.deepEqual(opts.allowedMcpServers,[]);assert.equal(opts.ephemeral,true);assert.deepEqual(opts.processEnvironment,{});
});
