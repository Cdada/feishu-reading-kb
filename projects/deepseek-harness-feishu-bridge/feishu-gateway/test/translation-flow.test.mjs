import test from 'node:test';
import assert from 'node:assert/strict';
import {TranslationFlow,renderTranslationConsent} from '../src/translation-flow.mjs';
import {translationPatch} from '../src/dsh-translation-client.mjs';
import {isTranslationDeliveryRequest,isRetranslationRequest} from '../src/translation-delivery-intent.mjs';
import {CODEX_TRANSLATION_TIMEOUT_MS} from '../src/translation-executors.mjs';
const event=(changes={})=>({senderId:'owner',chatId:'chat',chatType:'p2p',rawContentType:'text',resources:[],
  content:'需要完整中文译稿',messageId:'request',createTime:Date.now(),...changes});
const c={policy_version:'dsh-translation-consent/v1',consent_id:'translation_'+'a'.repeat(32),consent_hash:'a'.repeat(64),
  plan_id:'plan',source_task_id:'task',source_filename:'Synthetic English',recipient:'deepseek_dsh_and_openai_codex_cloud',
  model:'deepseek-flash',target_language:'中文',redaction:'none',max_revisions:2,expires_at:Math.floor(Date.now()/1000)+1800,
  purposes:['translate','review','revise'],
  files:[{relative_path:'正文.md',size_bytes:42,sha256:'b'.repeat(64)}],total_bytes:42,access_window_seconds:3600,formal_write:false};
