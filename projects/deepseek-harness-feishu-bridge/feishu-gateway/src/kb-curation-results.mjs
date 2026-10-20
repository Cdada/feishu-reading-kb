import { humanSize } from './user-facing-text.mjs';

const isCurationResult = (value) => value
  && typeof value === 'object'
  && value.schema_version === 'kb-curation/v1'
  && typeof value.status === 'string';

export function extractCurationResults(value, found = []) {
  if (typeof value === 'string') {
    try {
      extractCurationResults(JSON.parse(value), found);
    } catch {
      // Ordinary model or progress text is not a structured curation result.
    }
    return found;
  }
  if (!value || typeof value !== 'object') return found;
  if (isCurationResult(value)) {
    found.push(value);
    // Filenames and labels inside a result are data, not nested tool results.
    return found;
  }
  for (const child of Array.isArray(value) ? value : Object.values(value)) {
    extractCurationResults(child, found);
  }
  return found;
}

export function uniqueCurationResults(results) {
  const seen = new Set();
  return results.filter((item) => {
    const key = [item.request_id, item.plan_id, item.status, item.error_code, item.preview_relpath, item.archive_preview?.preview_hash].join('|');
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

export const latestCurationPlan = (results) => [...(results ?? [])]
  .reverse()
  .find((item) => item.ok === true && typeof item.plan_id === 'string') ?? null;

export const latestArchivePreview = (results) => [...(results ?? [])]
  .reverse()
  .find((item) => item.ok === true && item.archive_preview?.scope === 'metadata_preflight_not_write_authorization')?.archive_preview ?? null;

export function requireCurationSource(plan, expectedSourceTaskId) {
  const source = plan?.source?.task_id ?? plan?.archive_preview?.source_task_id;
  if (source !== expectedSourceTaskId) throw new Error('后处理结果与当前材料不一致，已保留原计划；请重新核对材料');
}

const inertLabel = (value) => String(value ?? '').slice(0, 160).replace(/[\r\n]/gu, ' ')
  .replace(/&/gu, '&amp;').replace(/</gu, '&lt;').replace(/>/gu, '&gt;').replace(/@/gu, '&#64;')
  .replace(/[\\`*_\[\]()#!|]/gu, '\\$&');

export function renderArchivePreview(results) {
  const preview = latestArchivePreview(results);
  if (!preview) return '';
  const count = Number.isSafeInteger(preview.file_count) ? preview.file_count : 0;
  const bytes = Number.isSafeInteger(preview.total_bytes) ? preview.total_bytes : 0;
  const expires = Number.isSafeInteger(preview.expires_at)
    ? new Date(preview.expires_at * 1000).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false })
    : '以本机记录为准';
  return [
    '', '---', '', '📋 **本机写入前预览（尚未入库）**', '',
    `- 材料：${inertLabel(preview.source_filename)}`,
    `- 知识内容候选：${count} 个，共 ${humanSize(bytes)}；原始上传文件不在本次拟复制清单中`,
    `- 辅助记录：${preview.supporting_files?.length ?? 0} 个；默认排除代码：${preview.excluded_code_files?.length ?? 0} 个；仍待判断：${preview.undecided_files?.length ?? 0} 个`,
    `- 来源分类：${inertLabel(preview.target?.category_label)}`,
    `- 归档位置：个人知识库 / ${inertLabel(preview.target?.category_label ?? '来源分类明确后才能确定')}（具体资料文件夹将在最终预览中确认）`,
    '- 保持候选文件原有内容和相对路径；不覆盖已有文件',
    `- 预览到期时间（北京时间）：${expires}；新预览会替代旧预览`,
    '', '**知识内容候选：**', '',
    ...(preview.files ?? []).slice(0, 5).map((file) => `- ${inertLabel(file.display_name)}`),
    ...(count > 5 ? [`- 另有 ${count - 5} 个文件，完整清单保存在本机预览台账`] : []),
    '', '**辅助记录（保留追溯，不作为知识正文）：**', '',
    ...(preview.supporting_files ?? []).slice(0, 4).map((file) => `- ${inertLabel(file.display_name)}`),
    '', '**代码（默认不入库，不自动上传 GitHub）：**', '',
    ...(preview.excluded_code_files ?? []).slice(0, 3).map((file) => `- ${inertLabel(file.display_name)}`),
    '', '**仍待判断：**', '',
    ...(preview.undecided_files ?? []).slice(0, 3).map((file) => `- ${inertLabel(file.display_name)}：${inertLabel(file.classification?.reason)}`),
    '- 分类只影响预览，不删除或移动任何原件。',
    '', '**目前还缺少：**', '',
    ...(preview.blocker_messages ?? []).slice(0, 12).map((label) => `- ${inertLabel(label)}`),
    '', '**此处不是写入审批；回复“同意”或“继续”也不会自动入库。**',
  ].join('\n');
}
