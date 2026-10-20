import test from 'node:test';
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { mkdtemp, writeFile, rm, realpath, truncate, open } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { MODEL, clientOptions, textEnvironment, checkConfig, checkSubscription,
  makeAccessCheck, runText, guideSchema, readPacketInput } from '../src/pilot-reading-guide.mjs';
import { CodexAppServerClient } from '../src/codex-app-server-client.mjs';
import { fileURLToPath } from 'node:url';

const sha=(v)=>createHash('sha256').update(v).digest('hex');
const account={ requiresOpenaiAuth:true,account:{type:'chatgpt',planType:'pro',email:'synthetic@example.invalid'} };
const accountSha=sha(account.account.email);
const limits={rateLimits:{limitId:'codex',planType:'pro',rateLimitReachedType:null,
  primary:{usedPercent:10},secondary:{usedPercent:20},credits:{hasCredits:false,unlimited:false}}};
function setup(overrides={}) {
  const calls=[];
  const client={
    start:async()=>calls.push('start'), pinCurrentProcess:()=>calls.push('pin'), close:async()=>calls.push('close'),
    request:async(method,params)=>{calls.push([method,params]);return method==='account/read'?account:limits;},
    ensureThread:async()=>{calls.push('thread');return 'synthetic-thread';},
    prompt:async(thread,text,options)=>{calls.push({thread,text,options});return {text:'{}',toolCalls:[]};},
    ...overrides,
  };
  const packet={model:MODEL,effort:'medium',waitMs:600000,request:'{"synthetic":true}',requestId:'synthetic',
    binding:{expiresAt:new Date(Date.now()+3600000).toISOString(),accountSha,requestSha:sha('{"synthetic":true}')}};
  return {calls,client,packet};
}

test('only approved account and nonpaid subscription pass; secrets never needed',()=>{
  assert.equal(checkSubscription(account,limits,accountSha).paidCreditsAvailable,false);
  for(const a of [{...account,account:{type:'apiKey'}},
    {...account,account:{...account.account,planType:'self_serve_business_usage_based'}},
    {...account,account:{...account.account,email:'different@example.invalid'}}]) {
    assert.throws(()=>checkSubscription(a,limits,accountSha));
  }
  for(const credits of [null,{}, {hasCredits:true,unlimited:false},{hasCredits:false,unlimited:true}]) {
    assert.throws(()=>checkSubscription(account,{rateLimits:{...limits.rateLimits,credits}},accountSha));
  }
});

test('exhausted or unknown usage does not start a turn',async()=>{
  for(const usedPercent of [95,100,null,NaN]) {
    const s=setup({request:async(m)=>m==='account/read'?account:{rateLimits:{...limits.rateLimits,primary:{usedPercent}}}});
    assert.equal((await runText(s.packet,{client:s.client,check:async()=>{}})).status,'blocked_before_turn');
    assert.equal(s.calls.includes('thread'),false);
  }
});

test('environment excludes keys, custom providers, proxy and node injection',()=>{
  const actual=textEnvironment({HOME:'/synthetic',PATH:'/bin',CODEX_HOME:'/synthetic/codex',
    OPENAI_API_KEY:'synthetic-placeholder',CODEX_ACCESS_TOKEN:'synthetic-placeholder',
    NODE_OPTIONS:'--import=bad',HTTPS_PROXY:'https://example.invalid',OPENAI_BASE_URL:'https://example.invalid'});
  assert.deepEqual(Object.keys(actual).sort(),['CODEX_HOME','HOME','PATH']);
});

test('configuration pins official provider without forced logout settings',()=>{
  const cfg={model_provider:'openai',model:MODEL,model_reasoning_effort:'medium'};
  checkConfig(cfg);
  for(const change of [{model_provider:'other'},{model_providers:{openai:{}}},
    {chatgpt_base_url:'https://example.invalid'},{model_reasoning_effort:'high'}]) {
    assert.throws(()=>checkConfig({...cfg,...change}));
  }
  const options=clientOptions({cwd:'/synthetic',codexBinary:'/synthetic/codex',check:async()=>{},environment:{}});
  assert.equal(options.processArgs.join(' ').includes('forced_login_method'),false);
  assert.ok(options.processArgs.includes('chatgpt_base_url="https://chatgpt.com/backend-api"'));
  const client=new CodexAppServerClient(options);
  client.restrictedConfig=client.buildRestrictedConfig({mcp_servers:{unrelated:{}},plugins:{unrelated:{}}});
  const p=client.threadParams();
  assert.equal(p.modelProvider,'openai'); assert.equal(p.approvalPolicy,'never');
  assert.equal(p.config.features.shell_tool,false); assert.equal(p.config.features.hooks,false);
  assert.equal(p.config.mcp_servers.unrelated.enabled,false);
  assert.equal(p.config.plugins.unrelated.enabled,false);
  assert.equal(p.config.project_doc_max_bytes,0);
});

