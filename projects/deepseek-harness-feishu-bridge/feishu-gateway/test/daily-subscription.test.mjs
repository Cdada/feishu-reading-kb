import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, writeFile, rm, realpath } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { createHash } from 'node:crypto';
import { probe } from '../src/daily-subscription.mjs';
import { standingReserve } from '../src/pilot-reading-guide.mjs';
const hash = v => createHash('sha256').update(v).digest('hex');

test('temporary reserve returns to five percent at the exact expiry',async()=>{
  const root=await realpath(await mkdtemp(join(tmpdir(),'subscription-')));
  try {
    const path=join(root,'rules.json');const raw=JSON.stringify({approved:true,policy:{model_budget_mode:'standing_daily',model_turn_limit:null,paid_model_cny:0,temporary_reserve_percent:1,temporary_reserve_until:1000}});
    await writeFile(path,raw);
    assert.equal(await standingReserve(path,hash(raw),()=>999000),1);
    assert.equal(await standingReserve(path,hash(raw),()=>1000000),5);
    await assert.rejects(standingReserve(path,'a'.repeat(64),()=>999000));
  } finally {await rm(root,{recursive:true,force:true});}
});

test('quota probe makes only account/status requests and no model turn',async()=>{
  for (const [used,reserve,expected] of [[98,1,'ready'],[99,1,'waiting'],[96,5,'waiting']]) {
    const calls=[];
    const client={start:async()=>{},pinCurrentProcess:()=>{},close:async()=>{},request:async method=>{
      calls.push(method);
      return method==='account/read' ? {requiresOpenaiAuth:true,account:{type:'chatgpt',planType:'plus',email:'fixture@example.invalid'}}
        : {rateLimits:{limitId:'codex',primary:{usedPercent:used},credits:{hasCredits:false,unlimited:false}}};
    }};
    const result=await probe({accountSha:hash('fixture@example.invalid')},{client,check:async()=>reserve});
    assert.equal(result.state,expected);assert.equal(result.modelCalls,0);
    assert.deepEqual(calls,['account/read','account/rateLimits/read']);
  }
});

test('unavailable quota metadata waits instead of assuming unlimited',async()=>{
  const client={start:async()=>{},pinCurrentProcess:()=>{},close:async()=>{},request:async()=>({})};
  assert.equal((await probe({accountSha:'a'.repeat(64)},{client,check:async()=>5})).state,'waiting');
});
