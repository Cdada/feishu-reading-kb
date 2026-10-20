const isLocalConversionResult = (value) => value
  && typeof value === 'object'
  && value.schema_version === 'local-document-conversion/v1'
  && typeof value.status === 'string';

export function extractLocalConversionResults(value, found = []) {
  if (typeof value === 'string') {
    try {
      extractLocalConversionResults(JSON.parse(value), found);
    } catch {
      // Ordinary model or progress text is not a structured conversion result.
    }
    return found;
  }
  if (!value || typeof value !== 'object') return found;
  if (isLocalConversionResult(value)) found.push(value);
  for (const child of Array.isArray(value) ? value : Object.values(value)) {
    extractLocalConversionResults(child, found);
  }
  return found;
}

export function uniqueLocalConversionResults(results) {
  const seen = new Set();
  return results.filter((item) => {
    const key = [item.request_id, item.inspection_id, item.task_id, item.status, item.error_code].join('|');
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

export const awaitingDuplicateConfirmations = (results) => (results ?? [])
  .filter((item) => item.ok === true && item.status === 'awaiting_duplicate_confirmation');

export const awaitingDuplicateConfirmation = (results) => awaitingDuplicateConfirmations(results).at(-1) ?? null;

export const failedConversionResult = (results) => [...(results ?? [])]
  .reverse()
  .find((item) => item.ok === false || item.status === 'failed') ?? null;

export const latestCandidateTask = (results) => [...(results ?? [])]
  .reverse()
  .find((item) => item.ok === true
    && typeof item.task_id === 'string'
    && ['ready_candidate', 'needs_review', 'delivery_ready', 'delivery_too_large'].includes(item.status)) ?? null;
