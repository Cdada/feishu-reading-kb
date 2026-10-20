// Local stdio fixture: no credentials, real model or network access.
import './offline-network-guard.mjs';
import { MODEL } from '../src/pilot-reading-guide.mjs';
import { createInterface } from 'node:readline';
const send=(message)=>process.stdout.write(JSON.stringify(message)+'\n');
const profile={model:MODEL,reasoningEffort:'medium',modelProvider:'openai'};
for await (const line of createInterface({input:process.stdin})) {
  const message=JSON.parse(line);
  if(message.id==null) continue;
  let result;
  switch(message.method) {
    case 'initialize': result={}; break;
    case 'config/read': result={config:{model_provider:'openai',model:profile.model,model_reasoning_effort:'medium'}}; break;
    case 'account/read': result={requiresOpenaiAuth:true,account:{type:'chatgpt',planType:'pro',email:'synthetic@example.invalid'}}; break;
    case 'account/rateLimits/read': result={rateLimits:{limitId:'codex',primary:{usedPercent:10},credits:{hasCredits:false,unlimited:false}}}; break;
    case 'thread/start': result={thread:{id:'synthetic-thread'},...profile}; break;
    case 'turn/start': result={turn:{id:'synthetic-turn'}}; break;
    default: throw Error('unexpected synthetic protocol method');
  }
  send({id:message.id,result});
  if(message.method==='turn/start') {
    send({method:'item/completed',params:{threadId:'synthetic-thread',turnId:'synthetic-turn',
      item:{type:'agentMessage',phase:'final_answer',text:'{}'}}});
    send({method:'turn/completed',params:{threadId:'synthetic-thread',turn:{id:'synthetic-turn',status:'completed'}}});
  }
}
