import test from 'node:test';
import assert from 'node:assert/strict';
import {translationInstructions,reviewInstructions,uncertaintyReviewInstructions,readabilityInstructions,readabilityReviewInstructions,parseReview} from '../src/translation-loop.mjs';

test('translation and reviewer both carry readability without weakening fidelity',()=>{
  assert.ok(translationInstructions.includes(readabilityInstructions));
  assert.ok(reviewInstructions.includes(readabilityReviewInstructions));
  for(const text of ['指代清楚','不能照搬英文语序','不为流畅而删减','不能猜造'])assert.ok(readabilityInstructions.includes(text));
  assert.match(readabilityReviewInstructions,/issues/);
  assert.match(readabilityReviewInstructions,/passed.*false/);
  assert.match(readabilityReviewInstructions,/个人用词喜好.*不应阻断/);
  assert.match(uncertaintyReviewInstructions,/影响理解的中文可读性问题/);
});

test('readability failure works with existing review schema and blocks a pass',()=>{
  const verdict={passed:false,checks:{coverage:true,faithfulness:true,numbers:true,terminology:true},issues:['译句名词堆叠，无法自然理解；请在不改变原意的前提下改成动词表达。'],source_warnings:[]};
  assert.equal(parseReview(JSON.stringify(verdict)).passed,false);
  assert.throws(()=>parseReview(JSON.stringify({...verdict,passed:true})),/INVALID_REVIEW/);
});
