import {spawn, execFile} from 'node:child_process';
import {promisify} from 'node:util';
import {userInfo} from 'node:os';
import {randomUUID} from 'node:crypto';
import {HarnessSdkClient} from './harness-sdk-client.mjs';
import {executorEnvironment} from './consent-client.mjs';
import {translationInstructions,uncertaintyInstructions} from './translation-loop.mjs';
import {dshTranslationModel} from './workflow-models.mjs';
const exec = promisify(execFile);

// sdk-minimal is a supported standalone DSH profile. Explicitly remove its two
// default tool stacks, persistence, package inventory, and session-log uploader.
export const translationPatch = [
  ...['persistent-bash','persistent-pwsh','str-replace-editor','terminal-bash','terminal-pwsh',
    'pty','subprocess','fs-local','sandbox','sandbox-policy','sessions','session-log-deepseek',
    'plugin-package-inventory-deepseek'].map(id=>({id,disabled:true})),
  {id:'llm-deepseek', config:{baseURL:'https://api.deepseek.com',thinking:'enabled',reasoningEffort:dshTranslationModel.reasoningEffort,maxTokens:32768,
    streamIdleTimeoutMs:120000,retryPolicy:{mode:'normal',maxRetries:0}}},
  {id:'sdk-jsonrpc-server',config:{maxTokensAsSuccess:false}},
];

// The credential crosses a private stdin pipe, never argv, docker -e, disk or
// logging. The fresh home cannot load the user's coding tools or old sessions.
const bootstrap = String.raw`
const fs=require('node:fs'),os=require('node:os'),path=require('node:path'),cp=require('node:child_process');
let buffer=Buffer.alloc(0);
function first(chunk){buffer=Buffer.concat([buffer,chunk]);const i=buffer.indexOf(10);if(i<0){if(buffer.length>32768)process.exit(70);return;}
process.stdin.pause();process.stdin.removeListener('data',first);
try{const init=JSON.parse(buffer.subarray(0,i));const home=fs.mkdtempSync(path.join(os.tmpdir(),'dsh-translation-'));
const patch=path.join(home,'translation.json');fs.writeFileSync(patch,JSON.stringify(init.patch),{mode:0o600});
const child=cp.spawn(process.execPath,['--expose-internals','/opt/deepseek-harness/node_modules/.bin/dsh','--profile','sdk-minimal','--patch',patch],
{cwd:home,env:{PATH:'/usr/local/bin:/usr/bin:/bin',HOME:home,DSH_HOME:home,DEEPSEEK_API_KEY:init.key,DSH_SYSTEM_PROMPT:init.instructions},stdio:['pipe','pipe','pipe']});
init.key='';child.stdout.pipe(process.stdout);child.stderr.on('data',()=>{});
child.on('error',()=>process.exit(71));child.on('exit',code=>process.exit(code??72));
process.on('SIGTERM',()=>child.kill('SIGTERM'));process.on('SIGINT',()=>child.kill('SIGTERM'));
if(buffer.length>i+1)child.stdin.write(buffer.subarray(i+1));buffer=Buffer.alloc(0);
process.stdin.on('end',()=>child.kill('SIGTERM'));process.stdin.pipe(child.stdin);process.stdin.resume();
}catch{process.exit(73);}}
process.stdin.on('data',first);`;

export async function createDshTranslationClient() {
  const client = new HarnessSdkClient({
    workspace:'/tmp', container:'deepseek-harness-feishu-bridge-harness-1',
    harnessBinary:'/opt/deepseek-harness/node_modules/.bin/dsh',
    selection:dshTranslationModel,
    strictCompletion:true, maxTokens:32768,
    launch:async()=>{
      let key;
      try { key=(await exec('/usr/bin/security',['find-generic-password','-a',userInfo().username,'-s','codex-deepseek-api-key','-w'],{maxBuffer:4096,timeout:10000})).stdout.trim(); }
      catch {throw new Error('DSH_TRANSLATION_CREDENTIAL_UNAVAILABLE');}
      if(!key || /[\r\n\0]/u.test(key))throw new Error('DSH_TRANSLATION_CREDENTIAL_INVALID');
      const child=spawn('/Applications/Docker.app/Contents/Resources/bin/docker',[
        'exec','-i','deepseek-harness-feishu-bridge-harness-1','node','-e',bootstrap],
        {shell:false,stdio:['pipe','pipe','pipe'],env:executorEnvironment()});
      child.stdin.on('error',()=>{});
      child.stdin.write(JSON.stringify({key,patch:translationPatch,instructions:translationInstructions+'\n'+uncertaintyInstructions})+'\n'); key='';
      return child;
    },
  });
  return {
    async translate(input) {
      const result = await client.prompt(`translation-${randomUUID()}`,input,180000,{onEvent(event){
        if(event.type.startsWith('tool/') || event.type.startsWith('subagent/')) {
          client.child?.kill('SIGTERM');throw new Error('DSH_TRANSLATION_TOOL_REJECTED');
        }
      }});
      if(result.deliveryFiles.length || result.conversionResults.length)throw new Error('DSH_TRANSLATION_UNEXPECTED_TOOLS');
      return result.text;
    },
    close:()=>client.close(),
  };
}
