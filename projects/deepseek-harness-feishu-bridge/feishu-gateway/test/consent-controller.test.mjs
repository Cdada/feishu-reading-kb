import test from 'node:test';
import assert from 'node:assert/strict';
import { ConsentController, consentMessageFromSdk } from '../src/consent-controller.mjs';

const snapshot = { policy_version:'cloud-body-consent/v1', consent_id:'consent_1', consent_hash:'a'.repeat(64),
  plan_id:'plan_1',source_task_id:'task_1',source_filename:'synthetic.pdf',recipient:'openai_codex_cloud',redaction:'none',
  files:[{artifact_id:'artifact_1',relative_path:'正文.md',size_bytes:10,sha256:'b'.repeat(64)}],
  total_bytes:10,purposes:['organize'],target_language:null,expires_at:2000,access_window_seconds:3600,formal_write:false };
const event = (changes={}) => ({senderId:'owner_1',chatId:'chat_1',chatType:'p2p',messageId:'message_1',createTime:1000000,
  rawContentType:'text',content:'准备正文授权',resources:[],...changes});

function fixture() {
  const state={pendingCuration:{planId:'plan_1',sourceTaskId:'task_1'},pendingAttachments:[],pendingContentConsent:null};
  const calls=[],sent=[],saves=[];
  const dependencies={signingKey:Buffer.alloc(32, 7),ownerId:'owner_1',now:()=>1000,
    save:async()=>{saves.push(structuredClone(state));},
    send:async(...args)=>{sent.push(args);return {messageId:'prompt_1'};},
    client:async(operation, payload)=>{
      calls.push({operation,payload});
      if(operation==='prepare') return structuredClone(snapshot);
      if(operation==='invalidate') return {access_active:false};
      return {consent_id:'consent_1',plan_id:'plan_1',state:{presented:'presented',allow_cloud_body_once:'approved',deny_cloud_body:'denied',revoke_cloud_body:'revoked'}[payload.proof.kind]};
    }};
  return {state,calls,sent,saves,dependencies,controller:new ConsentController(dependencies)};
}

test('SDK adapter uses millisecond createTime and preserves reply binding',()=>{
  assert.equal(consentMessageFromSdk(event()).createdAtSeconds,1000);
  assert.equal(consentMessageFromSdk(event({createTime:undefined})).createdAtSeconds,undefined);
  assert.equal(consentMessageFromSdk(event({replyToMessageId:'p'})).replyToMessageId,'p');
});
test('completed v1.1 workflow cannot fall through to legacy translation offer or body grant',async()=>{
  for(const [content,options] of [['准备正文授权',{}],['请开始这份文件',{prepare:true}]]){
    const f=fixture();f.controller.translationChoice=true;
    f.state.workflowV11={id:'flow',phase:'complete',filename:'已完成.pdf'};
    f.state.pendingTranslationChoice={planId:'plan_1',sourceTaskId:'task_1'};
    assert.equal(await f.controller.handle(event({content}),f.state,options),true);
    assert.equal(f.calls.length,0);assert.equal(f.state.pendingTranslationChoice,null);
    assert.match(f.sent.at(-1)[1].text,/已经完成/);
  }
});
test('prepare, send, persist delivery and explicit reply use control plane only',async()=>{
  const f=fixture();
  assert.equal(await f.controller.handle(event(),f.state),true);
  assert.deepEqual(f.saves.map(s=>s.pendingContentConsent?.phase),['preparing','sending','presenting','awaiting']);
  assert.equal(f.sent[0][1].text.includes('未经脱敏'),true);
  assert.equal(await f.controller.handle(event({content:'允许本次正文外发',messageId:'reply_1',replyToMessageId:'prompt_1'}),f.state),true);
  assert.equal(f.state.pendingContentConsent.phase,'approved');
  assert.equal(f.calls.filter(x=>x.operation==='proof').length,2);
  assert.ok(!JSON.stringify(f.state).includes('access_token'));
  assert.ok(f.sent.at(-1)[1].text.includes('没有读取或外发正文'));
});
test('casual replies, wrong sender and wrong parent do not create approval',async()=>{
  const f=fixture();await f.controller.handle(event(),f.state);
  for(const message of [event({content:'好的',replyToMessageId:'prompt_1'}),event({content:'允许本次正文外发',replyToMessageId:'other'}),
    event({content:'允许本次正文外发',senderId:'other',replyToMessageId:'prompt_1'}),
    event({content:'允许本次正文外发',rawContentType:'post',replyToMessageId:'prompt_1'})]) await f.controller.handle(message,f.state);
  assert.equal(f.calls.filter(x=>x.payload.proof?.kind==='allow_cloud_body_once').length,0);
});
test('expired body authorization identifies expiry and offers renewal without approving',async()=>{
  const f=fixture();await f.controller.handle(event(),f.state);f.state.pendingContentConsent.consent.expires_at=999;
  await f.controller.handle(event({content:'允许本次正文外发',replyToMessageId:'prompt_1',messageId:'reply'}),f.state);
  assert.match(f.sent.at(-1)[1].text,/过期/);assert.match(f.sent.at(-1)[1].text,/重新发送正文授权/);
  assert.equal(f.calls.filter(x=>x.payload.proof?.kind==='allow_cloud_body_once').length,0);
});
test('new attachment and plan-changing text invalidate before normal routing',async()=>{
  for(const message of [event({resources:[{type:'file'}]}),event({content:'改到其他知识库'})]){
    const f=fixture();await f.controller.handle(event(),f.state);
    assert.equal(await f.controller.handle(message,f.state),false);
    assert.equal(f.calls.at(-1).operation,'invalidate');assert.equal(f.state.pendingContentConsent,null);
  }
});
test('reset, cancel and restart invalidate previous scope, status does not',async()=>{
  for(const flags of [{reset:true},{cancel:true}]){
    const f=fixture();await f.controller.handle(event(),f.state);
    await f.controller.handle(event({content:'操作'}),f.state,flags);
    assert.equal(f.state.pendingContentConsent,null);
  }
  const f=fixture();await f.controller.handle(event(),f.state);
  await f.controller.handle(event({content:'状态'}),f.state,{status:true});
  assert.equal(f.state.pendingContentConsent.phase,'awaiting');
  const restarted=new ConsentController(f.dependencies);await restarted.recover({chat_1:f.state});
  assert.equal(f.state.pendingContentConsent,null);assert.equal(f.calls.at(-1).operation,'invalidate');
});
test('uncertain send outcome is never retried or treated as presented',async()=>{
  const f=fixture();f.controller.send=async()=>{throw new Error('network');};
  await assert.rejects(f.controller.handle(event(),f.state));
  assert.equal(f.state.pendingContentConsent.phase,'sending');
  assert.equal(f.calls.length,1);
  await f.controller.recover({chat:f.state});
  assert.equal(f.state.pendingContentConsent,null);assert.equal(f.calls.at(-1).operation,'invalidate');
});
test('uncertain approval outcome is invalidated on recovery without body dispatch',async()=>{
  const f=fixture();await f.controller.handle(event(),f.state);
  f.controller.client=async(op,payload)=>{if(op==='proof')throw new Error('unknown outcome');return f.dependencies.client(op,payload);};
  await assert.rejects(f.controller.handle(event({content:'允许本次正文外发',replyToMessageId:'prompt_1',messageId:'reply_1'}),f.state));
  assert.equal(f.state.pendingContentConsent.phase,'deciding');
  await f.controller.recover({chat:f.state});assert.equal(f.state.pendingContentConsent,null);
});

