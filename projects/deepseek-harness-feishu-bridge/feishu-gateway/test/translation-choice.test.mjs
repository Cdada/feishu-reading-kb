import test from 'node:test';
import assert from 'node:assert/strict';
import {ConsentController} from '../src/consent-controller.mjs';
import {WorkflowRouter} from '../src/workflow-router.mjs';
import {renderTranslationChoice} from '../src/translation-choice.mjs';

const offer = {plan_id:'plan_1',source_task_id:'task_1',source_filename:'English.pdf',language:'non_chinese',
  ask_translation:true,planned_translation:false,scope_hash:'a'.repeat(64)};
const message = (changes={}) => ({senderId:'owner',chatId:'chat',chatType:'p2p',messageId:'msg',createTime:1000000,
  rawContentType:'text',content:'开始正文处理',resources:[],...changes});
function fixture(change={}) {
  const state = {pendingCuration:{planId:'plan_1',sourceTaskId:'task_1'},pendingAttachments:[]};
  const calls=[],sent=[];
  const controller = new ConsentController({signingKey:Buffer.alloc(32,1),ownerId:'owner',now:()=>1000,
    translationChoice:true,save:async()=>{},send:async(c,b)=>{sent.push(b.text);return {messageId:'prompt'};},
    client:async(op,payload)=>{calls.push({op,payload});if(op==='translation-offer')return {...offer,...change};
      if(op==='prepare-selected')return {policy_version:'cloud-body-consent/v1',consent_id:'consent_1',consent_hash:'b'.repeat(64),
        plan_id:'plan_1',source_task_id:'task_1',source_filename:'English.pdf',recipient:'openai_codex_cloud',redaction:'none',
        files:[{artifact_id:'artifact_1',relative_path:'body.md',size_bytes:12,sha256:'c'.repeat(64)}],total_bytes:12,
        purposes:['organize'],target_language:null,expires_at:2000,access_window_seconds:3600,formal_write:false};
      if(op==='proof')return {consent_id:'consent_1',plan_id:'plan_1',state:'presented'};
      return {};
    }});
  return {controller,state,calls,sent};
}
test('foreign/uncertain material asks without granting body access',async()=>{
  for(const language of ['non_chinese','uncertain']) {
    const f=fixture({language});await f.controller.handle(message(),f.state);
    assert.deepEqual(f.calls.map(x=>x.op),['translation-offer']);
    assert.match(f.sent[0],/完整中文译稿/);assert.match(f.sent[0],/不是摘要/);
    assert.equal(f.state.pendingContentConsent,undefined);
  }
});
test('request retains preference but never dispatches or silently changes provider',async()=>{
  const f=fixture();await f.controller.handle(message(),f.state);
  await f.controller.handle(message({content:'需要完整中文译稿',messageId:'reply',replyToMessageId:'prompt'}),f.state);
  assert.equal(f.state.pendingTranslationChoice.preference,'requested');
  assert.match(f.sent.at(-1),/不会自动换成其他模型/);
  assert.deepEqual(f.calls.map(x=>x.op),['translation-offer','translation-offer']);
});
test('decline prepares organize-only consent, not cloud approval',async()=>{
  const f=fixture();await f.controller.handle(message(),f.state);
  await f.controller.handle(message({content:'不需要译稿，继续整理',messageId:'reply',replyToMessageId:'prompt'}),f.state);
  assert.equal(f.state.pendingTranslationChoice,null);
  assert.equal(f.state.pendingContentConsent.phase,'awaiting');
  assert.equal(f.calls.find(x=>x.op==='prepare-selected').payload.translate,false);
  assert.equal(f.calls.filter(x=>x.payload.proof?.kind==='allow_cloud_body_once').length,0);
});
test('wrong owner, stale message, wrong reply, attachment and casual yes never choose',async()=>{
  for(const changes of [{senderId:'stranger'},{createTime:0},{replyToMessageId:'other'},{resources:[{type:'file'}]},{content:'好的'}]) {
    const f=fixture();await f.controller.handle(message(),f.state);
    await f.controller.handle(message({content:'需要完整中文译稿',messageId:'reply',replyToMessageId:'prompt',...changes}),f.state);
    assert.ok(!f.calls.some(x=>x.op==='prepare-selected'));
    assert.notEqual(f.state.pendingTranslationChoice?.preference,'requested');
  }
});
test('Chinese material proceeds without extra question unless translation already planned',async()=>{
  const f=fixture({language:'chinese',ask_translation:false});await f.controller.handle(message(),f.state);
  assert.equal(f.state.pendingTranslationChoice,undefined);assert.equal(f.state.pendingContentConsent.phase,'awaiting');
  const g=fixture({language:'chinese',ask_translation:false,planned_translation:true});await g.controller.handle(message(),g.state);
  assert.match(g.sent[0],/计划中包含翻译/);
});
test('router keeps choice replies away from classifier and preserves original event',async()=>{
  const f=fixture();await f.controller.handle(message(),f.state);
  const router=new WorkflowRouter({ownerId:'owner',save:async()=>{},send:async()=>{},classify:async()=>{throw new Error('must not classify');}});
  const event=message({content:'需要完整中文译稿',replyToMessageId:'prompt'});
  assert.deepEqual(await router.handle(event,f.state),{});assert.equal(event.content,'需要完整中文译稿');
});
test('prompt contains no fingerprint and requires separate dual-provider consent',()=>{
  const result=renderTranslationChoice(offer);assert.ok(!result.includes(offer.scope_hash));assert.match(result,/选择需要不等于同意正文外发/);
  assert.match(result,/DSH/);assert.match(result,/最多修改两轮/);assert.match(result,/DeepSeek 和 OpenAI/);
});
