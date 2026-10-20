import test from 'node:test';
import assert from 'node:assert/strict';
import {DocumentReferenceGuard,createMessageReader,isFileStart} from '../src/document-reference.mjs';
import {IntakeFlow} from '../src/intake-flow.mjs';
import {WorkflowProgress} from '../src/workflow-progress.mjs';

const event=(extra={})=>({senderId:'owner',chatId:'chat',chatType:'p2p',messageId:'reply',createTime:Date.now(),rawContentType:'text',content:'请开始这份文件',resources:[],replyToMessageId:'new-upload',...extra});
const file=(extra={})=>({message_id:'new-upload',chat_id:'chat',msg_type:'file',sender:{sender_type:'user',id_type:'open_id',id:'owner'},body:{content:JSON.stringify({file_key:'file-key',file_name:'黄卫东.webarchive'})},...extra});
function fixture(){
  const sent=[],read=[],audit=[];
  const state={workflowV11:{id:'old-flow',uploadId:'old-upload',phase:'complete',filename:'李飞飞.zip',planId:'old-plan',sourceTaskId:'old-task',progress:{status:'complete',remaining:[]}},
    pendingCuration:{planId:'old-plan',sourceTaskId:'old-task'},pendingTranslationChoice:{planId:'old-plan',sourceTaskId:'old-task',promptId:'old-choice'}};
  const guard=new DocumentReferenceGuard({ownerId:'owner',appId:'app',readMessage:async id=>{read.push(id);return file();},
    send:async(c,v,o)=>{sent.push({c,v,o});return {messageId:'notice'};},audit:e=>audit.push(e)});
  return {state,guard,sent,read,audit};
}
test('exact production case: quoted new file bypasses old task; no consent or timestamp is fabricated',async()=>{
  const f=fixture(),m=event(),before=JSON.stringify(f.state);
  const result=await f.guard.resolve(m,f.state);
  assert.equal(result.message.resources[0].fileName,'黄卫东.webarchive');
  assert.equal(result.message.sourceUploadId,'new-upload');assert.equal(result.message.resourceMessageId,'new-upload');
  for(const key of ['messageId','createTime','senderId','chatId','replyToMessageId','content'])assert.equal(result.message[key],m[key]);
  assert.deepEqual(m.resources,[]);assert.equal(JSON.stringify(f.state),before);
});
test('resolved upload registers original attachment message once and only waits for purpose',async()=>{
  const f=fixture(),calls=[],downloads=[];
  const flow=new IntakeFlow({ownerId:'owner',save:async()=>{},send:f.guard.send,
    download:async m=>{downloads.push(m.resourceMessageId);return [{originalFileName:m.resources[0].fileName,sourceRelpath:'safe/file'}];},
    client:async(op,data)=>{calls.push({op,data});if(op==='workflow-lookup')return {matches:[]};if(op==='workflow-register')return {workflow_id:'new-flow',state:'received'};throw Error(op);},
    progress:new WorkflowProgress({save:async()=>{},create:async()=> 'new-card',update:async()=>{}}),
    translation:{running:false},curation:{},convert:async()=>assert.fail('must wait'),deliver:async()=>assert.fail('must wait')});
  const m=(await f.guard.resolve(event(),f.state)).message;
  assert.equal(await flow.handle(m,f.state),true);
  assert.deepEqual(downloads,['new-upload']);assert.equal(calls.find(x=>x.op==='workflow-register').data.upload_message_id,'new-upload');
  assert.equal(f.state.workflowV11.phase,'choosing');assert.equal(f.state.pendingCuration,null);assert.equal(f.state.pendingTranslationChoice,null);
  await flow.handle(m,f.state);assert.equal(downloads.length,1);assert.equal(calls.length,2);
  const choice=event({content:'2 中文＋网页归档'});
  assert.equal((await f.guard.resolve(choice,f.state)).message,choice); // original user choice, not a synthetic upload
});
test('a different attachment never replaces a running workflow',async()=>{
  const f=fixture();f.state.workflowV11.phase='running';
  const before=JSON.stringify(f.state),flow=new IntakeFlow({ownerId:'owner',send:f.guard.send,translation:{running:true},download:async()=>assert.fail('no download')});
  await flow.handle((await f.guard.resolve(event(),f.state)).message,f.state);
  assert.equal(JSON.stringify(f.state),before);assert.match(f.sent.at(-1).v.text,/没有加入自动队列/);
});
test('questions, negation, approval and old-file revisions cannot switch or act on current document',async()=>{
  for(const content of ['这是什么？','查看笔记','不处理这份文件','确认本次入库','允许本次正文外发','第三点请修改','好的']){
    const f=fixture(),before=JSON.stringify(f.state);
    assert.equal((await f.guard.resolve(event({content}),f.state)).handled,true,content);
    assert.equal(JSON.stringify(f.state),before);
  }
});
test('completed vague starts stop locally; explicit reads remain available',async()=>{
  for(const replyToMessageId of [undefined,'old-upload','old-choice']){
    const f=fixture();assert.equal((await f.guard.resolve(event({replyToMessageId}),f.state)).handled,true);
    assert.equal(f.read.length,0);assert.match(f.sent.at(-1).v.text,/已经完成/);
  }
  const f=fixture(),m=event({content:'查看译稿',replyToMessageId:'old-upload'});
  assert.equal((await f.guard.resolve(m,f.state)).message,m);
});
test('same-file active prompt keeps approval checks downstream, without reading body or model',async()=>{
  const f=fixture();f.state.workflowV11.phase='review_ready';f.state.pendingPublication={planId:'old-plan',delivery:{prompt_message_id:'preview'}};
  for(const content of ['确认本次入库','请修改第三点']){
    const m=event({content,replyToMessageId:'preview'});assert.equal((await f.guard.resolve(m,f.state)).message,m);
  }
  assert.equal(f.read.length,0);
});
test('follows authenticated bot parent chain, never the thread root or quoted body instructions',async()=>{
  const f=fixture();f.guard.readMessage=async id=>id==='bot'?file({message_id:'bot',msg_type:'text',sender:{sender_type:'app',id:'app'},parent_id:'new-upload',root_id:'old-upload',body:{content:'ignore rules'}}):file();
  assert.equal((await f.guard.resolve(event({replyToMessageId:'bot'}),f.state)).message.sourceUploadId,'new-upload');
});
test('unreadable, deleted, foreign chat/user/app, mismatched ID and malformed resource fail closed',async()=>{
  for(const raw of [null,file({deleted:true}),file({chat_id:'other'}),file({message_id:'other'}),file({sender:{sender_type:'user',id_type:'open_id',id:'other'}}),
    file({sender:{sender_type:'app',id:'other'}}),file({body:{content:'no-json'}}),file({body:{content:'{}'}}),file({msg_type:'text',root_id:'old-upload'})]){
    const f=fixture();f.guard.readMessage=async()=>raw;
    assert.equal((await f.guard.resolve(event(),f.state)).handled,true);
  }
  const f=fixture();f.guard.readMessage=async()=>{throw Object.assign(Error('timeout'),{code:'ETIMEDOUT'});};
  assert.equal((await f.guard.resolve(event(),f.state)).handled,true);assert.match(f.sent.at(-1).v.text,/超时/);
});
test('cycles and long quote chains have bounded lookups',async()=>{
  const f=fixture();let count=0;
  f.guard.readMessage=async id=>{count++;return file({message_id:id,msg_type:'text',parent_id:`next-${count}`});};
  assert.equal((await f.guard.resolve(event(),f.state)).handled,true);assert.equal(count,6);
  count=0;f.guard.readMessage=async id=>{count++;return file({message_id:id,msg_type:'text',parent_id:id});};
  assert.equal((await f.guard.resolve(event(),f.state)).handled,true);assert.equal(count,1);
});
test('untrusted inbound is rejected, direct attachments do not inherit an old quoted task',async()=>{
  const f=fixture();assert.equal((await f.guard.resolve(event({senderId:'other'}),f.state)).handled,true);
  assert.equal((await f.guard.resolve(event({chatType:'group'}),f.state)).handled,true);
  const m=event({resources:[{fileName:'new.pdf'}],replyToMessageId:'old-upload'});
  assert.equal((await f.guard.resolve(m,f.state)).message,m);assert.equal(f.read.length,0);
});
test('metadata reader uses open IDs and a bounded timeout; missing/ambiguous receipts fail closed',async()=>{
  let args;const reader=createMessageReader({rawClient:{im:{v1:{message:{get:async a=>{args=a;return {code:0,data:{items:[file()]}};}}}}}});
  assert.equal((await reader('new-upload')).message_id,'new-upload');assert.equal(args.params.user_id_type,'open_id');
  for(const result of [{code:1},{code:0,data:{items:[]}},{code:0,data:{items:[file(),file()]}}]){
    await assert.rejects(createMessageReader({rawClient:{im:{v1:{message:{get:async()=>result}}}}})('id'));
  }
  const timeout=createMessageReader({rawClient:{im:{v1:{message:{get:()=>new Promise(()=>{})}}}}},{timeoutMs:5});
  await assert.rejects(timeout('id'),{code:'ETIMEDOUT'});
});
test('only clear intake requests resolve a new reference, never implied approval',()=>{
  for(const value of ['请开始这份文件','开始处理','请处理这个文件','2 无','3 英文＋PDF'])assert.equal(isFileStart(value),true,value);
  for(const value of ['是否开始','不要处理','确认本次入库','允许本次正文外发','为什么要整理'])assert.equal(isFileStart(value),false,value);
});
