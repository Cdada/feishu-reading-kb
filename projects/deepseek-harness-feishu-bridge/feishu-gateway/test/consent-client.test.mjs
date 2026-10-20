import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, writeFile, chmod, symlink, rm, readFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { createConsentClient, executorEnvironment, loadConsentKey } from '../src/consent-client.mjs';

test('control client fixes loopback destination, disallows redirect and sends no MCP bearer',async()=>{
  let request;
  const client=createConsentClient({key:Buffer.alloc(32,1),fetchImpl:async(...args)=>{request=args;return {ok:true,json:async()=>({ok:true,result:{state:'presented'}})};}});
  assert.equal((await client('proof',{proof:{}})).state,'presented');
  assert.equal(request[0],'http://127.0.0.1:18767/gateway/consent/proof');
  assert.equal(request[1].redirect,'error');assert.equal(request[1].headers.authorization,undefined);
  assert.match(request[1].headers['x-consent-signature'],/^[a-f0-9]{64}$/);
  await assert.rejects(client('read_chunk',{}));
});
test('control client rejects unexpected capability delivery and hides raw response errors',async()=>{
  for(const result of [{ok:true,result:{access_token:'secret'}},{error:'<script>secret</script>'}]) {
    const client=createConsentClient({key:Buffer.alloc(32),fetchImpl:async()=>({ok:true,json:async()=>result})});
    await assert.rejects(client('proof',{}),error=>!error.message.includes('secret'));
  }
});
test('executor environments never inherit gateway authority settings',async()=>{
  const env={PATH:'/bin',KB_CURATION_GATEWAY_ENABLED:'1',KB_CURATION_GATEWAY_KEY_FILE:'/private/key',KB_CURATION_GATEWAY_KEY:'secret',KB_CURATION_GATEWAY_OWNER_FILE:'/private/owner'};
  assert.deepEqual(executorEnvironment(env),{PATH:'/bin'});
  for(const path of ['codex-app-server-client.mjs','harness-sdk-client.mjs']) {
    assert.match(await readFile(new URL(`../src/${path}`,import.meta.url),'utf8'),/env: executorEnvironment\(\)/);
  }
});
test('gateway activation requires explicit opt-in and private non-symlink key',async()=>{
  const root=await mkdtemp(join(tmpdir(),'consent-key-test-'));
  try {
    const path=join(root,'key');await writeFile(path,'c'.repeat(64),{mode:0o600});
    const env={KB_CURATION_GATEWAY_ENABLED:'1',KB_CURATION_GATEWAY_KEY_FILE:path};
    assert.equal((await loadConsentKey(env)).toString(),'c'.repeat(64));
    assert.equal(await loadConsentKey({}),null);
    await chmod(path,0o644);await assert.rejects(loadConsentKey(env));await chmod(path,0o600);
    const linked=join(root,'linked');await symlink(path,linked);
    await assert.rejects(loadConsentKey({...env,KB_CURATION_GATEWAY_KEY_FILE:linked}));
  } finally { await rm(root,{recursive:true,force:true}); }
});