const passed=JSON.stringify({passed:true,checks:{coverage:true,faithfulness:true,numbers:true,terminology:true},issues:[]});
function fixture(){
  const state={pendingCuration:{planId:'plan',sourceTaskId:'task'},pendingAttachments:[]},calls=[],sent=[],models=[],pdfs=[];
  let taken=false;
  const deps={ownerId:'owner',signingKey:Buffer.alloc(32,3),save:async()=>{},
    send:async(_chat,value)=>{sent.push(value.text);return {messageId:'prompt'};},
    sendPdf:async(...args)=>{pdfs.push(args);return {messageId:'pdf'};},
    createExecutors:async()=>({translate:async()=>{models.push('dsh');return JSON.stringify({translation:'不要删除这 12 页。'});},
      review:async()=>{models.push('codex');return passed;},close:async()=>{}}),
    client:async(op,data)=>{calls.push([op,data]);
      if(op==='translation-offer')return {plan_id:'plan',source_task_id:'task',scope_hash:'d'.repeat(64)};
      if(op==='translation-prepare')return structuredClone(c);
      if(op==='translation-proof')return {consent_id:c.consent_id,state:{presented:'presented',allow_cloud_body_once:'approved',deny_cloud_body:'denied'}[data.proof.kind]};
      if(op==='translation-check')return {consent_id:c.consent_id,active:true};
      if(op==='translation-record')return {consent_id:c.consent_id,saved:true};
      if(op==='translation-pull'){if(taken)return {consent_id:c.consent_id,done:true};taken=true;return {consent_id:c.consent_id,index:0,total:1,source:'Do not delete the 12 pages.',done:false};}
      if(op==='translation-finish')return {consent_id:c.consent_id,state:'completed',formal_write:false,pdf_base64:'synthetic-pdf'};
      if(op==='translation-delivery')return {consent_id:c.consent_id,plan_id:'plan',source_task_id:'task',state:'completed',formal_write:false,reused:true,layout:'bilingual-v1',pdf_base64:'saved-pdf'};
      if(op==='translation-status')return {consent_id:c.consent_id,completed_parts:0};
      return {};
    }};
  return {state,calls,sent,models,pdfs,deps,flow:new TranslationFlow(deps)};
}
const approval=()=>event({content:'同意本份翻译和复核',replyToMessageId:'prompt',messageId:'approval'});
test('deep-thinking review uses a bounded ten-minute call limit',()=>assert.equal(CODEX_TRANSLATION_TIMEOUT_MS,600000));
test('workflow timeout stops top-level state and stores an actionable standalone notice',async()=>{
  const f=fixture();f.state.workflowV11={id:'flow',phase:'running',planId:'plan',sourceTaskId:'task'};
  f.state.pendingTranslation={workflowId:'flow',phase:'approved',planId:'plan',sourceTaskId:'task',chatId:'chat',consent:c};
  f.flow.createExecutors=async()=>({translate:async()=>JSON.stringify({translation:'已保存译文'}),review:async()=>{throw Error('Codex 调用超时');},close:async()=>{}});
  f.flow.startWorkflow(event(),f.state);await f.flow.running.promise;
  assert.equal(f.state.workflowV11.phase,'stopped');assert.equal(f.state.pendingTranslation.phase,'needs_attention');
  assert.match(f.sent.at(-1),/Codex 复核调用超时/);assert.match(f.sent.at(-1),/不会自动继续/);assert.match(f.sent.at(-1),/继续翻译/);
  assert.equal(f.state.pendingTranslation.failureNoticeUncertain,false);assert.equal(f.pdfs.length,0);
  await f.flow.recover({chat:f.state});assert.equal(f.state.pendingTranslation.phase,'needs_attention');
});
test('explicit resume uses new gateway grant, while queries and negation never resume',async()=>{
  const f=fixture();f.state.workflowV11={id:'flow',phase:'stopped',planId:'plan',sourceTaskId:'task'};
  f.state.pendingTranslation={workflowId:'flow',phase:'needs_attention',planId:'plan',sourceTaskId:'task',chatId:'chat',consent:c};
  let started=0;f.flow.startWorkflow=()=>started++;
  const client=f.flow.client;f.flow.client=async(op,data)=>op==='workflow-translation-resume'?{plan_id:'plan',source_task_id:'task',consent:c,completed_parts:3,total_parts:9,resume_phase:'reviewing'}:client(op,data);
  for(const content of ['可以继续翻译吗？','不要继续翻译','好的'])await f.flow.handle(event({content}),f.state);
  assert.equal(started,0);f.state.pendingTranslation.phase='needs_attention';
  await f.flow.handle(event({content:'继续翻译'}),f.state);
  assert.equal(started,1);assert.match(f.sent.at(-1),/第 4 \/ 9 段/);assert.match(f.sent.at(-1),/只重新进行中断的复核/);
});
test('failed stop-notification receipt is retained for next owner interaction',async()=>{
  const f=fixture();f.state.workflowV11={id:'flow',phase:'running',planId:'plan',sourceTaskId:'task'};
  f.state.pendingTranslation={workflowId:'flow',phase:'approved',planId:'plan',sourceTaskId:'task',chatId:'chat',consent:c};
  f.flow.createExecutors=async()=>{throw Error('timeout');};f.flow.send=async()=>{throw Error('send failed');};
  f.flow.startWorkflow(event(),f.state);await f.flow.running.promise;
  assert.equal(f.state.pendingTranslation.failureNoticeUncertain,true);
  f.flow.send=f.deps.send;await f.flow.notifyRecovery(event(),f.state);
  assert.equal(f.state.pendingTranslation.failureNoticeUncertain,false);assert.match(f.sent.at(-1),/不会自动继续/);
});
test('no active translation leaves normal ingestion messages alone',async()=>{
  const f=fixture();assert.equal(await f.flow.handle(event({content:'看看入库预览'}),f.state),false);
  assert.equal(await f.flow.handle(event({content:'先停一下'}),f.state),false);
  assert.equal(f.sent.length,0);assert.equal(f.calls.length,0);
});
test('request only prepares consent; explicit approval executes DSH then Codex then PDF',async()=>{
  const f=fixture();await f.flow.begin(event(),f.state);
  assert.equal(f.models.length,0);assert.equal(f.state.pendingTranslation.phase,'awaiting');
  await f.flow.handle(approval(),f.state);await f.flow.running?.promise;
  assert.deepEqual(f.models,['dsh','codex']);assert.equal(f.pdfs.length,1);assert.equal(f.state.pendingTranslation.phase,'completed');
  assert.ok(!JSON.stringify(f.state).includes('不要删除这'));assert.ok(!f.calls.some(([op])=>['start','write-proof','prepare-selected'].includes(op)));
});
test('casual, wrong reply, other sender and old normal approval cannot authorize',async()=>{
  const f=fixture();await f.flow.begin(event(),f.state);
  for(const changes of [{content:'好的'}, {replyToMessageId:'other'}, {senderId:'other'}, {content:'允许本次正文外发'}]){
    await f.flow.handle(event({...approval(),...changes}),f.state);
  }
  assert.equal(f.models.length,0);assert.equal(f.calls.filter(([op,d])=>op==='translation-proof'&&d.proof.kind==='allow_cloud_body_once').length,0);
});
test('denial and changed plan stop without model calls',async()=>{
  const f=fixture();await f.flow.begin(event(),f.state);await f.flow.handle(event({...approval(),content:'暂不翻译'}),f.state);
  assert.equal(f.models.length,0);assert.equal(f.state.pendingTranslation.phase,'denied');
  const g=fixture();await g.flow.begin(event(),g.state);g.state.pendingCuration.planId='other';await g.flow.handle(approval(),g.state);
  assert.equal(g.models.length,0);
});
test('restart invalidates in-flight consent and never resumes automatically',async()=>{
  const f=fixture();await f.flow.begin(event(),f.state);await f.flow.recover({chat:f.state});
  assert.equal(f.state.pendingTranslation.phase,'cancelled');assert.equal(f.models.length,0);
  await f.flow.notifyRecovery(event(),f.state);assert.match(f.sent.at(-1),/机器人重启/);
  const count=f.sent.length;await f.flow.notifyRecovery(event(),f.state);assert.equal(f.sent.length,count);
});
test('shutdown preserves failed and owner-waiting translations; recovery migrates only old restart cancellation',async()=>{
  for(const phase of ['needs_attention','awaiting_owner','completed','delivery_uncertain']){
    const f=fixture();f.state.pendingTranslation={phase,consent:c};await f.flow.cancel(f.state,'restart');
    assert.equal(f.state.pendingTranslation.phase,phase);assert.equal(f.calls.length,0);
  }
  const f=fixture();f.state.workflowV11={id:'flow',phase:'stopped'};
  f.state.pendingTranslation={workflowId:'flow',phase:'cancelled',recoveryNotice:true,consent:c};
  await f.flow.recover({chat:f.state});assert.equal(f.state.pendingTranslation.phase,'needs_attention');assert.equal(f.models.length,0);
  f.state.workflowV11.phase='cancelled';f.state.pendingTranslation.phase='cancelled';
  await f.flow.recover({chat:f.state});assert.equal(f.state.pendingTranslation.phase,'cancelled');
});
test('failed review after two revisions sends no PDF',async()=>{
  const f=fixture();let n=0;
  f.flow.createExecutors=async()=>({translate:async()=>JSON.stringify({translation:'错误译稿'}),review:async()=>{n++;return JSON.stringify({passed:false,checks:{coverage:true,faithfulness:false,numbers:true,terminology:true},issues:['意思不对']});},close:async()=>{}});
  await f.flow.begin(event(),f.state);await f.flow.handle(approval(),f.state);await f.flow.running?.promise;
  assert.equal(n,3);assert.equal(f.pdfs.length,0);assert.equal(f.state.pendingTranslation.phase,'needs_attention');
});
test('owner can stop during a remote call; no next provider or PDF',async()=>{
  const f=fixture();let release;const blocked=new Promise(resolve=>{release=resolve;});
  let started;const startedPromise=new Promise(resolve=>{started=resolve;});
  f.flow.createExecutors=async()=>({translate:async()=>{started();await blocked;return JSON.stringify({translation:'样稿'});},
    review:async()=>{throw Error('must not review');},close:async()=>{release();}});
  await f.flow.begin(event(),f.state);await f.flow.handle(approval(),f.state);const completion=f.flow.running.promise;
  await startedPromise;await f.flow.handle(event({content:'停止翻译'}),f.state);await completion;
  assert.equal(f.pdfs.length,0);assert.equal(f.state.pendingTranslation.phase,'cancelled');
});
test('uncertain delivery does not repeat translation or file send',async()=>{
  const f=fixture();let n=0;f.flow.sendPdf=async()=>{n++;throw Error('uncertain');};
  await f.flow.begin(event(),f.state);await f.flow.handle(approval(),f.state);await f.flow.running?.promise;
  assert.equal(f.state.pendingTranslation.phase,'delivery_uncertain');assert.equal(n,1);
  await f.flow.recover({chat:f.state});assert.equal(n,1);
});
test('consent names both recipients and rejects changed provider',()=>{
  const text=renderTranslationConsent(c);assert.match(text,/DeepSeek 云端/);assert.match(text,/OpenAI 云端/);
  assert.ok(!text.includes(c.consent_hash));assert.ok(!text.includes('SHA-256'));
  assert.throws(()=>renderTranslationConsent({...c,recipient:'openai_codex_cloud'}));
  assert.match(text,/前确认这条提示/);assert.match(text,/处理时间为 1 小时/);
});
test('correctly quoted expired approval explains expiry, never invokes a model',async()=>{
  const f=fixture();await f.flow.begin(event(),f.state);f.flow.now=()=>c.expires_at+1;
  f.state.pendingTranslation.consent.expires_at=Math.floor(Date.now()/1000)-1;
  await f.flow.handle(approval(),f.state);assert.match(f.sent.at(-1),/你引用的是对应的翻译提示/);
  assert.match(f.sent.at(-1),/过期/);assert.match(f.sent.at(-1),/重新发送翻译确认/);assert.equal(f.models.length,0);
});
test('help and renewal quoted to consent are not mistaken for approval',async()=>{
  const f=fixture();await f.flow.begin(event(),f.state);
  await f.flow.handle(event({...approval(),content:'本份翻译提示是什么？我要引用的是哪一个？'}),f.state);
  assert.match(f.sent.at(-1),/长按/);
  assert.equal(await f.flow.handle(event({...approval(),content:'重新发送翻译确认'}),f.state),false);
  assert.equal(f.state.pendingTranslation.phase,'awaiting');assert.equal(f.models.length,0);
});
test('DSH translation profile disables tools, persistence, and automatic retries',()=>{
  for(const id of ['persistent-bash','persistent-pwsh','str-replace-editor','sessions','session-log-deepseek'])assert.equal(translationPatch.find(x=>x.id===id).disabled,true);
  assert.equal(translationPatch.find(x=>x.id==='llm-deepseek').config.retryPolicy.maxRetries,0);
});

