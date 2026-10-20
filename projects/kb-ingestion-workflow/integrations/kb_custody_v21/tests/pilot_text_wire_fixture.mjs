// Synthetic cross-language transport only; no real client or account is opened.
import '../../../../deepseek-harness-feishu-bridge/feishu-gateway/test/offline-network-guard.mjs';
import { runText } from '../../../../deepseek-harness-feishu-bridge/feishu-gateway/src/pilot-reading-guide.mjs';
import { writeFileSync } from 'node:fs';
let input='';for await(const c of process.stdin)input+=c;
const {packet,response,mode,marker}=JSON.parse(input);
const client={start:async()=>{},pinCurrentProcess:()=>{},close:async()=>{},ensureThread:async()=>'synthetic-thread',
  request:async(method)=>method==='account/read'?
    {requiresOpenaiAuth:true,account:{type:'chatgpt',planType:'pro',email:'synthetic-account@example.invalid'}}:
    {rateLimits:{limitId:'codex',primary:{usedPercent:1},credits:{hasCredits:false,unlimited:false}}},
  prompt:async()=>{
    writeFileSync(marker,'synthetic submission',{flag:'wx'});
    if(mode==='crash')process.exit(77);
    return {text:response,toolCalls:[]};
  }};
process.stdout.write(JSON.stringify(await runText(packet,{client,check:async()=>{}})));