test('success uses a single business turn, no login and no tools',async()=>{
  const s=setup(); let checks=0;
  const result=await runText(s.packet,{client:s.client,check:async()=>{checks++;}});
  assert.equal(result.status,'received');
  assert.deepEqual(s.calls.filter(Array.isArray).map(x=>x[0]),['account/read','account/rateLimits/read']);
  assert.deepEqual(s.calls.find(x=>Array.isArray(x)&&x[0]==='account/read')[1],{refreshToken:false});
  const turn=s.calls.find(x=>x?.options);
  assert.deepEqual(turn.options.outputSchema,guideSchema);assert.equal(turn.options.effort,'medium');
  assert.equal(s.calls.filter(x=>x?.options).length,1); assert.ok(checks>=5);
  assert.equal(s.calls.at(-1),'close');
  assert.ok(s.calls.indexOf('pin') < s.calls.findIndex(x=>Array.isArray(x)&&x[0]==='account/read'));
});

for (const exitAfter of [null, 'account/rateLimits/read', 'thread/start']) {
  test(exitAfter ? `verified child exits after ${exitAfter}: no replacement or generation`
    : 'verified original child completes exactly one synthetic turn', async()=>{
    const s=setup();
    const client=new CodexAppServerClient({
      ...clientOptions({cwd:tmpdir(),check:async()=>{}}),
      codexBinary:process.execPath,
      processArgs:[fileURLToPath(new URL('./pilot-app-server-fixture.mjs',import.meta.url))],
    });
    const request=client.request.bind(client), methods=[];
    client.request=async(method,...args)=>{
      methods.push(method);
      const value=await request(method,...args);
      if(method===exitAfter) await client.close();
      return value;
    };
    const result=await runText(s.packet,{client,check:async()=>{}});
    if(exitAfter) assert.notEqual(result.status,'received');
    else assert.equal(result.status,'received');
    assert.equal(methods.filter(x=>x==='initialize').length,1);
    assert.equal(methods.filter(x=>x==='turn/start').length,exitAfter ? 0 : 1);
    assert.equal(client.child,null);
  });
}

test('replacement during asynchronous request guard receives no request bytes',async()=>{
  let writes=0;
  const child=()=>({exitCode:null,signalCode:null,killed:false,stdin:{writable:true,write:()=>{writes++;}}});
  const client=new CodexAppServerClient({beforeRequest:async()=>{
    client.child=child(); // A different live process must not inherit verification.
    client.processGeneration++;
  }});
  client.child=child();
  client.pinCurrentProcess();
  await assert.rejects(client.request('turn/start',{}),/已核验/);
  assert.equal(writes,0);
  assert.equal(client.pendingRequests.size,0);
  assert.throws(()=>client.pinCurrentProcess(),/已核验/);
});

test('lost turn response is unknown with no resubmission',async()=>{
  let calls=0;const s=setup({prompt:async()=>{calls++;throw Error('synthetic connection loss');}});
  assert.equal((await runText(s.packet,{client:s.client,check:async()=>{}})).status,'unknown');
  assert.equal(calls,1);assert.equal(s.calls.at(-1),'close');
});

test('a tool call result is rejected after receipt',async()=>{
  const s=setup({prompt:async()=>({text:'{}',toolCalls:[{tool:'synthetic'}]})});
  assert.equal((await runText(s.packet,{client:s.client,check:async()=>{}})).status,'received_rejected');
});

test('expiry after account metadata prevents generation',async()=>{
  const s=setup();let n=0;
  const result=await runText(s.packet,{client:s.client,check:async()=>{if(++n===3)throw Error('expired');}});
  assert.equal(result.status,'blocked_before_turn');assert.equal(s.calls.includes('thread'),false);
});

test('close failure cannot relabel an attempted turn as never submitted',async()=>{
  const s=setup({close:async()=>{throw Error('cleanup failure');}});
  const result=await runText(s.packet,{client:s.client,check:async()=>{}});
  assert.equal(result.status,'unknown');assert.equal(result.cleanup,'failed');
});

test('payload hash mismatch prevents client start',async()=>{
  const s=setup();s.packet.request='changed';
  assert.equal((await runText(s.packet,{client:s.client,check:async()=>{}})).status,'blocked_before_turn');
  assert.equal(s.calls.includes('start'),false);
});

