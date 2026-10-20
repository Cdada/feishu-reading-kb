import test from 'node:test';
import assert from 'node:assert/strict';
import {
  awaitingDuplicateConfirmation,
  awaitingDuplicateConfirmations,
  extractLocalConversionResults,
  failedConversionResult,
  latestCandidateTask,
  uniqueLocalConversionResults,
} from '../src/local-conversion-results.mjs';

const duplicate = {
  schema_version: 'local-document-conversion/v1',
  ok: true,
  request_id: 'req-1',
  status: 'awaiting_duplicate_confirmation',
};

test('structured conversion results are found in objects and JSON text', () => {
  const found = extractLocalConversionResults({
    structuredContent: duplicate,
    content: [{ type: 'text', text: JSON.stringify(duplicate) }],
  });
  assert.equal(found.length, 2);
  assert.equal(uniqueLocalConversionResults(found).length, 1);
  assert.equal(awaitingDuplicateConfirmation(found)?.request_id, 'req-1');
  assert.equal(awaitingDuplicateConfirmations(found).length, 2);
});

test('failed conversion result is surfaced even when model text claims completion', () => {
  const failure = {
    schema_version: 'local-document-conversion/v1',
    ok: false,
    request_id: 'req-2',
    status: 'failed',
    error_code: 'INSPECTION_NOT_FOUND',
  };
  assert.equal(failedConversionResult([duplicate, failure])?.error_code, 'INSPECTION_NOT_FOUND');
  assert.equal(failedConversionResult([duplicate]), null);
});

test('latest terminal candidate task can be remembered for curation', () => {
  const ready = { ...duplicate, task_id: `task_${'a'.repeat(32)}`, status: 'ready_candidate' };
  const delivery = { ...ready, request_id: 'req-3', status: 'delivery_ready' };
  assert.equal(latestCandidateTask([ready, delivery])?.task_id, ready.task_id);
  assert.equal(latestCandidateTask([duplicate]), null);
});
