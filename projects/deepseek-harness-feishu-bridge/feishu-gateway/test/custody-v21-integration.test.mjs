import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtemp,readFile,writeFile,mkdir,rm} from 'node:fs/promises';
import {join,resolve} from 'node:path';
import {fileURLToPath} from 'node:url';
import {IntakeFlow} from '../src/intake-flow.mjs';
import {createGatewayIntakeFlow} from '../src/gateway-intake.mjs';
import {createAttachmentReceiver,saveInboundResources} from '../src/attachment-store.mjs';
import {CustodyDevBridge,createDevAttachmentClient} from '../src/custody-v21-dev.mjs';

const worktree=resolve(fileURLToPath(new URL('../../../..',import.meta.url)));
const parent=join(worktree,'.dev-runtime/kb-v21/test-output');
const content=Buffer.from('%PDF synthetic gateway original');
const message=(id='upload')=>({messageId:id,chatId:'mock-chat',chatType:'p2p',senderId:'owner',
  content:'',resources:[{type:'file',fileKey:'mock-key',fileName:'gateway.pdf',sizeBytes:content.length}]});

test('Pilot actual gateway accepts only fixed group and restarts the existing conversion',async()=>{
  const runtime=await mkdtemp(join(parent,'pilot-gateway-'));
  const config=join(runtime,'pilot.local.json');
  await writeFile(config,JSON.stringify({scope:{app_id:'mock-app',chat_id:'mock-chat',user_id:'owner'},max_attachments:5,max_pending:8}));
  const bytes=Buffer.from('# Synthetic Pilot\n\nOriginal local text.\n');
  const channel=createDevAttachmentClient({'mock-key':{bytes}});
  const bridge=new CustodyDevBridge({worktree,runtime,pilotConfig:config});
  const unexpected=async()=>{throw Error('no V1, send, or model call');};
  const flow=createGatewayIntakeFlow({mode:'v2.1-dev',channel,custodyBridge:bridge,ownerId:'owner',save:async()=>{},
    client:unexpected,send:unexpected,curation:{},translation:{},progress:{}});
  const m={...message('pilot-message'),appId:'mock-app',chatType:'group',
    resources:[{type:'file',fileKey:'mock-key',fileName:'synthetic.md',sizeBytes:bytes.length}]};
  await assert.rejects(flow.handle({...m,senderId:'outsider'},{}),/configured application/);
  assert.equal(channel.downloadCalls.length,0);
  const state={};await flow.handle(m,state);
  const entry=state.workflowV21.attachments[0].entry_id;
  const before=await bridge.processingStatus(entry);
  assert.equal(before.processing_state,'ready_candidate');
  delete channel.fixtures['mock-key'];
  await flow.handle(m,state);
  const reopened=new CustodyDevBridge({worktree,runtime,pilotConfig:config});
  assert.equal((await reopened.processingStatus(entry)).task_id,before.task_id);
  assert.equal((await reopened.processNext()).state,'idle');
  assert.equal(channel.downloadCalls.length,1);
  assert.equal((await bridge.snapshot()).mock_calls.length,0);
});

async function fixture(name){
  const runtime=await mkdtemp(join(parent,`gateway-${name}-`));
  const channel=createDevAttachmentClient({'mock-key':{bytes:content}});
  const bridge=new CustodyDevBridge({worktree,runtime});
  const receiver=createAttachmentReceiver({mode:'v2.1-dev',channel,custodyBridge:bridge});
  const state={}, calls=[];
  const unexpected=async()=>{throw Error('V1 or S7 path must not run for staged V2.1 intake');};
  const flow=createGatewayIntakeFlow({mode:'v2.1-dev',channel,custodyBridge:bridge,ownerId:'owner',save:async()=>calls.push('save'),
    client:unexpected,send:unexpected,download:unexpected,convert:unexpected,
    curation:{},translation:{},progress:{}});
  return {runtime,channel,bridge,receiver,state,flow,calls,
    evidence:async()=>{const data=await bridge.snapshot();await writeFile(join(runtime,'test-output/gateway-observed.json'),JSON.stringify(data,null,2));return data;}};
}

