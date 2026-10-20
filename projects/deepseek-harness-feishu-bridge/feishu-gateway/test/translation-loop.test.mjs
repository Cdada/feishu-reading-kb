import test from 'node:test';
import assert from 'node:assert/strict';
import {translateAndReview, parseReview, parseTranslation, validateSourceWarnings} from '../src/translation-loop.mjs';
const verdict = (passed) => JSON.stringify({passed, checks:{coverage:true,faithfulness:passed,numbers:passed,terminology:true}, issues:passed?[]:['应保留否定条件及数字 12。']});
const fixture = () => {
  const calls = [], checkpoints = [];
  return {calls, checkpoints, options:{source:'Do not ship the 12 units until Monday.', authorize:async()=>{},
    checkpoint:async x=>checkpoints.push(x), translate:async x=>{calls.push(['dsh',JSON.parse(x)]);return JSON.stringify({translation:'周一之前不要发运这 12 件货物。'});},
    review:async x=>{calls.push(['codex',JSON.parse(x)]);return verdict(true);}}};
};
test('first pass: DSH translates, Codex reviews, only then releases',async()=>{
  const f=fixture(); const result=await translateAndReview(f.options);
  assert.equal(result.revision,0); assert.deepEqual(f.calls.map(x=>x[0]),['dsh','codex']);
  assert.equal(f.checkpoints.at(-1).phase,'reviewed');
});
test('resume reviewing reuses exact saved translation and does not repeat DSH',async()=>{
  const f=fixture();f.options.resume={phase:'reviewing',revision:1,translation:'已保存译文',verdict:JSON.parse(verdict(false))};
  const result=await translateAndReview(f.options);
  assert.deepEqual(f.calls.map(x=>x[0]),['codex']);assert.equal(f.calls[0][1].translation,'已保存译文');
  assert.equal(result.revision,1);assert.deepEqual(f.checkpoints.map(x=>x.phase),['reviewed']);
});
test('resume revising repeats only interrupted revision without resetting revision budget',async()=>{
  const f=fixture();f.options.resume={phase:'revising',revision:2,translation:'旧译文',verdict:JSON.parse(verdict(false))};
  f.options.review=async()=>verdict(false);f.options.allowOwnerDecision=true;
  const result=await translateAndReview(f.options);
  assert.equal(f.calls.length,1);assert.equal(f.calls[0][1].revision,2);assert.equal(result.pendingOwner,true);
  assert.deepEqual(f.checkpoints.map(x=>x.phase),['reviewing','needs_revision','owner_pending']);
});
test('resume final failed review only marks owner pending, never calls a model or manufactures pass',async()=>{
  const f=fixture();f.options.resume={phase:'needs_revision',revision:2,translation:'待判断译文',verdict:JSON.parse(verdict(false))};f.options.allowOwnerDecision=true;
  const result=await translateAndReview(f.options);assert.equal(result.pendingOwner,true);assert.equal(f.calls.length,0);
  assert.equal(result.verdict.passed,false);assert.deepEqual(f.checkpoints,[{phase:'owner_pending',revision:2}]);
});
test('resume initial translation retains interrupted intent and malformed resume is rejected',async()=>{
  const f=fixture();f.options.resume={phase:'translating',revision:0};await translateAndReview(f.options);
  assert.deepEqual(f.checkpoints.map(x=>x.phase),['reviewing','reviewed']);
  for(const resume of [{phase:'reviewed',revision:0},{phase:'reviewing',revision:0},{phase:'revising',revision:3}]){
    await assert.rejects(translateAndReview({...fixture().options,resume}),/INVALID_RESUME/);
  }
});
test('Codex feedback goes to DSH; changed text is independently reviewed',async()=>{
  const f=fixture(); let n=0;
  f.options.review=async x=>{f.calls.push(['codex',JSON.parse(x)]);return verdict(n++>0);};
  const result=await translateAndReview(f.options);
  assert.equal(result.revision,1); assert.deepEqual(f.calls.map(x=>x[0]),['dsh','codex','dsh','codex']);
  assert.equal(f.calls[2][1].feedback.issues.length,1);
  assert.equal(f.calls[3][1].translation,result.translation);
});
test('two revisions maximum; unresolved errors never deliver',async()=>{
  const f=fixture(); let reviews=0; f.options.review=async()=>{reviews++;return verdict(false);};
  await assert.rejects(translateAndReview(f.options),/REVIEW_NOT_PASSED/); assert.equal(reviews,3);
});
test('revocation after translation prevents review and release',async()=>{
  const f=fixture(); let n=0; f.options.authorize=async()=>{if(n++)throw Error('revoked');};
  await assert.rejects(translateAndReview(f.options),/revoked/);assert.deepEqual(f.calls.map(x=>x[0]),['dsh']);
});
test('uncertain transport outcome does not retry',async()=>{
  const f=fixture();let n=0;f.options.translate=async()=>{n++;throw Error('timeout');};
  await assert.rejects(translateAndReview(f.options),/timeout/);assert.equal(n,1);
});
test('invalid, contradictory, or incomplete review fails closed',()=>{
  for(const x of ['{}', '{"passed":true}',verdict(false).replace('"passed":false','"passed":true')]) assert.throws(()=>parseReview(x));
  assert.throws(()=>parseTranslation('{"translation":"", "approved":true}'));
  assert.throws(()=>parseTranslation('```json\n{}\n```'));
});
test('source uncertainty may pass only with exact original, cautious translation and note',async()=>{
  const w={source_quote:'The device are maybe ready.',cautious_translation:'设备也许已准备好。',note:'原句语法有误，是否就绪并不确定。'};
  const translation=`【原文存疑】原句：${w.source_quote} 谨慎译文：${w.cautious_translation} 说明：${w.note}`;
  const v={passed:true,checks:{coverage:true,faithfulness:true,numbers:true,terminology:true},issues:[],source_warnings:[w]};
  const f=fixture();f.options.source=w.source_quote;f.options.translate=async()=>JSON.stringify({translation});f.options.review=async()=>JSON.stringify(v);
  assert.equal((await translateAndReview(f.options)).verdict.source_warnings.length,1);
  for(const text of [translation.replace(w.source_quote,''),translation.replace(w.note,''),translation.replace('【原文存疑】',''),translation.replace(w.cautious_translation,'已确定准备好了')])assert.throws(()=>validateSourceWarnings(v,w.source_quote,text));
  assert.throws(()=>validateSourceWarnings(v,'Another source',translation));
  assert.throws(()=>validateSourceWarnings({...v,source_warnings:[]},w.source_quote,translation));
});
test('real mistranslation still blocks and carries repair details',async()=>{
  const f=fixture();f.options.review=async()=>verdict(false);
  await assert.rejects(translateAndReview(f.options),e=>e.message==='TRANSLATION_REVIEW_NOT_PASSED'&&e.reviewIssues[0].includes('否定'));
  assert.throws(()=>parseReview(JSON.stringify({passed:true,checks:{coverage:true,faithfulness:false,numbers:true,terminology:true},issues:['漏译'],source_warnings:[]})));
});
test('incomplete source annotation cannot pass but gets a bounded repair and new review',async()=>{
  const f=fixture();const source='The device are maybe ready.';
  const w={source_quote:source,cautious_translation:'设备也许已准备好。',note:'原文措辞存疑。'};
  const v={passed:true,checks:{coverage:true,faithfulness:true,numbers:true,terminology:true},issues:[],source_warnings:[w]};
  let count=0;f.options.source=source;
  f.options.translate=async()=>JSON.stringify({translation:count++?`【原文存疑】${source} ${w.cautious_translation} ${w.note}`:w.cautious_translation});
  f.options.review=async()=>JSON.stringify(v);
  const result=await translateAndReview(f.options);assert.equal(result.revision,1);
  assert.equal(f.checkpoints.filter(c=>c.phase==='reviewed').length,1);
  assert.equal(f.checkpoints.find(c=>c.phase==='needs_revision').verdict.passed,false);
});