test('access check rechecks deadline after source hashing',async(t)=>{
  const dir=await realpath(await mkdtemp(join(tmpdir(),'pilot-text-')));t.after(()=>rm(dir,{recursive:true,force:true}));
  const expiresAt=new Date(Date.now()+3600000).toISOString(),startsAt=new Date(Date.now()-1000).toISOString();
  const path=join(dir,'approval.json'),source=join(dir,'source.txt');
  const a=JSON.stringify({approved:true,model:MODEL,effort:'medium',starts_at:startsAt,
    expires_at:expiresAt,account_sha256:accountSha,request_sha256:['a'.repeat(64)]});
  await writeFile(path,a);await writeFile(source,'synthetic');
  const binding={startsAt,expiresAt,consentExpiresAt:expiresAt,approvalPath:path,approvalSha:sha(a),
    accountSha,requestSha:'a'.repeat(64),files:{[source]:sha('synthetic')}};
  await makeAccessCheck(binding)();
  let reads=0;
  await assert.rejects(makeAccessCheck(binding,()=>++reads<=3?Date.parse(startsAt):Date.parse(expiresAt)));
  await writeFile(source,'changed');
  await assert.rejects(makeAccessCheck(binding));
});

test('profile must echo provider as well as model and effort',async()=>{
  const client=new CodexAppServerClient({model:MODEL,effort:'medium',modelProvider:'openai'});
  let closed=false;client.close=async()=>{closed=true;};
  await assert.rejects(client.verifyThreadProfile({model:MODEL,reasoningEffort:'medium',modelProvider:'other'}));
  assert.equal(closed,true);
});

test('large frozen runtime file is streamed and changed bytes are rejected',async(t)=>{
  const dir=await realpath(await mkdtemp(join(tmpdir(),'pilot-text-')));t.after(()=>rm(dir,{recursive:true,force:true}));
  const expiresAt=new Date(Date.now()+3600000).toISOString(),startsAt=new Date(Date.now()-1000).toISOString();
  const path=join(dir,'approval.json'),source=join(dir,'synthetic-runtime.bin');
  const a=JSON.stringify({approved:true,model:MODEL,effort:'medium',starts_at:startsAt,
    expires_at:expiresAt,account_sha256:accountSha,request_sha256:['a'.repeat(64)]});
  await writeFile(path,a);await writeFile(source,'');await truncate(source,33*1024*1024);
  const hash=createHash('sha256'),chunk=Buffer.alloc(64*1024);
  for(let i=0;i<528;i++)hash.update(chunk);
  const binding={startsAt,expiresAt,consentExpiresAt:expiresAt,approvalPath:path,approvalSha:sha(a),
    accountSha,requestSha:'a'.repeat(64),files:{[source]:hash.digest('hex')}};
  await makeAccessCheck(binding)();
  const handle=await open(source,'r+');
  try {await handle.write(Buffer.from([1]),0,1,33*1024*1024-1);} finally {await handle.close();}
  await assert.rejects(makeAccessCheck(binding));
});

test('request guard rejects immediately before protocol write',async()=>{
  const client=new CodexAppServerClient({beforeRequest:async()=>{throw Error('expired');}});
  let writes=0;client.write=()=>{writes++;};
  await assert.rejects(client.request('turn/start',{}));
  assert.equal(writes,0);assert.equal(client.pendingRequests.size,0);
});

test('startup guard rejects before opening the official child',async()=>{
  const client=new CodexAppServerClient({beforeStart:async()=>{throw Error('expired');}});
  await assert.rejects(client.start());assert.equal(client.child,null);
});


test('blocked checks identify configuration, account, paid credit and quota without submitting',async()=>{
  const cases=[
    ['configuration_rejected',{start:async()=>checkConfig({})},'client_start'],
    ['account_binding_rejected',{request:async(m)=>m==='account/read'
      ? {...account,account:{...account.account,email:'wrong@example.invalid'}} : limits},'subscription_check'],
    ['paid_credit_policy_rejected',{request:async(m)=>m==='account/read' ? account
      : {rateLimits:{...limits.rateLimits,credits:{hasCredits:true,unlimited:false}}}},'subscription_check'],
    ['quota_threshold_reached',{request:async(m)=>m==='account/read' ? account
      : {rateLimits:{...limits.rateLimits,primary:{usedPercent:96}}}},'subscription_check'],
    ['quota_metadata_rejected',{request:async(m)=>m==='account/read' ? account : {}},'subscription_check'],
  ];
  for(const [reason,override,checkpoint] of cases) {
    const s=setup(override),r=await runText(s.packet,{client:s.client,check:async()=>{}});
    assert.deepEqual({status:r.status,checkpoint:r.checkpoint,reason:r.reason},
      {status:'blocked_before_turn',checkpoint,reason});
    assert.equal(s.calls.some(x=>x?.options),false);
    assert.equal(s.calls.at(-1),'close');
  }
});