test('V2.1 actual IntakeFlow stages one attachment and freezes downstream IDs',async()=>{
  const f=await fixture('single');
  assert.equal(await f.flow.handle({...message(),resources:[],content:'2 无'},f.state),true);
  assert.equal(f.state.workflowV11,undefined);
  assert.equal(await f.flow.handle(message(),f.state),true);
  const binding=f.state.workflowV21.attachments[0];
  assert.equal(binding.schema_version,'staging-input/v2.1');
  assert.match(binding.asset_id,/^asset_/);
  assert.match(binding.acceptance_id,/^acceptance_/);
  assert.equal(binding.sourceRelpath,undefined);
  assert.equal(binding.path,undefined);
  assert.equal(f.channel.downloadCalls.length,1);
  assert.deepEqual(await f.receiver.readInput(binding),content);
  await assert.rejects(f.receiver.readInput({...binding,asset_id:'asset_'+'0'.repeat(32)}),/identity mismatch/);
  await f.flow.handle({...message(),resources:[],content:'2 无'},f.state);
  const data=await f.evidence();
  assert.equal(data.staging_receipts[0].status,'accepted');
  assert.equal(data.staging_attempts.length,1);
  assert.equal(data.mock_deliveries.length,1);
  assert.equal(data.mock_deliveries[0].target_chat_id,'mock-chat');
});

test('V2.1 actual IntakeFlow transport interruption fails safely and repairs once',async()=>{
  const f=await fixture('interrupted');
  f.channel.fixtures['mock-key'].failAfterFirstChunk=true;
  await assert.rejects(f.flow.handle(message(),f.state),/transport interrupted/);
  let data=await f.evidence();
  assert.equal(data.staging_receipts[0].status,'failed');
  assert.equal(data.custody_events.filter(e=>e.event_type==='source_staging_accepted').length,0);
  assert.equal(data.mock_deliveries.length,1);
  delete f.channel.fixtures['mock-key'].failAfterFirstChunk;
  await f.flow.handle(message(),f.state);
  data=await f.evidence();
  assert.equal(data.staging_attempts.length,2);
  assert.equal(data.assets.length,1);
  assert.equal(data.mock_deliveries.length,2);
});

test('V2.1 gateway replay/restart never downloads accepted deleted resource',async()=>{
  const f=await fixture('replay');
  await f.flow.handle(message(),f.state);
  const old=structuredClone(f.state.workflowV21.attachments[0]);
  delete f.channel.fixtures['mock-key'];
  await rm(join(f.runtime,'inbound'),{recursive:true}); // this synthetic transport only
  await mkdir(join(f.runtime,'inbound'));
  await f.flow.handle(message(),f.state);
  assert.deepEqual(f.state.workflowV21.attachments[0],old);
  const newReceiver=createAttachmentReceiver({mode:'v2.1-dev',channel:f.channel,
    custodyBridge:new CustodyDevBridge({worktree,runtime:f.runtime})});
  assert.deepEqual((await newReceiver.receive(message()))[0],old);
  assert.deepEqual(await newReceiver.readInput(old),content);
  assert.equal(f.channel.downloadCalls.length,1);
  const data=await f.evidence();
  assert.equal(data.assets.length,1);assert.equal(data.intake_entries.length,1);
  assert.equal(data.staging_attempts.length,1);assert.equal(data.mock_deliveries.length,1);
});

test('V2.1 gateway concurrent replay downloads only once under custody lock',async()=>{
  const f=await fixture('concurrent');
  const results=await Promise.all([f.receiver.receive(message()),f.receiver.receive(message())]);
  assert.deepEqual(results[0],results[1]);
  assert.equal(f.channel.downloadCalls.length,1);
  const data=await f.evidence();assert.equal(data.mock_deliveries.length,1);
});

test('V2.1 gateway explicit same-intake group emits one group success',async()=>{
  const f=await fixture('group');
  f.channel.fixtures['second-key']={bytes:content};
  const m=message();m.resources.push({...m.resources[0],fileKey:'second-key',fileName:'second.pdf'});
  m.sourceGroup={source_group_id:'group_'+'a'.repeat(32),membership_revision:1,required_indices:[0,1]};
  await f.flow.handle(m,f.state);
  const data=await f.evidence();
  assert.equal(data.staging_receipts.length,2);
  assert.equal(data.mock_deliveries.length,1);
  assert.match(data.mock_deliveries[0].payload,/本组必需/);
  assert.equal(data.notification_outbox[0].intake_id,data.intakes[0].intake_id);
});

