import {spawn} from 'node:child_process';
import {createInterface} from 'node:readline';
import {join} from 'node:path';
import {Readable} from 'node:stream';

const devClients=new WeakSet();

// This factory cannot construct a real SDK client. Only synthetic bytes are used.
export function createDevAttachmentClient(fixtures={}) {
  const channel={fixtures,downloadCalls:[]};
  channel.rawClient={im:{v1:{messageResource:{get:async({path})=>{
    channel.downloadCalls.push({...path});
    const fixture=channel.fixtures[path.file_key];
    if(!fixture)throw Error('mock resource unavailable');
    return {getReadableStream:()=>Readable.from((async function*(){
      const bytes=Buffer.from(fixture.bytes);
      yield bytes.subarray(0,Math.min(4,bytes.length));
      if(fixture.failAfterFirstChunk)throw Error('mock transport interrupted');
      yield bytes.subarray(Math.min(4,bytes.length));
    })())};
  }}}}};
  devClients.add(channel);
  return channel;
}

export const isDevAttachmentClient=client=>devClients.has(client);

export class CustodyDevBridge {
  constructor({worktree,runtime,pilotConfig=null}) { this.worktree=worktree;this.runtime=runtime;this.pilotConfig=pilotConfig; }
  async request(request,onDownload=null) {
    const script=join(this.worktree,'projects/kb-ingestion-workflow/integrations/kb_custody_v21/gateway_bridge.py');
    // The Python Runtime gate runs before any download request or DB use.
    const args=[script,'--worktree',this.worktree,'--runtime',this.runtime];
    if(this.pilotConfig)args.push('--pilot-config',this.pilotConfig);
    const child=spawn(process.env.KB_V21_PYTHON??'python3',args,
      {cwd:this.worktree,stdio:['pipe','pipe','pipe'],env:{PATH:process.env.PATH,LANG:'en_US.UTF-8',PYTHONIOENCODING:'utf-8',
        ...(this.pilotConfig&&process.env.PYTHONPATH?{PYTHONPATH:process.env.PYTHONPATH}:{})}});
    let stderr='',response=null,pipeError=null;
    child.stderr.on('data',chunk=>{stderr=(stderr+chunk.toString()).slice(-8000);});
    child.stdin.on('error',error=>{pipeError=error;});
    const exit=new Promise((resolve,reject)=>{child.on('error',reject);child.on('close',code=>resolve(code));});
    // Observe early spawn errors even while the stdout iterator is waiting.
    exit.catch(()=>{});
    const timer=setTimeout(()=>child.kill('SIGKILL'),60000);
    const lines=createInterface({input:child.stdout});
    try {
      child.stdin.write(JSON.stringify(request)+'\n');
      for await(const line of lines){
        const value=JSON.parse(line);
        if(value.action==='download'){
          let reply;
          try {
            if(!onDownload)throw Error('unexpected download request');
            reply={path:await onDownload(value.inbound_root)};
          } catch(error) {reply={error:error.message};}
          child.stdin.write(JSON.stringify(reply)+'\n');
        } else response=value;
      }
      const code=await exit;
      if(code!==0||response?.error)throw Error(response?.error?.message??stderr??`custody bridge exit ${code}`);
      if(!response||pipeError)throw pipeError??Error('custody bridge returned no result');
      return response.result;
    } finally {
      clearTimeout(timer);lines.close();child.stdin.end();
      if(child.exitCode===null&&child.signalCode===null)child.kill('SIGKILL');
      await exit.catch(()=>{});
    }
  }
  prepare(message){return this.request({operation:'prepare',message});}
  receive(message,resourceIndex,onDownload){return this.request({operation:'receive',message,resource_index:resourceIndex},onDownload);}
  async readInput(binding){return Buffer.from((await this.request({operation:'read',binding})).base64,'base64');}
  snapshot(){return this.request({operation:'snapshot'});}
  processNext(){return this.request({operation:'pilot_work'});}
  processingStatus(entryId){return this.request({operation:'pilot_status',entry_id:entryId});}
}
