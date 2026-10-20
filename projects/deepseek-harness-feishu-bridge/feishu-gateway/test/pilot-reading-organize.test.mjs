import test from 'node:test';
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { runReading, organizeSchema, reviewSchema, readingClientOptions } from '../src/pilot-reading-organize.mjs';
import { MODEL } from '../src/pilot-reading-guide.mjs';

const sha=(v)=>createHash('sha256').update(v).digest('hex');
function fixture(purpose) {
  const account={requiresOpenaiAuth:true,account:{type:'chatgpt',planType:'pro',email:'synthetic@example.invalid'}};
  const limits={rateLimits:{limitId:'codex',planType:'pro',rateLimitReachedType:null,
    primary:{usedPercent:20},credits:{hasCredits:false,unlimited:false}}};
  const calls=[];
  const client={start:async()=>{},pinCurrentProcess:()=>{},close:async()=>{},
    request:async(m)=>m==='account/read'?account:limits,ensureThread:async()=> 'synthetic',
    prompt:async(t,r,o)=>{calls.push(o);return {text:'{}',toolCalls:[]};}};
  const request=JSON.stringify({protocol:'P41-reading-v1',purpose});
  return {client,calls,packet:{model:MODEL,effort:'medium',waitMs:1000,request,requestId:'synthetic',
    binding:{expiresAt:new Date(Date.now()+60000).toISOString(),requestSha:sha(request),accountSha:sha(account.account.email)}}};
}
test('organization and independent review use distinct reviewed schemas',async()=>{
  for (const [purpose,schema] of [['organize',organizeSchema],['review',reviewSchema]]) {
    const f=fixture(purpose);
    assert.equal((await runReading(f.packet,{client:f.client,check:async()=>{}})).status,'received');
    assert.equal(f.calls.length,1);assert.deepEqual(f.calls[0].outputSchema,schema);
  }
});
test('a packet cannot select a tool, billing mode or arbitrary output schema',async()=>{
  const f=fixture('shell');
  await assert.rejects(runReading(f.packet,{client:f.client,check:async()=>{}}));
  assert.equal(f.calls.length,0);
  const opts=readingClientOptions({cwd:'/synthetic',codexBinary:'/synthetic/codex',check:async()=>{},environment:{}});
  assert.deepEqual(opts.allowedMcpServers,[]);
  assert.equal(opts.model,MODEL);assert.equal(opts.effort,'medium');
  assert.match(opts.developerInstructions,/虚构不得提升为现实知识/);
});
test('reading does not retry after an unknown turn or accept tool output',async()=>{
  const f=fixture('review');let calls=0;
  f.client.prompt=async()=>{calls++;throw new Error('synthetic timeout');};
  assert.equal((await runReading(f.packet,{client:f.client,check:async()=>{}})).status,'unknown');
  assert.equal(calls,1);
  f.client.prompt=async()=>({text:'{}',toolCalls:[{name:'shell'}]});
  assert.equal((await runReading(f.packet,{client:f.client,check:async()=>{}})).status,'received_rejected');
});

test('identity, completeness and review outcomes use the Python contract options',()=>{
  const fields=organizeSchema.properties;
  assert.deepEqual(fields.category.enum,['', '01_书籍与期刊', '02_课程', '03_GitHub项目',
    '04_网页文章与在线资源', '05_现场讲座与调研', '06_个人数据与资料包']);
  assert.deepEqual(fields.content_nature.enum,['', 'fiction', 'nonfiction', 'mixed']);
  assert.deepEqual(fields.source_completeness.enum,['', 'full_text', 'excerpt_only', 'incomplete', 'limited']);
  assert.deepEqual(reviewSchema.properties.validation_status.enum,['', 'human_reviewed_deep_read',
    'human_reviewed_structure', 'human_reviewed_limited_source', 'human_reviewed_source_incomplete',
    'human_reviewed_time_sensitive']);
  assert.ok(!fields.source_completeness.enum.includes('已整理全部正文'));
});
test('an empty library still permits useful new cards and topics without choosing paths',()=>{
  const options=readingClientOptions({cwd:'/synthetic',codexBinary:'/synthetic/codex',check:async()=>{},environment:{}});
  assert.match(options.developerInstructions,/可按复用价值创建新卡片\/主题/);
  assert.match(options.developerInstructions,/existing_path 填空字符串/);
  assert.match(options.developerInstructions,/无复用价值则留空列表/);
});

test('review findings are unresolved issues, with positive evidence kept in scope',()=>{
  assert.match(reviewSchema.properties.findings.description,/approve 时必须为空数组/);
  assert.match(reviewSchema.properties.findings.description,/依据与说明写入 review_scope/);
});
