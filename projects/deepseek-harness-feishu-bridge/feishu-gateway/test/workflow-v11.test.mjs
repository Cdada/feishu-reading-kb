import test from 'node:test';
import assert from 'node:assert/strict';
import {IntakeFlow,parseIntakeChoice} from '../src/intake-flow.mjs';
import {WorkflowProgress,initialStages,progressText} from '../src/workflow-progress.mjs';
import {translateAndReview} from '../src/translation-loop.mjs';
import {TranslationFlow} from '../src/translation-flow.mjs';
const event=(content='',extra={})=>({senderId:'owner',chatId:'chat',chatType:'p2p',messageId:'message',createTime:Date.now(),rawContentType:'text',content,resources:[],...extra});

test('purpose selection does not treat questions or negation as authorization',()=>{
  for(const value of ['好的','无','可以翻译入库吗？','不要入库','是否只转换','为什么要入库','确认本次入库','查看入库预览','允许本次正文外发'])assert.equal(parseIntakeChoice(value),null,value);
  assert.deepEqual(parseIntakeChoice('3 英文＋PDF'),{purpose:3,language:'英文',document_profile:'general'});
  assert.equal(parseIntakeChoice('只转换格式').purpose,1);
  assert.equal(parseIntakeChoice('请翻译并入库').purpose,3);
});
test('remaining counts include current, conditional revisions and truthful complete',async()=>{
  assert.equal(initialStages(1).length,3);assert.equal(initialStages(2).length,5);assert.equal(initialStages(3).length,9);
  const state={workflowV11:{chatId:'chat',progress:{filename:'test.pdf',purpose:3,stage:'1',status:'running',remaining:initialStages(3)}}};
  let creates=0,updates=0;const p=new WorkflowProgress({save:async()=>{},create:async()=>{creates++;return 'card';},update:async()=>{updates++;}});
  await p.set(state,{});await p.stage(state,'4.1',{done:['1','2','3']});
  assert.equal(state.workflowV11.progress.remaining.length,6);
  await p.stage(state,'5.2',{remaining:['5.2','5.3','5.4','6']});
  assert.match(progressText(state.workflowV11.progress),/还有 4 个步骤/);
  await p.stage(state,'6',{remaining:['6']});assert.equal(state.workflowV11.progress.remaining.length,1);
  await p.set(state,{status:'complete',remaining:[]});assert.equal(creates,1);assert.equal(updates,4);
});
test('uncertain card creation never creates another card automatically',async()=>{
  let calls=0;const p=new WorkflowProgress({save:async()=>{},create:async()=>{calls++;throw Error('timeout');},update:async()=>{}});
  const state={workflowV11:{chatId:'chat',progress:{filename:'test'}}};
  await p.set(state,{});await p.set(state,{stage:'2'});assert.equal(calls,1);
});
function fixture(purpose=3){
  const calls=[],messages=[],state={};
  const progress=new WorkflowProgress({save:async()=>{},create:async()=> 'card',update:async()=>{}});
  const client=async(op,data)=>{calls.push(op);
    if(op==='workflow-lookup')return {matches:[]};
    if(op==='workflow-register')return {workflow_id:'flow',state:'received'};
    if(op==='workflow-select')return {workflow_id:'flow'};
    if(op==='workflow-bind')return {workflow_id:'flow',source_task_id:'task',plan_id:'plan'};
    if(op==='workflow-grant')return {consent:{consent_id:'consent'}};
    if(op==='workflow-metadata')return {undecided:[],conversion_text_checks:[]};
    return {};
  };
  const flow=new IntakeFlow({client,ownerId:'owner',save:async()=>{},send:async(c,v)=>{messages.push(v.text);return {messageId:'sent'};},
    download:async()=>[{fileName:'01-test.pdf',originalFileName:'test.pdf',sourceRelpath:'date/id/01-test.pdf'}],
    convert:async()=>{calls.push('convert');return {taskId:'task',deliveryFiles:[{}]};},
    deliver:async()=>calls.push('deliver'),curation:{approved:async()=>calls.push('notes'),cancel:async()=>{}},
    translation:{beginWorkflow:async()=>calls.push('translation'),cancel:async()=>{}},progress});
  return {flow,calls,messages,state,purpose};
}
test('one choice grants same-file notes; translation only after committed callback',async()=>{
  const f=fixture();await f.flow.handle(event('',{resources:[{}]}),f.state);
  assert.ok(!f.calls.includes('convert'));await f.flow.handle(event('3 英文 PDF'),f.state);await Promise.all([...f.flow.jobs]);
  assert.ok(f.calls.includes('notes'));assert.ok(!f.calls.includes('translation'));
  await f.flow.published(event(),f.state,{plan_id:'other',publication_id:'pub'});assert.ok(!f.calls.includes('translation'));
  await f.flow.published(event(),f.state,{plan_id:'plan',publication_id:'pub'});assert.equal(f.calls.at(-1),'translation');
});
test('publication sends one standalone transition notice before translation and not for notes only',async()=>{
  const f=fixture();await f.flow.handle(event('',{resources:[{}]}),f.state);
  Object.assign(f.state.workflowV11,{planId:'plan',choice:{purpose:3}});
  let noticesAtStart=0;
  f.flow.translation.beginWorkflow=async()=>{noticesAtStart=f.messages.filter(x=>x.includes('你现在不需要操作')).length;};
  await f.flow.published(event(),f.state,{plan_id:'other',publication_id:'pub'});
  assert.equal(noticesAtStart,0);
  await f.flow.published(event(),f.state,{plan_id:'plan',publication_id:'pub'});
  assert.equal(noticesAtStart,1);
  await f.flow.published(event(),f.state,{plan_id:'plan',publication_id:'pub'});
  assert.equal(noticesAtStart,1);
  assert.equal(f.state.workflowV11.translationHandoffNoticeUncertain,false);
  const g=fixture();await g.flow.handle(event('',{resources:[{}]}),g.state);
  Object.assign(g.state.workflowV11,{planId:'plan',choice:{purpose:2}});
  await g.flow.published(event(),g.state,{plan_id:'plan',publication_id:'pub'});
  assert.ok(!g.messages.some(x=>x.includes('你现在不需要操作')));
});
test('uncertain transition notification does not block translation or resend on replay',async()=>{
  const f=fixture();await f.flow.handle(event('',{resources:[{}]}),f.state);
  Object.assign(f.state.workflowV11,{planId:'plan',choice:{purpose:3}});
  let attempts=0;f.flow.send=async()=>{attempts++;throw Error('send timeout');};
  await f.flow.published(event(),f.state,{plan_id:'plan',publication_id:'pub'});
  assert.ok(f.calls.includes('translation'));assert.equal(f.state.workflowV11.translationHandoffNoticeUncertain,true);
  await f.flow.published(event(),f.state,{plan_id:'plan',publication_id:'pub'});
  assert.equal(attempts,1);
});
test('conversion only terminates after delivery, new upload never replaces running document',async()=>{
  const f=fixture(1);await f.flow.handle(event('',{resources:[{}]}),f.state);
  await f.flow.handle(event('',{resources:[{}],messageId:'other'}),f.state);assert.equal(f.calls.filter(x=>x==='workflow-register').length,1);
  await f.flow.handle(event('1 无'),f.state);await Promise.all([...f.flow.jobs]);
  assert.ok(f.calls.includes('deliver'));assert.ok(!f.calls.includes('notes'));assert.equal(f.state.workflowV11.phase,'complete');
});
test('artifact-role judgment precedes authorization; questions do not mutate roles',async()=>{
  const f=fixture();let classified=false;const client=f.flow.client;
  f.flow.client=async(op,data)=>op==='workflow-metadata'?{undecided:classified?[]:[{name:'附录说明.md'}]}:client(op,data);
  f.flow.classifyArtifacts=async()=>{f.calls.push('classify');classified=true;};
  await f.flow.handle(event('',{resources:[{}]}),f.state);await f.flow.handle(event('2 无'),f.state);await Promise.all([...f.flow.jobs]);
  assert.ok(f.calls.indexOf('classify')<f.calls.indexOf('workflow-grant'));
  f.state.workflowV11.phase='artifact_question';const count=f.calls.length;
  await f.flow.handle(event('这是什么意思？'),f.state);assert.equal(f.calls.length,count);
});
test('generic owner stop cancels the persisted workflow, not only the old legacy handler',async()=>{
  const f=fixture();await f.flow.handle(event('',{resources:[{}]}),f.state);
  await f.flow.handle(event('停止'),f.state);
  assert.equal(f.state.workflowV11.phase,'cancelled');assert.ok(f.calls.includes('workflow-cancel'));
});
test('explicit ZIP retry uses saved upload and default DSH; vague acknowledgements do not retry',async()=>{
  const f=fixture();await f.flow.handle(event('',{resources:[{}]}),f.state);
  Object.assign(f.state.workflowV11,{filename:'article.zip',phase:'fallback',choice:{purpose:3}});
  await f.flow.handle(event('好的'),f.state);assert.ok(!f.calls.includes('convert'));
  await f.flow.handle(event('重新处理图片压缩包'),f.state);await Promise.all([...f.flow.jobs]);
  assert.ok(f.calls.includes('convert'));assert.equal(f.state.workflowV11.executor,null);
  assert.equal(f.calls.filter(x=>x==='workflow-register').length,1);
});
test('converted handoff waits across restart and resumes notes without OCR or translation',async()=>{
  const f=fixture();await f.flow.handle(event('',{resources:[{}]}),f.state);
  Object.assign(f.state.workflowV11,{phase:'converted_ready',sourceTaskId:'task',convertedHandoff:true,choice:{purpose:3}});
  await f.flow.recover({chat:f.state});assert.equal(f.state.workflowV11.phase,'converted_ready');
  for(const text of ['好的','可以继续整理这份资料吗？','不要继续整理这份资料'])await f.flow.handle(event(text),f.state);
  assert.ok(!f.calls.includes('notes'));assert.ok(!f.calls.includes('workflow-grant'));
  await f.flow.handle(event('继续整理这份资料'),f.state);await Promise.all([...f.flow.jobs]);
  assert.ok(f.calls.includes('notes'));assert.ok(!f.calls.includes('convert'));assert.ok(!f.calls.includes('translation'));
});
test('known layout with no prose stops at conversion without cloud classification or grant',async()=>{
  const f=fixture();const client=f.flow.client;
  f.flow.client=async(op,data)=>op==='workflow-metadata'?{undecided:[],conversion_text_checks:[{state:'text_missing'}]}:client(op,data);
  f.flow.classifyArtifacts=async()=>f.calls.push('classify');
  await f.flow.handle(event('',{resources:[{}]}),f.state);await f.flow.handle(event('2 无'),f.state);await Promise.all([...f.flow.jobs]);
  assert.equal(f.state.workflowV11.phase,'text_required');assert.equal(f.state.workflowV11.progress.stage,'3');
  assert.ok(!f.calls.includes('classify'));assert.ok(!f.calls.includes('workflow-grant'));assert.ok(!f.calls.includes('notes'));
  assert.match(f.messages.at(-1),/补做本机文字识别/);
  assert.equal(f.state.workflowV11.progress.remaining.length,3);
});
test('recheck classification reuses conversion; questions and vague continue do not execute',async()=>{
  const f=fixture();await f.flow.handle(event('',{resources:[{}]}),f.state);
  Object.assign(f.state.workflowV11,{phase:'artifact_question',sourceTaskId:'task',choice:{purpose:2},planId:'plan'});
  await f.flow.handle(event('为什么需要判断？'),f.state);await f.flow.handle(event('继续'),f.state);
  assert.ok(!f.calls.includes('workflow-grant'));
  await f.flow.handle(event('重新检查转换结果'),f.state);await Promise.all([...f.flow.jobs]);
  assert.ok(!f.calls.includes('convert'));assert.ok(f.calls.includes('notes'));
});
test('restart refreshes incomplete text status locally and never starts cloud or conversion',async()=>{
  const f=fixture();await f.flow.handle(event('',{resources:[{}]}),f.state);
  Object.assign(f.state.workflowV11,{phase:'artifact_question',sourceTaskId:'task',choice:{purpose:3}});
  const client=f.flow.client;
  f.flow.client=async(op,data)=>op==='workflow-metadata'?{undecided:[],conversion_text_checks:[{state:'normalization_required'}]}:client(op,data);
  await f.flow.recover({chat:f.state});
  assert.equal(f.state.workflowV11.phase,'text_required');assert.equal(f.flow.jobs.size,0);
  assert.ok(!f.calls.includes('convert'));assert.ok(!f.calls.includes('workflow-grant'));
});
test('startup check failure remains recheckable; old stopped state needs server no-run proof',async()=>{
  const f=fixture();await f.flow.handle(event('',{resources:[{}]}),f.state);
  Object.assign(f.state.workflowV11,{phase:'artifact_question',sourceTaskId:'task',choice:{purpose:2}});
  f.flow.client=async()=>{throw Error('service starting');};await f.flow.recover({chat:f.state});
  assert.equal(f.state.workflowV11.phase,'artifact_question');assert.match(f.state.workflowV11.progress.detail,/重新检查转换结果/);
  for(const allowed of [false,true]){
    f.state.workflowV11.phase='stopped';f.state.workflowV11.progress.stage='4.1';
    f.flow.client=async()=>({classification_retry_allowed:allowed,undecided:[],conversion_text_checks:[{state:'text_missing'}]});
    await f.flow.recover({chat:f.state});assert.equal(f.state.workflowV11.phase,allowed?'text_required':'stopped');
  }
  assert.equal(f.flow.jobs.size,0);
});
test('two failed repairs preserve full draft for owner, not a fake machine pass',async()=>{
  const checkpoints=[];let translations=0,reviews=0;
  const result=await translateAndReview({source:'Original text.',notes:'Terminology only.',allowOwnerDecision:true,authorize:async()=>{},checkpoint:async c=>checkpoints.push(c),
    translate:async()=>{translations++;return JSON.stringify({translation:'中文稿'});},review:async()=>{reviews++;return JSON.stringify({passed:false,checks:{coverage:true,faithfulness:false,numbers:true,terminology:true},issues:['意义需核对']});}});
  assert.equal(translations,3);assert.equal(reviews,3);assert.equal(result.pendingOwner,true);
  assert.deepEqual(checkpoints.at(-1),{phase:'owner_pending',revision:2});
});
test('translation workflow delivers then links; link failure never marks all complete',async()=>{
  for(const broken of [false,true]){
    const calls=[],state={pendingCuration:{planId:'plan',sourceTaskId:'task'},workflowV11:{id:'flow',planId:'plan',sourceTaskId:'task',phase:'running',progress:{}}};let pulled=false;
    const flow=new TranslationFlow({ownerId:'owner',signingKey:Buffer.alloc(32),save:async()=>{},send:async()=>({messageId:'notice'}),
      progress:{stage:async()=>{},set:async(_s,data)=>calls.push(data.status)},sendPdf:async()=>{calls.push('pdf');return {messageId:'pdf'};},
      createExecutors:async()=>({translate:async()=>JSON.stringify({translation:'中文'}),review:async()=>JSON.stringify({passed:true,checks:{coverage:true,faithfulness:true,numbers:true,terminology:true},issues:[]}),close:async()=>{}}),
      client:async op=>{
        calls.push(op);if(op==='workflow-grant')return {plan_id:'plan',source_task_id:'task',consent:{consent_id:'translation'}};
        if(op==='translation-check')return {consent_id:'translation',active:true};
        if(op==='translation-pull'){if(pulled)return {consent_id:'translation',done:true};pulled=true;return {consent_id:'translation',index:0,total:1,source:'original'};}
        if(op==='translation-record')return {consent_id:'translation',saved:true};
        if(op==='translation-finish')return {consent_id:'translation',state:'completed',formal_write:false,pdf_base64:'pdf'};
        if(op==='workflow-link'){if(broken)throw Error('LINK_FAILED');return {consent_id:'translation',state:'linked'};}
        return {};
      }});
    await flow.beginWorkflow(event(),state,state.workflowV11);await flow.running?.promise;
    assert.ok(calls.indexOf('pdf')<calls.indexOf('workflow-link'));
    assert.equal(state.workflowV11.phase==='complete',!broken);
  }
});