test('completed translation requests reuse DSH result without consent or model calls',async()=>{
  for(const text of ['查看译稿','重发中英文对照稿','请把译稿改成中英文对照版','需要完整中文译稿','不要重新翻译，只要已有的译稿']){
    const f=fixture();f.state.pendingTranslation={phase:'completed',planId:'plan',sourceTaskId:'task',chatId:'chat',consent:c};
    await f.flow.begin(event({content:text}),f.state);
    assert.deepEqual(f.calls.map(x=>x[0]),['translation-delivery']);
    assert.equal(f.pdfs.length,1);assert.equal(f.models.length,0);assert.match(f.sent.at(-1),/没有重新翻译/);
  }
});
test('view without a completed copy does not prepare translation; explicit retranslation still needs consent',async()=>{
  const f=fixture();await f.flow.handle(event({content:'查看译稿'}),f.state);
  assert.equal(f.calls.length,0);assert.equal(f.models.length,0);
  f.state.pendingTranslation={phase:'completed',planId:'plan',sourceTaskId:'task',chatId:'chat',consent:c};
  await f.flow.handle(event({content:'重新翻译当前材料'}),f.state);
  assert.ok(f.calls.some(x=>x[0]==='translation-prepare'));assert.equal(f.models.length,0);
  assert.equal(f.state.pendingTranslation.phase,'awaiting');
});
test('redelivery failure, mismatched material and other sender cannot trigger translation',async()=>{
  for(const mode of ['failure','mismatch','sender']){
    const f=fixture();f.state.pendingTranslation={phase:'completed',planId:'plan',sourceTaskId:'task',chatId:'chat',consent:c};
    if(mode==='failure')f.flow.sendPdf=async()=>{throw Error('timeout');};
    if(mode==='mismatch')f.flow.client=async()=>({consent_id:c.consent_id,plan_id:'other'});
    await f.flow.handle(event({content:'重发译稿',...(mode==='sender'?{senderId:'other'}:{})}),f.state);
    assert.equal(f.models.length,0);assert.equal(f.pdfs.length,0);
    assert.ok(!f.calls.some(x=>x[0]==='translation-prepare'));
  }
});
test('quoted, negated and mixed requests do not become explicit retranslation',()=>{
  for(const text of ['不要重新翻译当前材料','“重新翻译当前材料”是什么意思','重新翻译当前材料并覆盖知识库'])assert.equal(isRetranslationRequest(text),false);
  assert.equal(isRetranslationRequest('请重新翻译这份文件。'),true);
  assert.equal(isTranslationDeliveryRequest('查看中英文对照稿'),true);
});
