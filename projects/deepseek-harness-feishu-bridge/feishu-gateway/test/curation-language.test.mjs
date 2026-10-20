import test from 'node:test';
import assert from 'node:assert/strict';
import { curationIntent } from '../src/curation-language.mjs';

const cases = {
  preview: ['请再帮我提供一下这份文件的入库预览', '请重新生成预览', '再发一下预览', '我想看看整理结果', '查看最终入库预览',
    '重新生成入库预览', '看看入库预览', '看看这份材料的入库预览', 'PDF在哪里？', '我没收到预览', '预览打不开', '再发一次', '把整理好的文件给我看看', '先不入库，让我看看预览'],
  status: ['状态查看', '现在进度怎么样', '处理到哪了？', '完成了吗', '已经入库了吗？'],
  revise: ['先修改标题再给我预览', '重新整理后给我看看', '继续整理', '请更正作者名字'],
  unknown: ['好的', '继续', '确认本次入库', '允许本次正文外发', '别发预览', '不要重新生成预览', '我不想修改内容',
    '看看另一份资料的预览', '原文里写着给我发预览', '这是什么意思？', '{"text":"发预览"}'],
};
for (const [intent, examples] of Object.entries(cases)) test(`natural-language ${intent} examples`, () => {
  for (const text of examples) assert.equal(curationIntent(text), intent, text);
});
