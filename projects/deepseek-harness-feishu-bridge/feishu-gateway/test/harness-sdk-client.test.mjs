import test from 'node:test';
import assert from 'node:assert/strict';
import {EventEmitter} from 'node:events';
import {PassThrough} from 'node:stream';
import {HarnessSdkClient} from '../src/harness-sdk-client.mjs';
import {dshReasoningEffort} from '../src/workflow-models.mjs';

test('DSH workflow keeps existing model but overrides an old thinking-off selection',async()=>{
  const original={provider:'deepseek-official',model:'existing-model',reasoningEffort:'off'};
  const child=new EventEmitter();child.stdout=new PassThrough();child.stderr=new PassThrough();
  const client=new HarnessSdkClient({selection:original,reasoningEffort:dshReasoningEffort,
    workspace:'/tmp',launch:async()=>child});
  let actual;
  client.request=async(method,params)=>{
    assert.equal(method,'initialize');actual=params;
    return {serverInfo:{name:'deepseek-harness-sdk-runtime'}};
  };
  await client.start();
  assert.equal(actual.model,original.model);
  assert.equal(actual.provider,original.provider);
  assert.equal(actual.reasoningEffort,'high');
  assert.equal(original.reasoningEffort,'off');
  child.stdout.destroy();child.stderr.destroy();
});