test('V2.1 gateway optional transport failure has entry scope and preserves required promise',async()=>{
  const f=await fixture('optional-failure');
  f.channel.fixtures['optional-key']={bytes:content,failAfterFirstChunk:true};
  const m=message();m.resources.push({...m.resources[0],fileKey:'optional-key',fileName:'optional.pdf'});
  m.sourceGroup={source_group_id:'group_'+'b'.repeat(32),membership_revision:1,required_indices:[0]};
  await assert.rejects(f.flow.handle(m,f.state),/transport interrupted/);
  let data=await f.evidence();
  const optional=data.source_group_memberships.find(row=>row.required===0);
  const receipt=data.intake_custody.find(row=>row.entry_id===optional.entry_id);
  assert.equal(receipt.source_staging_status,'failed');
  assert.equal(receipt.feishu_attachment_retention,'retain_required');
  const required=data.intake_custody.find(row=>row.entry_id!==optional.entry_id);
  assert.equal(required.feishu_attachment_retention,'cleanup_eligible');
  const groupEvents=data.custody_events.filter(row=>row.event_type==='source_group_staging_accepted');
  assert.equal(groupEvents.length,1);
  assert.equal(data.notification_outbox.filter(row=>row.scope_type==='source_group').length,1);
  const failures=data.notification_outbox.filter(row=>row.kind==='failure');
  assert.equal(failures.length,1);
  assert.equal(failures[0].scope_type,'single_source');
  assert.equal(failures[0].scope_id,receipt.acceptance_id);
  assert.equal(data.mock_deliveries.filter(row=>/本组必需/.test(row.payload)).length,1);
  // Keep the failed observation before replay/repair overwrites the final snapshot.
  await writeFile(join(f.runtime,'test-output/gateway-optional-failed.json'),JSON.stringify(data,null,2));
  await assert.rejects(f.flow.handle(m,f.state),/transport interrupted/);
  data=await f.evidence();
  assert.equal(data.notification_outbox.filter(row=>row.kind==='failure').length,1);
  delete f.channel.fixtures['optional-key'].failAfterFirstChunk;
  await f.flow.handle(m,f.state);
  await f.flow.handle(m,f.state);
  data=await f.evidence();
  assert.equal(data.intake_custody.find(row=>row.entry_id===optional.entry_id).feishu_attachment_retention,'cleanup_eligible');
  assert.deepEqual(data.custody_events.filter(row=>row.event_type==='source_group_staging_accepted'),groupEvents);
  assert.equal(data.notification_outbox.filter(row=>row.scope_type==='single_source' && row.kind==='success').length,1);
  assert.equal(data.mock_deliveries.length,3); // required success, optional failure, optional success
});

test('V2.1 safety rejects an actual-looking client and production runtime',async()=>{
  const f=await fixture('safety');
  assert.throws(()=>createAttachmentReceiver({mode:'v2.1-dev',channel:{rawClient:{}},custodyBridge:f.bridge}),/mock/);
  assert.throws(()=>createAttachmentReceiver({mode:'v2.1-real',channel:f.channel}),/mode/);
  const bad=new CustodyDevBridge({worktree,runtime:join(worktree,'production')});
  await assert.rejects(bad.prepare(message()),/DEV test-output/);
  assert.equal(f.channel.downloadCalls.length,0);
  await f.evidence();
});

test('V1 default attachment receiver preserves inbound interface and feature-off IntakeFlow',async()=>{
  const f=await fixture('v1-default');
  const inboxDir=join(f.runtime,'inbound-v1');
  const v1=createAttachmentReceiver({channel:f.channel,inboxDir});
  const [saved]=await v1.receive(message());
  assert.ok(saved.sourceRelpath);assert.ok(saved.path);assert.equal(saved.asset_id,undefined);
  assert.deepEqual(await readFile(saved.path),content);
  let registered=null;
  const flow=createGatewayIntakeFlow({channel:f.channel,inboxDir,ownerId:'owner',save:async()=>{},send:async()=>({messageId:'mock'}),
    download:m=>v1.receive(m),client:async(op,data)=>{if(op==='workflow-lookup')return {matches:[]};
      if(op==='workflow-register'){registered=data;return {workflow_id:'v1-flow',state:'received'};}return {};},
    translation:{},curation:{},progress:{set:async()=>{},stage:async()=>{}}});
  const state={};await flow.handle(message(),state);
  assert.equal(state.workflowV11.id,'v1-flow');assert.equal(state.workflowV21,undefined);
  assert.ok(registered.source_relpath);assert.equal(registered.asset_id,undefined);
  assert.equal(typeof saveInboundResources,'function');
  await f.evidence();
});
