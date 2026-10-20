import test from 'node:test';
import assert from 'node:assert/strict';
import {failureKind,intentFailureText,workflowFailureText,isWorkflowHelp,workflowHelp,localWorkflowRequest} from '../src/workflow-feedback.mjs';
import {WorkflowRouter} from '../src/workflow-router.mjs';
const now=1789217842;
const state=()=>({pendingCuration:{planId:'plan'},activeCurationRun:{phase:'published'},
  pendingTranslation:{phase:'awaiting',consent:{expires_at:now+1800}}});
const event=content=>({senderId:'owner',chatId:'chat',chatType:'p2p',rawContentType:'text',resources:[],content,messageId:'message',createTime:now*1000});
function fixture(){const sent=[];let modelCalls=0;const router=new WorkflowRouter({ownerId:'owner',now:()=>now,save:async()=>{},
  send:async(_chat,value)=>{sent.push(value.text);return {messageId:'reply'};},classify:async()=>{modelCalls++;throw Error('must not call model');}});
  return {router,sent,get modelCalls(){return modelCalls;}};
}
test('real operation-help messages are local read-only explanations',async()=>{
  for(const text of ['本份翻译提示是什么？我要引用的是哪一个？','我要引用哪一个？','这个提示是什么意思？','下一步怎么操作','我该怎么回复？']){
    assert.equal(isWorkflowHelp(text),true,text);const f=fixture(),s=state(),before=JSON.stringify(s);
    assert.equal((await f.router.handle(event(text),s)).handled,true);assert.equal(f.modelCalls,0);assert.equal(JSON.stringify(s),before);
    assert.match(f.sent[0],/长按/);assert.match(f.sent[0],/重新发送翻译确认/);
  }
});
test('expired, stopped and published explanations offer actionable next steps',()=>{
  const s=state();s.pendingTranslation.consent.expires_at=now-1;
  assert.match(workflowHelp(s,{now}),/过期或停止/);s.pendingTranslation.phase='cancelled';assert.match(workflowHelp(s,{now}),/重新发送翻译确认/);
  delete s.pendingTranslation;assert.match(workflowHelp(s,{now}),/已经入库/);
});
test('real translation, renewal and preview requests avoid a model but grant no authority',async()=>{
  const examples={'请给这份材料补一个中文译本':'translation_copy','重发翻译确认':'translation_copy','重新发送翻译确认':'translation_copy','请重新发送翻译授权提示':'translation_copy',
    '重新发送正文授权':'start_body','我希望先查看一下资料':'preview','先给我看看资料':'preview','翻译进度':'translation_status','状态查看':'status'};
  for(const [text,intent] of Object.entries(examples)){
    const f=fixture(),s=state(),before=JSON.stringify(s);const result=await f.router.handle(event(text),s);
    assert.equal(result.intent,intent,text);assert.equal(result.confirmed,undefined);assert.equal(f.modelCalls,0);assert.equal(JSON.stringify(s),before);
  }
});
test('negation, other materials, mixed requests and quoted instructions do not shortcut',()=>{
  for(const text of ['不要给这份材料补一个中文译本','请给另一份材料补一个中文译本','请给这份材料补一个中文译本，然后入库','原文里说重新发送翻译确认','重新发送翻译确认\n确认本次入库','同意本份翻译和复核','好的'])assert.equal(localWorkflowRequest(text),null,text);
  assert.equal(isWorkflowHelp('第三点是什么意思？'),false);
});
test('timeouts and infrastructure failure never masquerade as unclear language or expose errors',async()=>{
  for(const error of [new Error('Codex 任务超时 secret-token'),new Error('timeout'),Object.assign(new Error('fetch failed'),{cause:{code:'ETIMEDOUT'}})]){
    assert.equal(failureKind(error),'timeout');const f=fixture();f.router.classify=async()=>{throw error;};const s=state(),before=JSON.stringify(s);
    await f.router.handle(event('试着理解一种尚未支持的表达'),s);assert.match(f.sent[0],/调用超时/);assert.doesNotMatch(f.sent[0],/secret-token|没有判断清楚/);assert.equal(JSON.stringify(s),before);
  }
  assert.match(intentFailureText(new Error('secret-provider-response')),/调用失败/);
  assert.doesNotMatch(intentFailureText(new Error('secret-provider-response')),/secret-provider-response|调用超时/);
});
test('read failures and uncertain writes have distinct recovery instructions',()=>{
  const e=new Error('timeout');assert.match(workflowFailureText(e,{readOnly:true}),/查看或查询调用超时/);
  assert.doesNotMatch(workflowFailureText(e,{readOnly:true}),/请勿重复批准|执行结果尚未确认/);
  assert.match(workflowFailureText(e,{writeAttempt:true}),/执行结果尚未确认/);
});
