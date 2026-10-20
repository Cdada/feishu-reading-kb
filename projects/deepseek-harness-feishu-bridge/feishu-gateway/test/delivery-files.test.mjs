import test from 'node:test';
import assert from 'node:assert/strict';
import { extractDeliveryFiles, extractDeliveryFilesFromContent, extractDeliveryFilesFromText, uniqueDeliveryFiles } from '../src/delivery-files.mjs';

test('delivery files are extracted only from structured MCP results', () => {
  const files = extractDeliveryFiles({ delivery_files: [{ display_name: '候选资料包.zip', outbound_tool_path: 'outputs/task/a.zip', feishu_sendable: true }] });
  assert.deepEqual(files, [{ outboundToolPath: 'outputs/task/a.zip', fileName: '候选资料包.zip', sendable: true }]);
  assert.equal(extractDeliveryFilesFromText('普通回答').length, 0);
});

test('nested DSH tool-result content is decoded', () => {
  const content = [{
    type: 'tool-result',
    content: [{ type: 'text', text: JSON.stringify({ delivery_files: [{ outbound_tool_path: 'outputs/task/dsh.zip', display_name: 'dsh.zip' }] }) }],
  }];
  assert.deepEqual(extractDeliveryFilesFromContent(content), [{ outboundToolPath: 'outputs/task/dsh.zip', fileName: 'dsh.zip', sendable: true }]);
});

test('duplicate delivery paths are collapsed', () => {
  const item = { outboundToolPath: 'outputs/task/a.zip', fileName: 'a.zip', sendable: true };
  assert.equal(uniqueDeliveryFiles([item, item]).length, 1);
});
