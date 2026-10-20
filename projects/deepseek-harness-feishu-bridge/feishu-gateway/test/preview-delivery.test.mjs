import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, readFile, stat, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { createPreviewSender, decodePreviewPdf } from '../src/preview-delivery.mjs';

const pdf = Buffer.from('%PDF-1.4\nsynthetic fixture\n%%EOF');
test('preview sends a privately stored PDF with a readable filename and exact bytes', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'kb-preview-test-'));
  try {
    const sent = [];
    const sender = createPreviewSender({ outputsDir:dir, send:async (...args) => { sent.push(args); return {messageId:'receipt'}; } });
    const result = await sender({chatId:'chat',messageId:'message'}, `write_${'a'.repeat(32)}`, pdf.toString('base64'));
    assert.equal(result.messageId, 'receipt');
    assert.equal(sent[0][1].file.fileName, '入库预览.pdf');
    const path = sent[0][1].file.source;
    assert.deepEqual(await readFile(path), pdf);
    assert.equal((await stat(path)).mode & 0o777, 0o600);
    await assert.rejects(() => sender({chatId:'chat'}, `write_${'a'.repeat(32)}`, pdf.toString('base64')), /EEXIST/u);
    assert.equal(sent.length, 1);
  } finally { await rm(dir, {recursive:true}); }
});
test('missing or truncated PDF cannot silently fall back to Markdown', () => {
  for (const value of [undefined, '', 'invalid!', Buffer.from('# markdown').toString('base64'), Buffer.from('%PDF-1.4').toString('base64')]) {
    assert.throws(() => decodePreviewPdf(value));
  }
});
test('explicit translation resend uses a new private file without replacing prior output',async()=>{
  const dir=await mkdtemp(join(tmpdir(),'kb-translation-resend-'));
  try{
    const sent=[];const sender=createPreviewSender({outputsDir:dir,send:async(...args)=>{sent.push(args);return {messageId:'sent'};}});
    for(let i=0;i<2;i++)await sender({chatId:'chat'},`translation_${'b'.repeat(32)}`,pdf.toString('base64'));
    assert.notEqual(sent[0][1].file.source,sent[1][1].file.source);
    for(const args of sent){assert.match(args[1].file.fileName,/对照稿/);assert.deepEqual(await readFile(args[1].file.source),pdf);}
  }finally{await rm(dir,{recursive:true});}
});
