import {createDshTranslationClient} from './dsh-translation-client.mjs';
import {CodexAppServerClient} from './codex-app-server-client.mjs';
import {translationModel,directTranslationModel} from './workflow-models.mjs';
import {translationInstructions,reviewInstructions,reviewSchema,uncertaintyInstructions,uncertaintyReviewInstructions} from './translation-loop.mjs';

// Per-call wall clock limit, separate from the bounded whole-workflow grant.
export const CODEX_TRANSLATION_TIMEOUT_MS = 10 * 60_000;

export async function createTranslationExecutors({cwd,direct=false}) {
  if(direct){
    const instructions=[translationInstructions,uncertaintyInstructions,'依据原文、知识笔记及 DSH 译稿生成完整自然中文；笔记仅辅助术语和上下文，不能把笔记解释补入原文。'].join('\n');
    const codex=new CodexAppServerClient({codexBinary:'/Applications/ChatGPT.app/Contents/Resources/codex',cwd,
      ...directTranslationModel,ephemeral:true,allowedMcpServers:[],autoApprovedMcpServers:[],
      developerInstructions:instructions,baseInstructions:instructions,serviceName:'feishu-translation-direct'});
    return {translate:async input=>{
      const thread=await codex.ensureThread();
      try{
        const result=await codex.prompt(thread,input,{outputSchema:{type:'object',additionalProperties:false,required:['translation'],properties:{translation:{type:'string'}}},timeoutMs:CODEX_TRANSLATION_TIMEOUT_MS});
        if(result.toolCalls.length||result.deliveryFiles.length)throw Error('TRANSLATION_TOOL_REJECTED');
        return result.text;
      }finally{await codex.request('thread/unsubscribe',{threadId:thread},5000).catch(()=>{});codex.threadIds.delete(thread);}
    },close:()=>codex.close()};
  }
  const dsh=await createDshTranslationClient();
  const codex=new CodexAppServerClient({codexBinary:'/Applications/ChatGPT.app/Contents/Resources/codex',cwd,
    ...translationModel,
    ephemeral:true,allowedMcpServers:[],autoApprovedMcpServers:[],
    developerInstructions:[reviewInstructions,uncertaintyInstructions,uncertaintyReviewInstructions].join('\n'),baseInstructions:[reviewInstructions,uncertaintyInstructions,uncertaintyReviewInstructions].join('\n'),
    serviceName:'feishu-translation-review'});
  return {
    translate:input=>dsh.translate(input),
    review:async input=>{
      const thread=await codex.ensureThread();
      try{
        const result=await codex.prompt(thread,input,{outputSchema:reviewSchema,timeoutMs:CODEX_TRANSLATION_TIMEOUT_MS});
        if(result.toolCalls.length||result.deliveryFiles.length)throw Error('TRANSLATION_REVIEW_TOOL_REJECTED');
        return result.text;
      }finally{
        await codex.request('thread/unsubscribe',{threadId:thread},5000).catch(()=>{});
        codex.threadIds.delete(thread);
      }
    },
    close:async()=>{await Promise.allSettled([dsh.close(),codex.close()]);},
  };
}