test('restart preserves only service-verified completed review, never replays approval', async () => {
  const good = {run_id:'run_1',plan_id:'plan_1',consent_id:'consent_1',consent_state:'exhausted',state:'review_ready'};
  for (const change of [null, {state:'reading'}, {consent_state:'revoked'}, {consent_id:'other'}, {plan_id:'other'}, {run_id:'other'}]) {
    const f=fixture();
    f.state.pendingContentConsent={phase:'approved',planId:'plan_1',consent:structuredClone(snapshot)};
    f.state.activeCurationRun={runId:'run_1',planId:'plan_1',consentId:'consent_1',phase:'review_ready'};
    f.controller.client=async(op,payload)=>op==='run-status' ? {...good,...change} : f.dependencies.client(op,payload);
    await f.controller.recover({chat:f.state});
    if (change) assert.equal(f.state.pendingContentConsent,null);
    else {
      assert.equal(f.state.pendingContentConsent.phase,'approved');
      assert.equal(f.state.pendingContentConsent.consent.expires_at,snapshot.expires_at);
      assert.equal(f.calls.length,0);
    }
    assert.equal(f.sent.length,0);
  }
});
test('durable-save failure before delivery prevents sending a consent prompt',async()=>{
  const f=fixture();f.controller.save=async()=>{throw new Error('disk full');};
  await assert.rejects(f.controller.handle(event(),f.state));
  assert.equal(f.sent.length,0);assert.equal(f.calls.length,0);
});
test('invalidation failure blocks next action and retains retry intent',async()=>{
  const f=fixture();await f.controller.handle(event(),f.state);
  f.controller.client=async()=>{throw new Error('offline');};
  await assert.rejects(f.controller.handle(event({resources:[{type:'file'}]}),f.state));
  assert.equal(f.state.pendingContentConsent.phase,'invalidating');
});
test('split consent prompts cannot be approved and duplicates never redispatch',async()=>{
  const f=fixture();f.controller.send=async()=>({messageId:'p',chunkIds:['p','p2']});
  await assert.rejects(f.controller.handle(event(),f.state),/单条/);
  assert.equal(f.calls.length,1);
  const good=fixture();await good.controller.handle(event(),good.state);
  const reply=event({content:'允许本次正文外发',replyToMessageId:'prompt_1',messageId:'reply_1'});
  await good.controller.handle(reply,good.state);await good.controller.handle(reply,good.state);
  assert.equal(good.calls.filter(x=>x.payload.proof?.kind==='allow_cloud_body_once').length,1);
});
