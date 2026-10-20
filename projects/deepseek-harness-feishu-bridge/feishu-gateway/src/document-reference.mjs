import {parseIntakeChoice} from './intake-flow.mjs';

// Resolve identity from authenticated Feishu metadata, never from an LLM or
// filenames in quoted text. A reference can select an intake, not grant consent.
export function isFileStart(text) {
  const value=String(text??'').trim();
  if(/[?？]|不|别|不要|暂缓|查看|看看|查询|为什么|怎么|能否|是否|确认|批准|授权|外发/u.test(value))return false;
  if(parseIntakeChoice(value))return true;
  return /^(?:请|请帮我|帮我)?(?:开始|继续|处理|整理|转换|入库)(?:处理|整理|转换)?(?:这份|这个|该|此)?(?:文件|资料|材料|文档)?(?:的)?(?:入库|处理|整理|转换)?[。！!]?$/u.test(value);
}

function currentReferences(state) {
  const w=state.workflowV11;
  const ids=new Set([w?.uploadId,w?.selectionPromptId,w?.progress?.messageId]);
  const matches=p=>p&&(!w||((!p.planId||p.planId===w.planId)&&(!p.sourceTaskId||p.sourceTaskId===w.sourceTaskId)));
  for(const p of [state.pendingWorkflowAction,state.pendingTranslationChoice,state.pendingPublication,state.pendingContentConsent,state.pendingTranslation]) {
    if(matches(p))for(const id of [p.promptId,p.delivery?.prompt_message_id,p.deliveryMessageId])ids.add(id);
  }
  ids.delete(undefined);ids.delete(null);return ids;
}

export class DocumentReferenceGuard {
  constructor({ownerId,appId,readMessage,send,audit=()=>{}}) {Object.assign(this,{ownerId,appId,readMessage,send,audit});}
  async resolve(message,state) {
    if(message.senderId!==this.ownerId||message.chatType!=='p2p')return {handled:true};
    const tell=text=>this.send(message.chatId,{text},{replyTo:message.messageId});
    const stop=async(reason,text)=>{this.audit({event:'document_reference_blocked',reason,messageId:message.messageId,workflowId:state.workflowV11?.id??null});await tell(text);return {handled:true};};
    if(message.resources?.length)return {message}; // A new attachment is explicit.
    const w=state.workflowV11;
    const completed=async()=>stop('completed_start',`《${w.filename}》已经完成。没有重新转换、整理或翻译。若要处理另一份资料，请引用那条文件消息回复“开始处理”，或上传新文件；查看已有成果可以说“查看笔记”或“查看译稿”。`);
    if(!message.replyToMessageId){
      if(w?.phase==='complete'&&isFileStart(message.content))return completed();
      return {message};
    }
    const references=currentReferences(state),visited=new Set();
    let id=message.replyToMessageId;
    for(let depth=0;depth<6;depth++) {
      if(references.has(id)){
        if(w?.phase==='complete'&&isFileStart(message.content))return completed();
        return {message};
      }
      if(!id||visited.has(id))break;
      visited.add(id);
      let raw;
      try {raw=await this.readMessage(id);}
      catch(error){return stop('lookup_failed',`${error?.code==='ETIMEDOUT'?'读取引用消息超时':'暂时无法读取你引用的消息'}，没有切换或启动任务。请稍后重试，或重新上传文件；不会按上一份资料执行。`);}
      if(raw?.message_id!==id||raw.chat_id!==message.chatId||raw.deleted
        ||!((raw.sender?.sender_type==='user'&&raw.sender?.id===this.ownerId&&raw.sender?.id_type==='open_id')
          ||(raw.sender?.sender_type==='app'&&raw.sender?.id===this.appId)))break;
      if(['file','image'].includes(raw.msg_type)&&raw.sender.sender_type==='user'){
        let body;try{body=JSON.parse(raw.body?.content??'{}');}catch{break;}
        const fileKey=raw.msg_type==='image'?body.image_key:body.file_key;
        if(typeof fileKey!=='string'||!fileKey||fileKey.length>300
          ||(raw.msg_type==='file'&&(typeof body.file_name!=='string'||!body.file_name)))break;
        if(!isFileStart(message.content))return stop('different_file',`你引用的是《${body.file_name??'图片'}》，不是当前资料。这次没有执行操作。要开始处理它，请引用文件回复“开始处理”；确认入库等回复必须对应本份预览。`);
        this.audit({event:'document_reference_resolved',messageId:message.messageId,uploadId:id,previousWorkflowId:w?.id??null});
        return {message:{...message,rawContentType:raw.msg_type,resourceMessageId:id,sourceUploadId:id,
          resources:[{type:raw.msg_type,fileKey,...(body.file_name?{fileName:body.file_name}:{})}]}};
      }
      // Do not use root_id: a conversation root is not the referenced document.
      id=raw.parent_id;
    }
    return stop('unresolved','没有确定你引用的消息属于哪份资料，这次没有执行操作。请直接引用原始文件回复“开始处理”；若要确认入库，请引用该资料最新的入库预览。不会自动沿用上一份任务。');
  }
}

export function createMessageReader(channel,{timeoutMs=15000}={}) {
  return async id=>{
    let timer;
    try {
      const result=await Promise.race([
        channel.rawClient.im.v1.message.get({path:{message_id:id},params:{user_id_type:'open_id'}}),
        new Promise((_,reject)=>{timer=setTimeout(()=>reject(Object.assign(new Error('REFERENCE_TIMEOUT'),{code:'ETIMEDOUT'})),timeoutMs);}),
      ]);
      if(result.code!==0||result.data?.items?.length!==1)throw Error('REFERENCE_UNAVAILABLE');
      return result.data.items[0];
    }finally{clearTimeout(timer);}
  };
}