test('upstream exceptions retain failed phase without exposing messages or forged reason',async()=>{
  const marker='synthetic-private-account-and-token';
  for(const checkpoint of ['account_read','quota_read','thread_setup']) {
    const error=Object.assign(new Error(marker),{code:marker});
    const s=setup({
      request:async(m)=>{
        if(m===(checkpoint==='account_read'?'account/read':'account/rateLimits/read')
          && checkpoint!=='thread_setup')throw error;
        return m==='account/read'?account:limits;
      },
      ensureThread:async()=>{throw error;},
    });
    const r=await runText(s.packet,{client:s.client,check:async()=>{}});
    assert.equal(r.status,'blocked_before_turn');assert.equal(r.checkpoint,checkpoint);
    assert.equal(r.reason,'operation_failed');assert.equal(JSON.stringify(r).includes(marker),false);
    assert.equal(s.calls.some(x=>x?.options),false);
  }
});

test('cleanup failure retains original preflight diagnosis and generation stays unknown',async()=>{
  const s=setup({request:async()=>({}),close:async()=>{throw Error('private cleanup detail');}});
  const r=await runText(s.packet,{client:s.client,check:async()=>{}});
  assert.equal(r.status,'blocked_before_turn');assert.equal(r.reason,'account_binding_rejected');
  assert.equal(r.checkpoint,'subscription_check');assert.equal(r.cleanup,'failed');
  const t=setup({prompt:async()=>{throw Error('private model detail');}});
  const u=await runText(t.packet,{client:t.client,check:async()=>{}});
  assert.equal(u.status,'unknown');assert.equal(u.checkpoint,'generation');
  assert.equal(u.reason,'operation_failed');assert.equal(JSON.stringify(u).includes('private'),false);
});


test('stdin preserves Chinese paths and emoji across every byte split', async () => {
  const { Readable } = await import('node:stream');
  const packet = { binding: { files: { '/工作区/材料📄.pdf': 'synthetic' } }, request: '中文来源' };
  const bytes = Buffer.from(JSON.stringify(packet));
  const stream = Readable.from([...bytes].map((value) => Buffer.from([value])), { objectMode: false });
  assert.deepEqual(await readPacketInput(stream), packet);
});

test('zero reserve only comes from the hash-checked approval and still rejects exhaustion', async () => {
  const s = setup({request: async (m) => m === 'account/read' ? account :
    {rateLimits: {...limits.rateLimits, primary: {usedPercent: 97}}}});
  const result = await runText(s.packet, {client: s.client, check: async () => ({minimumRemainingPercent: 0})});
  assert.equal(result.status, 'received');
  assert.throws(() => checkSubscription(account, {rateLimits: {...limits.rateLimits, primary: {usedPercent: 100}}}, accountSha, 0));
  assert.throws(() => checkSubscription(account, {rateLimits: {...limits.rateLimits, credits: {hasCredits: true, unlimited: false}}}, accountSha, 0));
  for (const invalid of [-1, 100, 1.5, '0', null]) assert.throws(() => checkSubscription(account, limits, accountSha, invalid));
});


test('reserve override is read from the immutable approval, never the packet alone', async(t) => {
  const dir=await realpath(await mkdtemp(join(tmpdir(),'pilot-text-')));t.after(()=>rm(dir,{recursive:true,force:true}));
  const expiresAt=new Date(Date.now()+3600000).toISOString(),startsAt=new Date(Date.now()-1000).toISOString();
  const path=join(dir,'approval.json'),source=join(dir,'source.txt');
  const approval={approved:true,model:MODEL,effort:'medium',starts_at:startsAt,expires_at:expiresAt,
    account_sha256:accountSha,request_sha256:['a'.repeat(64)]};
  await writeFile(source,'synthetic');
  let raw=JSON.stringify(approval);await writeFile(path,raw);
  const binding={startsAt,expiresAt,consentExpiresAt:expiresAt,approvalPath:path,approvalSha:sha(raw),
    accountSha,requestSha:'a'.repeat(64),files:{[source]:sha('synthetic')},minimumRemainingPercent:0};
  assert.equal((await makeAccessCheck(binding)()).minimumRemainingPercent,5);
  raw=JSON.stringify({...approval,minimum_remaining_percent:0});await writeFile(path,raw);
  await assert.rejects(makeAccessCheck(binding));
  binding.approvalSha=sha(raw);
  assert.equal((await makeAccessCheck(binding)()).minimumRemainingPercent,0);
});


test('layout evidence instructions exclude furniture but retain annotations',()=>{
  const options=clientOptions({cwd:'/synthetic',codexBinary:'/synthetic/codex',check:async()=>{}});
  assert.match(options.developerInstructions,/evidence_policy/);
  assert.match(options.developerInstructions,/页脚、页码不能作为正文证据/);
  assert.match(options.developerInstructions,/脚注、参考文献与补充说明应保留/);
});
