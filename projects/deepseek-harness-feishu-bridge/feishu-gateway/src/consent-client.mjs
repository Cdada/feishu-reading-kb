import { createHash, createHmac, randomBytes } from 'node:crypto';
import { open } from 'node:fs/promises';
import { constants } from 'node:fs';
import { isAbsolute } from 'node:path';

export function executorEnvironment(env = process.env) {
  return Object.fromEntries(Object.entries(env).filter(([name]) => !name.startsWith('KB_CURATION_GATEWAY_')));
}

export async function loadConsentKey(env = process.env) {
  if (!env.KB_CURATION_GATEWAY_ENABLED || env.KB_CURATION_GATEWAY_ENABLED === '0') return null;
  if (env.KB_CURATION_GATEWAY_ENABLED !== '1' || !isAbsolute(env.KB_CURATION_GATEWAY_KEY_FILE ?? '')) throw new Error('正文授权网关配置无效');
  const handle = await open(env.KB_CURATION_GATEWAY_KEY_FILE, constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK);
  try {
    const info = await handle.stat();
    if (!info.isFile() || info.uid !== process.getuid() || (info.mode & 0o077) || info.size > 4096) throw new Error('正文授权密钥必须为本人私有普通文件');
    const key = (await handle.readFile('utf8')).trim();
    if (!/^[a-f0-9]{64}$/u.test(key)) throw new Error('正文授权密钥格式无效');
    return Buffer.from(key, 'utf8');
  } finally { await handle.close(); }
}

export function createConsentClient({ key, fetchImpl = fetch, now = () => Math.floor(Date.now() / 1000) }) {
  return async (operation, payload) => {
    const workflowRead=['workflow-translation-existing','workflow-revise','workflow-translation-restart','workflow-translation-resume','workflow-metadata','workflow-translation-copy'].includes(operation);
    if (!workflowRead&&!['workflow-lookup','workflow-register','workflow-select','workflow-bind','workflow-grant','workflow-cancel','workflow-translation-delivery','workflow-translation-decide','workflow-link','translation-delivery','translation-prepare','translation-proof','translation-invalidate','translation-check','translation-pull','translation-record','translation-finish','translation-status', 'prepare', 'translation-offer', 'prepare-selected', 'proof', 'invalidate', 'start', 'resume', 'run-status', 'plan-status', 'final-preview', 'draft-preview', 'write-proof', 'invalidate-write', 'rollback-preview', 'publication-status'].includes(operation)) throw new Error('不支持的正文授权控制操作');
    const path = `/gateway/consent/${operation}`;
    const body = JSON.stringify(payload);
    if (Buffer.byteLength(body) > (operation === 'translation-record' ? 256*1024 : 16384)) throw new Error('正文授权请求过大');
    const timestamp = String(now());
    const nonce = randomBytes(16).toString('hex');
    const signature = createHmac('sha256', key).update(['consent-gateway/v1', 'POST', path, timestamp, nonce,
      createHash('sha256').update(body).digest('hex')].join('\n')).digest('hex');
    const response = await fetchImpl(`http://127.0.0.1:18767${path}`, {
      method: 'POST', redirect: 'error', signal: AbortSignal.timeout(15000), body,
      headers: { 'content-type': 'application/json', 'x-consent-timestamp': timestamp,
        'x-consent-nonce': nonce, 'x-consent-signature': signature },
    });
    const result = await response.json();
    if (!response.ok || result?.ok !== true || !result.result || Object.hasOwn(result.result, 'access_token')
      || (Object.hasOwn(result.result, 'run_token') && !['start', 'resume'].includes(operation))) {
      const code = /^[A-Z_]{1,80}$/u.test(result?.error ?? '') ? result.error : 'CONTROL_REQUEST_FAILED';
      throw new Error(`正文授权控制请求未完成（${code}）`);
    }
    return result.result;
  };
}
