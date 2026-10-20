import { mkdir, writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import { randomUUID } from 'node:crypto';

export function decodePreviewPdf(value) {
  if (typeof value !== 'string' || value.length > 14 * 1024**2
    || !/^[A-Za-z0-9+/]+={0,2}$/u.test(value)) throw new Error('可阅读的入库预览尚未生成，不能请求确认');
  const bytes = Buffer.from(value, 'base64');
  if (bytes.toString('base64') !== value || bytes.length > 10 * 1024**2
    || bytes.subarray(0, 5).toString('ascii') !== '%PDF-'
    || !bytes.subarray(-1024).toString('ascii').includes('%%EOF')) throw new Error('入库预览文件不完整，不能请求确认');
  return bytes;
}

export function createPreviewSender({ outputsDir, send }) {
  return async (message, writeId, pdfBase64) => {
    if (!/^(write|view|translation)_[a-f0-9]{32}$/u.test(writeId)) throw new Error('无效入库预览编号');
    const bytes = decodePreviewPdf(pdfBase64);
    await mkdir(outputsDir, { recursive: true, mode: 0o700 });
    const name = writeId.startsWith('translation_') ? '原文与中文译文对照稿-DSH.pdf' : writeId.startsWith('view_') ? '已保存草稿-仅供查看.pdf' : '入库预览.pdf';
    const path = join(outputsDir, `${writeId}${writeId.startsWith('translation_')?'-'+randomUUID():''}-${name}`);
    await writeFile(path, bytes, { mode: 0o600, flag: 'wx' });
    return send(message.chatId, { file: { source: path, fileName: name } }, { replyTo: message.messageId });
  };
}
