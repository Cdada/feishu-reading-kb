// Presentation only. Never apply these helpers to stored state, tool arguments,
// consent scopes, signatures, source documents or diagnostic logs.
export const humanSize = (bytes) => bytes < 1024 ? `${bytes} 字节`
  : bytes < 1024 ** 2 ? `${(bytes / 1024).toFixed(1)} KB` : `${(bytes / 1024 ** 2).toFixed(1)} MB`;
export const humanTime = (seconds) => new Date(seconds * 1000).toLocaleString('zh-CN', {
  timeZone: 'Asia/Shanghai', hour12: false,
}) + '（北京时间）';
export const completenessLabel = (value) => ({ full: '完整材料', excerpt_only: '仅有摘录', partial: '部分材料', uncertain: '完整性尚不确定' }[value] ?? '完整性待核对');
export const reviewLabel = (value) => ({
  human_reviewed_deep_read: '已做内容深读核对', human_reviewed_structure: '已做结构核对',
  human_reviewed_limited_source: '来源有限，结论需谨慎', human_reviewed_source_incomplete: '来源不完整，需补充核对',
  human_reviewed_time_sensitive: '内容具有时效性，使用前需核对最新情况',
}[value] ?? '复核范围需结合预览核对');
export function qualitySummary(quality) {
  const fields = [['serious_issues', '严重问题'], ['warnings', '提醒'], ['manual_review_count', '需人工核对']];
  return fields.map(([key, label]) => `${label} ${Number.isSafeInteger(quality?.[key]) && quality[key] >= 0 ? `${quality[key]} 项` : '数量未提供'}`).join('、');
}

// A location label, not a filesystem path. Exact paths remain in the receipt.
export const locationLabel = (path) => String(path ?? '').split('/').filter(Boolean).map((part) => {
  if (/^pub_[a-f0-9]{32}$/u.test(part)) return '本次撤回资料';
  return part.replace(/--[a-f0-9]{12}$/u, '');
}).join(' / ');

// Keep real file paths usable. Hide standalone diagnostic fields in prose, not
// arbitrary lines mentioning a problem or duplicate file.
export function humanReply(value) {
  const clean = (text) => text
    .replace(/\b(?:plan|task|run|consent|write|pub|artifact)_[a-f0-9]{32}\b/gu, '（后台记录）')
    .replace(/\b[a-f0-9]{64}\b/giu, '（后台校验信息）');
  const diagnosticOnly = /^\s*(?:[-*]\s*)?(?:\*\*)?(?:SHA-?256|文件哈希|来源哈希|预览校验值|文件编号|计划编号|任务编号|来源转换任务|plan_id|task_id|run_id|consent_id|publication_id|payload_hash|source_sha256)(?:\*\*)?\s*[:：]\s*[`*]*(?:[a-f0-9]{64}|(?:plan|task|run|consent|write|pub|artifact)_[A-Za-z0-9_.:-]+)[`*]*[。；;]?\s*$/iu;
  return String(value ?? '').split('\n').filter((line) => !diagnosticOnly.test(line))
    // Link destinations can contain identifiers; preserve them so delivery
    // links remain usable, while cleaning the visible label and other prose.
    .map((line) => line.split(/(\[[^\]\n]*\]\([^\s)]+\))/gu).map((part) => {
      const link = /^(\[)([^\]\n]*)(\]\([^\s)]+\))$/u.exec(part);
      return link ? link[1] + clean(link[2]) + link[3] : clean(part);
    }).join('')).join('\n')
    .replace(/\n{3,}/gu, '\n\n').trim();
}

export function humanProgress(value) {
  const text = String(value ?? '');
  if (/read_next_curation_chunk|save_curation_chunk/u.test(text)) return '正在逐段整理正文并保存结果。';
  if (/submit_curation_draft/u.test(text)) return '正在保存整理稿。';
  if (/review_curation_draft/u.test(text)) return '正在核对整理稿和来源。';
  if (/get_curation_run/u.test(text)) return '正在核对这份材料的处理进度。';
  if (/正在调用.*工具/u.test(text)) return '正在处理这份材料，请稍候。';
  return humanReply(text);
}
