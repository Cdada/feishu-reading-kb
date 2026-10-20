import test from 'node:test';
import assert from 'node:assert/strict';
import { CodexAppServerClient } from '../src/codex-app-server-client.mjs';
import { HarnessSdkClient } from '../src/harness-sdk-client.mjs';
import { parseTranslation } from '../src/translation-loop.mjs';

test('valid UTF-8 containing replacement characters is not a completed translation',()=>{
  assert.throws(()=>parseTranslation(JSON.stringify({translation:'中文\ufffd'})),/TRANSLATION_CORRUPTED_TEXT/);
});

for (const Client of [CodexAppServerClient, HarnessSdkClient]) {
  test(`${Client.name}: all UTF-8 split points preserve Chinese and emoji`, () => {
    const messages = [{method:'synthetic', params:{text:'中文汉字，繁體中文。🙂 é'}}, {id:2,result:'第二行'}];
    const bytes = Buffer.from(messages.map(x => JSON.stringify(x)+'\n').join(''));
    for (let split=1;split<bytes.length;split++) {
      const client = new Client({}); const received=[];
      client.handleMessage = x => received.push(x);
      client.acceptStdout(bytes.subarray(0,split)); client.acceptStdout(bytes.subarray(split));
      assert.deepEqual(received,messages);
    }
    const client=new Client({}); const received=[];client.handleMessage=x=>received.push(x);
    for(const byte of bytes)client.acceptStdout(Buffer.from([byte]));
    assert.deepEqual(received,messages);
  });
  test(`${Client.name}: malformed UTF-8 stops without dispatching corrupted text`,()=>{
    const client=new Client({});let error,dispatched=false,killed=false;
    client.child={kill:()=>{killed=true;}};
    client.handleProcessExit=x=>{error=x;};client.handleMessage=()=>{dispatched=true;};
    client.acceptStdout(Buffer.concat([Buffer.from('{"text":"'),Buffer.from([0xff]),Buffer.from('"}\n')]));
    assert.match(error.message,/UTF-8/);assert.equal(dispatched,false);assert.equal(killed,true);
  });
}
