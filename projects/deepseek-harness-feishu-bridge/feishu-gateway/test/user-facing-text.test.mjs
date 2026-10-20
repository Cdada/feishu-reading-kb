import test from 'node:test';
import assert from 'node:assert/strict';
import { humanReply, humanProgress, humanSize, locationLabel, qualitySummary, completenessLabel, reviewLabel } from '../src/user-facing-text.mjs';

test('normal replies hide diagnostic codes while retaining names, warnings and next steps', () => {
  const raw = `资料：研究报告.pdf\nSHA-256：${'a'.repeat(64)}\n- **plan_id**: plan_${'b'.repeat(32)}\n发现重复文件，请先确认是否继续。\n严重问题：2 项`;
  assert.equal(humanReply(raw), '资料：研究报告.pdf\n发现重复文件，请先确认是否继续。\n严重问题：2 项');
  assert.ok(raw.includes('a'.repeat(64)), 'the diagnostic input is unchanged');
  assert.ok(!humanReply(`已保存 plan_${'b'.repeat(32)}；${'a'.repeat(64)}`).includes('b'.repeat(32)));
});
test('diagnostic filtering preserves failure explanations and usable link targets', () => {
  const target = `https://example.test/files/${'a'.repeat(64)}`;
  assert.equal(humanReply(`[下载资料](${target})`), `[下载资料](${target})`);
  assert.match(humanReply(`任务编号：task_${'b'.repeat(32)}，转换失败，请重新上传。`), /转换失败，请重新上传/u);
  assert.equal(locationLabel('分类/资料--123456abcdef'), '分类 / 资料');
  assert.equal(locationLabel(`归档/pub_${'b'.repeat(32)}`), '归档 / 本次撤回资料');
});
test('known progress, quality and review fields have human labels and unknown is not zero', () => {
  assert.equal(humanProgress('正在调用本机知识库整理工具：get_curation_run'), '正在核对这份材料的处理进度。');
  assert.match(qualitySummary({serious_issues:2, warnings:3, manual_review_count:1, artifact_provenance:{sha256:'a'.repeat(64)}}), /严重问题 2 项、提醒 3 项、需人工核对 1 项/u);
  assert.match(qualitySummary({}), /未提供/u);
  assert.equal(completenessLabel('excerpt_only'), '仅有摘录');
  assert.match(reviewLabel('human_reviewed_source_incomplete'), /不完整/u);
  assert.equal(humanSize(2048), '2.0 KB');
});
