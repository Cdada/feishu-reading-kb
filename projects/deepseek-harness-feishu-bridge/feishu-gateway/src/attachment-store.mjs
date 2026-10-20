import { randomUUID } from 'node:crypto';
import { mkdir, writeFile } from 'node:fs/promises';
import { extname, join } from 'node:path';
import {isDevAttachmentClient} from './custody-v21-dev.mjs';

const maxAttachmentsPerMessage = 5;
const maxImageBytes = 10 * 1024 * 1024;
const maxFileBytes = 20 * 1024 * 1024;

// Shared intake boundary: V1 is the default. V2.1 is explicitly offline until S6.
export function createAttachmentReceiver({mode='v1',channel,inboxDir,custodyBridge=null}) {
  if(mode==='v1')return {mode,receive:message=>saveInboundResources({channel,
    messageId:message.resourceMessageId??message.messageId,resources:message.resources,inboxDir})};
  if(mode!=='v2.1-dev')throw Error('unsupported ingestion mode');
  if(!isDevAttachmentClient(channel)||!custodyBridge)throw Error('V2.1 DEV requires a registered mock client and custody bridge');
  return {mode,pilot:Boolean(custodyBridge.pilotConfig),processNext:()=>custodyBridge.processNext(),
    readInput:binding=>custodyBridge.readInput(binding),receive:async message=>{
    if(!Array.isArray(message.resources)||message.resources.length<1||message.resources.length>maxAttachmentsPerMessage)
      throw Error('V2.1 mock intake requires 1–5 explicit attachments');
    await custodyBridge.prepare(message);
    const attachments=[];
    for(let index=0;index<message.resources.length;index++){
      const binding=await custodyBridge.receive(message,index,async transportRoot=>{
        const [saved]=await saveInboundResources({channel,messageId:message.resourceMessageId??message.messageId,
          resources:[message.resources[index]],inboxDir:transportRoot});
        return saved.path; // Used only by the custody process's pending transport request.
      });
      attachments.push(binding);
    }
    return attachments;
  }};
}

const detectImageMediaType = (buffer) => {
  if (buffer.subarray(0, 8).equals(Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]))) {
    return 'image/png';
  }
  if (buffer[0] === 0xff && buffer[1] === 0xd8 && buffer[2] === 0xff) return 'image/jpeg';
  const prefix = buffer.subarray(0, 6).toString('ascii');
  if (prefix === 'GIF87a' || prefix === 'GIF89a') return 'image/gif';
  if (buffer.subarray(0, 4).toString('ascii') === 'RIFF'
    && buffer.subarray(8, 12).toString('ascii') === 'WEBP') return 'image/webp';
  return null;
};

const extensionForMediaType = (mediaType) => ({
  'image/png': '.png',
  'image/jpeg': '.jpg',
  'image/gif': '.gif',
  'image/webp': '.webp',
})[mediaType];

const sanitizeFileName = (value, fallback) => {
  const cleaned = (value || fallback)
    .normalize('NFC')
    .replace(/[\x00-\x1f\x7f/\\:]/gu, '_')
    .replace(/^\.+/u, '')
    .trim()
    .slice(0, 140);
  return cleaned || fallback;
};

const uniqueFileName = (name, index) => {
  const extension = extname(name);
  const stem = extension ? name.slice(0, -extension.length) : name;
  return `${String(index + 1).padStart(2, '0')}-${stem || 'attachment'}${extension}`;
};

const downloadMessageResource = async ({ channel, messageId, resource, limit }) => {
  const response = await channel.rawClient.im.v1.messageResource.get({
    path: {
      message_id: messageId,
      file_key: resource.fileKey,
    },
    params: {
      type: resource.type === 'image' ? 'image' : 'file',
    },
  });
  const stream = response.getReadableStream();
  const chunks = [];
  let bytes = 0;
  for await (const chunk of stream) {
    const buffer = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
    bytes += buffer.length;
    if (bytes > limit) {
      stream.destroy();
      throw new Error(`${resource.fileName || resource.type} 超过 ${Math.round(limit / 1024 / 1024)} MB 上限`);
    }
    chunks.push(buffer);
  }
  return Buffer.concat(chunks, bytes);
};

export const saveInboundResources = async ({ channel, messageId, resources, inboxDir }) => {
  if (resources.length > maxAttachmentsPerMessage) {
    throw new Error(`单条消息最多处理 ${maxAttachmentsPerMessage} 个附件`);
  }

  const unsupported = resources.filter((resource) => !['image', 'file'].includes(resource.type));
  if (unsupported.length > 0) {
    throw new Error(`当前暂不支持附件类型：${[...new Set(unsupported.map((item) => item.type))].join(', ')}`);
  }

  const receivedAt = new Date();
  const date = [
    receivedAt.getFullYear(),
    String(receivedAt.getMonth() + 1).padStart(2, '0'),
    String(receivedAt.getDate()).padStart(2, '0'),
  ].join('-');
  const messageDir = join(inboxDir, date, randomUUID());
  await mkdir(messageDir, { recursive: true, mode: 0o700 });

  const saved = [];
  for (const [index, resource] of resources.entries()) {
    const limit = resource.type === 'image' ? maxImageBytes : maxFileBytes;
    const buffer = await downloadMessageResource({ channel, messageId, resource, limit });

    let mediaType = null;
    let fallbackName = `attachment-${index + 1}`;
    if (resource.type === 'image') {
      mediaType = detectImageMediaType(buffer);
      if (!mediaType) throw new Error('图片格式无法验证，仅支持 PNG、JPEG、GIF 和 WebP');
      fallbackName = `image-${index + 1}${extensionForMediaType(mediaType)}`;
    }

    const originalFileName = sanitizeFileName(resource.fileName, fallbackName);
    const fileName = uniqueFileName(originalFileName, index);
    const path = join(messageDir, fileName);
    await writeFile(path, buffer, { flag: 'wx', mode: 0o600 });
    saved.push({
      type: resource.type,
      path,
      sourceRelpath: [date, messageDir.split('/').at(-1), fileName].join('/'),
      fileName,
      originalFileName,
      bytes: buffer.length,
      ...(mediaType ? { mediaType, encodedData: buffer.toString('base64') } : {}),
    });
  }

  return saved;
};

export const attachmentSummary = (attachments) => attachments
  .map((item) => `- ${item.fileName} (${(item.bytes / 1024).toFixed(1)} KB)`)
  .join('\n');

export const promptBlocksWithAttachments = (prompt, attachments) => {
  const files = attachments.filter((item) => item.type === 'file');
  const images = attachments.filter((item) => item.type === 'image');
  const text = [
    prompt,
    files.length > 0
      ? `\n飞书附件已保存到受控目录，请按任务需要处理：\n${files.map((item) => `- ${item.path}`).join('\n')}`
      : '',
    images.length > 0 ? `\n本次还附带了 ${images.length} 张图片。` : '',
  ].filter(Boolean).join('\n');

  return [
    { type: 'text', text },
    ...images.map((item) => ({
      type: 'image',
      data: item.encodedData,
      mimeType: item.mediaType,
    })),
  ];
};
