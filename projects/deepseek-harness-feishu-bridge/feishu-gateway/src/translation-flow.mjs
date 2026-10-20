import {randomUUID} from 'node:crypto';
import {createContentConsentPolicy} from './content-consent.mjs';
import {consentMessageFromSdk} from './consent-controller.mjs';
import {translateAndReview,parseTranslation} from './translation-loop.mjs';
import {humanTime} from './user-facing-text.mjs';
import {isWorkflowHelp,workflowHelp,localWorkflowRequest,failureKind} from './workflow-feedback.mjs';
import {isTranslationDeliveryRequest,isRetranslationRequest} from './translation-delivery-intent.mjs';
export const translationConsentReplies = Object.freeze({
  '同意本份翻译和复核':'allow_cloud_body_once', '暂不翻译':'deny_cloud_body', '停止翻译':'revoke_cloud_body',
});
const label=x=>String(x).replace(/[\u0000-\u001f\u007f-\u009f\u202a-\u202e\u2066-\u2069]/gu,' ');
export function renderTranslationConsent(c) {
  if(c?.policy_version!=='dsh-translation-consent/v1' || c.recipient!=='deepseek_dsh_and_openai_codex_cloud'
    || c.model!=='deepseek-flash' || c.target_language!=='中文' || c.redaction!=='none' || c.max_revisions!==2
    || typeof c.source_filename!=='string' || !c.source_filename.trim()
    || !Array.isArray(c.purposes) || [...c.purposes].sort().join()!=='review,revise,translate'
    || c.access_window_seconds!==3600 || c.formal_write!==false || !Array.isArray(c.files) || !c.files.length
    || (c.source_uncertainty_policy!==undefined&&c.source_uncertainty_policy!=='annotated-source-v1')
    || (c.excluded_duplicate_files!==undefined&&(!Array.isArray(c.excluded_duplicate_files)||c.excluded_duplicate_files.length>50||c.excluded_duplicate_files.some(x=>typeof x!=='string')))
    || c.files.length>50 || !Number.isSafeInteger(c.total_bytes) || c.total_bytes<=0 || c.total_bytes>128*1024
    || c.files.some(x=>typeof x.relative_path!=='string'||!Number.isSafeInteger(x.size_bytes)||x.size_bytes<0||!/^[a-f0-9]{64}$/u.test(x.sha256??''))
    || c.files.reduce((n,x)=>n+x.size_bytes,0)!==c.total_bytes)throw Error('TRANSLATION_INVALID_CONSENT');
  const text=[`请确认这份材料的翻译与复核：${label(c.source_filename)}`,
    '范围：以下转换文本的完整正文，不发送其他附件。',...c.files.map(x=>'- '+label(x.relative_path)),
    'DeepSeek 云端：由本机 DSH 调用 V4.1 Flash，逐段翻译；收到 Codex 的意见后修改，最多修改两轮。',
    'OpenAI 云端：由 Codex 对照原文和译文复核；有修改时，再检查修改后的版本。原文、译文及复核意见会在这两个处理步骤间传递，未经脱敏，云端服务可能保留处理记录。',
    ...(c.excluded_duplicate_files?.length?[`已排除 ${c.excluded_duplicate_files.length} 个经本机核对的重复正文副本，不重复翻译；原文件保留。`]:[]),
    ...(c.source_uncertainty_policy==='annotated-source-v1'?['原文有口误、转写错误或歧义时，保留原句、谨慎译文和“原文存疑”说明；复核确认标注忠实后可交付。真正的漏译、误译仍须修正，不能用存疑标注替代。']:[]),
    '只有所有段落复核通过才会把原文与中文译文对照 PDF 发回本飞书私聊。复核通过指未发现待修正的译文错误，不表示原文无疑点，也不保证绝对无误；两轮修改仍未通过就暂停，不自动换模型。',
    `请在 ${humanTime(c.expires_at)} 前确认这条提示；过期后可以说“重新发送翻译确认”。`,
    '确认通过后，可用于翻译与复核的处理时间为 1 小时，与上面的确认截止时间不同。停止仅阻止后续处理，已发送内容无法收回。不会修改原件、已入库文件或自动把译稿写入知识库。',
    '请引用回复本条消息：“同意本份翻译和复核”或“暂不翻译”。处理期间可以说“停止翻译”。选择是否需要译稿不等于同意外发。'].join('\n');
  if(text.length>12000)throw Error('TRANSLATION_CONSENT_TOO_LONG');
  return text;
}
export class TranslationFlow {
  constructor({client,signingKey,ownerId,save,send,sendPdf,createExecutors,progress=null,requestUpgrade=null,now=()=>Math.floor(Date.now()/1000)}) {
    Object.assign(this,{client,ownerId,save,send,sendPdf,createExecutors});
    this.now=now;
    this.policy=createContentConsentPolicy({signingKey,ownerId,replies:translationConsentReplies,now});
    this.running=null;
    this.progress=progress;
    this.requestUpgrade=requestUpgrade;
  }
  async beginWorkflow(message,state,w){
    if(this.running)throw Error('TRANSLATION_ALREADY_RUNNING');
    const existing=await this.client('workflow-translation-existing',{workflow_id:w.id});
    if(existing.found){
      if(existing.result.plan_id!==w.planId||existing.result.source_task_id!==w.sourceTaskId)throw Error('TRANSLATION_SOURCE_MISMATCH');
      const p=state.pendingTranslation={workflowId:w.id,planId:w.planId,sourceTaskId:w.sourceTaskId,chatId:message.chatId,consent:existing.consent,phase:'delivering'};
      await this.save();await this.progress?.stage(state,'5.4',{remaining:['5.4','6'],detail:'发现已有完成译稿，直接沿用，没有重译或再次语义复核。'});
      let sent;
      try{sent=await this.sendPdf(message,p.consent.consent_id,existing.result.pdf_base64);if(!sent?.messageId)throw Error('TRANSLATION_DELIVERY_UNCERTAIN');}
      catch(error){p.phase='delivery_uncertain';await this.save();throw error;}
      if(!sent?.messageId)throw Error('TRANSLATION_DELIVERY_UNCERTAIN');
      p.phase='completed';await this.save();
      await this.client('workflow-translation-delivery',{workflow_id:w.id,consent_id:p.consent.consent_id,prompt_message_id:sent.messageId});
      await this.linkWorkflow(message,state);return;
    }
    if(existing.language==='chinese'){
      w.phase='complete';await this.progress?.set(state,{status:'complete',remaining:[],detail:'本机识别原文为中文，无需再译为中文；知识笔记已入库。'});
      await this.tell(message,'知识笔记已入库。本机识别原文就是中文，因此跳过中文全文翻译；已保留原文。');return;
    }
    const grant=await this.client('workflow-grant',{workflow_id:w.id,operation:'translation'});
    if(grant.plan_id!==w.planId||grant.source_task_id!==w.sourceTaskId)throw Error('TRANSLATION_SOURCE_MISMATCH');
    const pending={phase:'approved',workflowId:w.id,planId:w.planId,sourceTaskId:w.sourceTaskId,chatId:message.chatId,consent:grant.consent};
    state.pendingTranslation=pending;await this.save();
    this.startWorkflow(message,state);
  }
  startWorkflow(message,state,direct=false){
    const run={pending:state.pendingTranslation,cancelled:false,executors:null,direct};this.running=run;
    run.promise=this.execute(message,state,run).catch(()=>{});
  }
  async linkWorkflow(message,state){
    await this.progress?.stage(state,'6',{remaining:['6']});
    const result=await this.client('workflow-link',{workflow_id:state.workflowV11.id});
    if(result.state!=='linked'||result.consent_id!==state.pendingTranslation.consent.consent_id)throw Error('TRANSLATION_LINK_UNCERTAIN');
    state.workflowV11.phase='complete';await this.progress?.set(state,{status:'complete',remaining:[],detail:'知识笔记、原文和采用译稿已挂接，全部完成。'});
    await this.tell(message,'本份处理全部完成：知识笔记已入库，对照译稿已发送，并已挂接到同一份知识库资料中。');
  }
  tell(message,text){return this.send(message.chatId,{text},{replyTo:message.messageId});}
  async begin(message,state,offer=null) {
    if(message.senderId!==this.ownerId||message.chatType!=='p2p')return;
    if(state.workflowV11&&!state.workflowV11.publicationId){
      await this.tell(message,'这份资料还没有完成知识笔记入库。先查看笔记并确认入库；之后再按你已选择的用途处理译稿。');return;
    }
    if(this.running){await this.tell(message,'译稿正在处理中，完成后会发给你。需要中止可以说“停止翻译”。');return;}
    if(state.workflowV11?.choice?.purpose===2&&!isTranslationDeliveryRequest(message.content)){
      await this.requestUpgrade?.(message,state);return;
    }
    if(state.workflowV11?.choice?.purpose===3&&isRetranslationRequest(message.content)){
      const w=state.workflowV11;
      const grant=await this.client('workflow-translation-restart',{workflow_id:w.id,actor_id:message.senderId,chat_id:message.chatId,message_id:message.messageId,issued_at:Math.floor(message.createTime/1000)});
      if(grant.plan_id!==w.planId||grant.source_task_id!==w.sourceTaskId)throw Error('TRANSLATION_SOURCE_MISMATCH');
      state.pendingTranslation={phase:'approved',workflowId:w.id,planId:w.planId,sourceTaskId:w.sourceTaskId,chatId:message.chatId,consent:grant.consent};
      w.phase='running';await this.progress?.stage(state,'5',{remaining:['5','5.1','5.4','6'],detail:'按你的明确要求重新翻译；旧稿和知识笔记保留。'});
      await this.save();this.startWorkflow(message,state);return;
    }
    const planId=state.pendingCuration?.planId??state.activeCurationRun?.planId;
    const sourceTaskId=state.pendingCuration?.sourceTaskId??state.lastCandidateTask?.taskId;
    if(!planId||!sourceTaskId||state.pendingAttachments?.length){await this.tell(message,'请先完成这份材料的转换和入库计划，再申请完整译稿。');return;}
    const prior=state.pendingTranslation;
    if(prior?.workflowId&&isTranslationDeliveryRequest(message.content)){await this.deliverExisting(message,state);return;}
    if(state.workflowV11?.choice?.purpose===3&&!prior&&!isTranslationDeliveryRequest(message.content)){
      await this.beginWorkflow(message,state,state.workflowV11);return;
    }
    if(prior&&['completed','delivery_uncertain',...(prior.workflowId?['awaiting_owner']:[])].includes(prior.phase)
      &&prior.planId===planId&&prior.sourceTaskId===sourceTaskId&&!isRetranslationRequest(message.content)){
      await this.deliverExisting(message,state);return;
    }
    if(isTranslationDeliveryRequest(message.content)){
      await this.tell(message,'当前材料还没有可发送的完成译稿。这次只查看，不会自动开始或重新翻译。');return;
    }
    if(state.activeCurationRun?.phase==='running'){await this.tell(message,'当前正文整理还在运行。请等本轮结束后申请译稿，或先停止整理。');return;}
    await this.cancel(state);
    offer??=await this.client('translation-offer',{plan_id:planId,source_task_id:sourceTaskId});
    if(offer.plan_id!==planId||offer.source_task_id!==sourceTaskId)throw Error('TRANSLATION_SOURCE_MISMATCH');
    const pending={phase:'preparing',planId,sourceTaskId,chatId:message.chatId};
    state.pendingTranslation=pending;await this.save();
    const c=await this.client('translation-prepare',{plan_id:planId,source_task_id:sourceTaskId,scope_hash:offer.scope_hash,request_id:'translation:'+randomUUID()});
    if(c.plan_id!==planId||c.source_task_id!==sourceTaskId)throw Error('TRANSLATION_SOURCE_MISMATCH');
    pending.consent=c;pending.phase='sending';await this.save();
    const sent=await this.tell(message,renderTranslationConsent(c));
    if(!sent?.messageId||(sent.chunkIds?.length??0)>1)throw Error('TRANSLATION_CONSENT_DELIVERY_UNCERTAIN');
    pending.delivery=this.policy.presented({consent:c,chatId:message.chatId,promptMessageId:sent.messageId});
    pending.phase='presenting';await this.save();
    const receipt=await this.client('translation-proof',{proof:pending.delivery});
    if(receipt.consent_id!==c.consent_id||receipt.state!=='presented')throw Error('TRANSLATION_RECEIPT_MISMATCH');
    pending.phase='awaiting';await this.save();
  }
  async deliverExisting(message,state){
    const p=state.pendingTranslation;
    if(this.delivering){await this.tell(message,'对照稿正在发送，请稍等；没有重新翻译。');return;}
    if(!p?.consent?.consent_id||p.chatId!==message.chatId)throw Error('TRANSLATION_SOURCE_MISMATCH');
    this.delivering=true;
    try{
      const result=p.workflowId?await this.client('workflow-translation-copy',{workflow_id:p.workflowId}):await this.client('translation-delivery',{consent_id:p.consent.consent_id,plan_id:p.planId,source_task_id:p.sourceTaskId});
      if(result.consent_id!==p.consent.consent_id||result.plan_id!==p.planId||result.source_task_id!==p.sourceTaskId
        ||!['completed',...(p.workflowId?['awaiting_owner']:[])].includes(result.state)||result.formal_write!==false||result.reused!==true||result.layout!=='bilingual-v1')throw Error('TRANSLATION_SOURCE_MISMATCH');
      if(state.pendingTranslation!==p||p.planId!==(state.pendingCuration?.planId??state.activeCurationRun?.planId)
        ||p.sourceTaskId!==(state.pendingCuration?.sourceTaskId??state.lastCandidateTask?.taskId)||state.pendingAttachments?.length)throw Error('TRANSLATION_SOURCE_MISMATCH');
      const sent=await this.sendPdf(message,p.consent.consent_id,result.pdf_base64);
      if(!sent?.messageId)throw Error('TRANSLATION_DELIVERY_UNCERTAIN');
      p.phase='completed';p.latestDeliveryMessageId=sent.messageId;await this.save();
      if(p.workflowId){
        if(result.state==='awaiting_owner'){
          p.phase='awaiting_owner';await this.save();
          const prompt=await this.tell(message,'已重发保存的对照稿，没有重新翻译。它仍有未通过复核的部分，请看稿内说明。请引用回复本条：1 采用当前 DSH 稿；2 让 Astra / medium 结合原文、笔记和 DSH 稿直接翻译。');
          if(!prompt?.messageId)throw Error('TRANSLATION_DELIVERY_UNCERTAIN');
          await this.client('workflow-translation-delivery',{workflow_id:p.workflowId,consent_id:p.consent.consent_id,prompt_message_id:prompt.messageId});
          await this.progress?.set(state,{status:'waiting',detail:'对照稿已重发，等待你选择。'});
        }else if(!['complete','cancelled'].includes(state.workflowV11.phase)){
          await this.client('workflow-translation-delivery',{workflow_id:p.workflowId,consent_id:p.consent.consent_id,prompt_message_id:sent.messageId});
          await this.linkWorkflow(message,state);
        }else await this.tell(message,'已有对照稿已重发，没有重新翻译、复核或改动知识库。');
        return;
      }
      await this.tell(message,'中外文对照稿已发送，使用之前完成的 DSH 译文，没有重新翻译或让 Codex 改写；也没有重新进行语义复核。已有入库文件不变。');
    }catch(error){
      await this.tell(message,failureKind(error)==='timeout'?'读取或发送对照稿超时，发送结果可能尚未确认，请先检查是否收到。这次没有重新翻译，也不会自动重试。':'已有译稿的读取、核验或发送未完成，请先核对是否收到。没有自动重新翻译或重复发送，原译稿保留。');
    }finally{this.delivering=false;}
  }
  async cancel(state,reason='user') {
    const p=state.pendingTranslation;
    if(!p)return;
    // Shutdown is not a new owner cancellation. Preserve existing waits/failures
    // and their usable continuation/delivery controls across a restart.
    if(reason==='restart'&&['needs_attention','awaiting_owner','completed','delivery_uncertain'].includes(p.phase))return;
    if(p.consent?.consent_id)await this.client('translation-invalidate',{consent_id:p.consent.consent_id});
    if(this.running?.pending===p){this.running.cancelled=true;await this.running.executors?.close();}
    if(!['completed','delivery_uncertain'].includes(p.phase)){
      if(reason==='restart'&&p.phase!=='cancelled')p.recoveryNotice=true;
      p.phase='cancelled';
    }
    await this.save();
  }
  async recover(chats){
    for(const state of Object.values(chats)){
      const p=state.pendingTranslation;
      if(p?.workflowId&&p.phase==='cancelled'&&p.recoveryNotice&&state.workflowV11?.id===p.workflowId&&state.workflowV11.phase!=='cancelled'){
        // Migrate old shutdown's misleading cancelled label, never an owner stop.
        p.phase='needs_attention';await this.save();
      }
      if(p&&!['completed','delivery_uncertain','awaiting_owner','needs_attention'].includes(p.phase))await this.cancel(state,'restart');
    }
  }
  async notifyRecovery(message,state){
    const p=state.pendingTranslation;
    if(p?.failureNoticeUncertain&&message.senderId===this.ownerId&&message.chatType==='p2p'){
      try{const sent=await this.tell(message,p.failureMessage);p.failureNoticeUncertain=!sent?.messageId;await this.save();}catch{}
    }
    if(!p?.recoveryNotice||message.senderId!==this.ownerId||message.chatType!=='p2p')return;
    await this.tell(message,p.workflowId?'机器人重启后，未完成的翻译已暂停，已有段落保留，没有自动继续。回复“继续翻译”可从保存的断点续接；已通过的段落不重做，未通过的仍保留待判断状态。':'机器人重启后，之前未完成的翻译流程已停止，旧确认不能再使用；没有自动继续翻译。若仍需要译稿，请说“重新发送翻译确认”，再确认新提示。已入库资料不变。');
    p.recoveryNotice=false;await this.save();
  }
  async showStatus(message,state){
    const p=state.pendingTranslation;
    if(!p?.consent?.consent_id){await this.tell(message,'当前没有已开始的翻译任务。需要译稿时可以说“需要完整中文译稿”；查询不会启动翻译。');return;}
    const value=await this.client('translation-status',{consent_id:p.consent.consent_id});
    const label=({awaiting:'等待你确认正文外发范围',running:'正在翻译或复核',completed:'译稿已发送',awaiting_owner:'完整对照稿已发送，等待你采用或选择 Astra 直接翻译',delivery_uncertain:'译稿已生成，发送结果需要核对',cancelled:'已停止',needs_attention:'已暂停，需要检查'})[p.phase]??'尚未开始翻译';
    await this.tell(message,`${label}，已有 ${value.completed_parts} 段完成本轮处理（不等于全部复核通过）。知识笔记未被改写。${p.workflowId&&['needs_attention','cancelled'].includes(p.phase)?'\n'+(p.failureMessage??'任务不会自动继续。回复“继续翻译”可从保存的断点续接。'):''}`);
  }
  async handle(message,state,{cancel=false}={}) {
    if(message.senderId!==this.ownerId||message.chatType!=='p2p')return true;
    const text=(message.content??'').trim();const p=state.pendingTranslation;
    if(p?.workflowId&&['继续翻译','从断点继续翻译','接着翻译','恢复翻译'].includes(text)){
      if(this.running){await this.tell(message,'翻译仍在进行，没有重复启动。可以查询“翻译进度”。');return true;}
      const w=state.workflowV11;
      if(cancel||!w||w.id!==p.workflowId||w.phase==='cancelled'||!['needs_attention','cancelled'].includes(p.phase)){
        await this.tell(message,'当前不是可续接的暂停翻译；没有重新翻译。请先查询“翻译进度”。');return true;
      }
      try{
        const grant=await this.client('workflow-translation-resume',{workflow_id:w.id,actor_id:message.senderId,chat_id:message.chatId,
          message_id:message.messageId,issued_at:Math.floor(message.createTime/1000)});
        if(grant.plan_id!==w.planId||grant.source_task_id!==w.sourceTaskId)throw Error('TRANSLATION_SOURCE_MISMATCH');
        state.pendingTranslation={phase:'approved',workflowId:w.id,planId:w.planId,sourceTaskId:w.sourceTaskId,chatId:message.chatId,
          consent:grant.consent,completedParts:grant.completed_parts,totalParts:grant.total_parts};
        w.phase='running';await this.save();
        const stage=grant.resume_phase==='reviewing'?'5.1':grant.resume_phase==='finish'?'5.4':'5';
        await this.progress?.stage(state,stage,{remaining:['5','5.1','5.4','6'],detail:'从保存的断点继续，已有段落不重做。'});
        await this.tell(message,`已从保存的断点继续${grant.completed_parts<grant.total_parts?`，当前第 ${grant.completed_parts+1} / ${grant.total_parts} 段`:''}。${grant.resume_phase==='reviewing'?'本段已有译文，只重新进行中断的复核。':''}已通过的段落不重做，两轮未通过的段落仍保留待判断。现在不需要操作。`).catch(()=>{});
        this.startWorkflow(message,state);
      }catch(error){
        await this.tell(message,failureKind(error)==='timeout'?'续接请求调用超时，结果需核对；没有自动重复提交。':'本次续接未通过核验或未完成。已有译文保留，没有从头重译；请核对任务状态。');
      }
      return true;
    }
    if(p?.workflowId&&p.phase==='awaiting_owner'&&['1','2'].includes(text)){
      if(this.running)return true;
      const result=await this.client('workflow-translation-decide',{workflow_id:p.workflowId,actor_id:message.senderId,chat_id:message.chatId,
        message_id:message.messageId,prompt_message_id:message.replyToMessageId??'',issued_at:Math.floor(message.createTime/1000),action:text==='1'?'accept':'direct'});
      if(result.consent_id!==p.consent.consent_id)throw Error('TRANSLATION_SOURCE_MISMATCH');
      if(text==='1'){
        p.phase='completed';await this.save();await this.linkWorkflow(message,state);
      }else{
        await this.progress?.stage(state,'direct',{remaining:['direct','5.4','6']});p.phase='approved';await this.save();this.startWorkflow(message,state,true);
      }
      return true;
    }
    if(p?.workflowId&&text==='重试挂接'&&p.phase==='completed'){await this.linkWorkflow(message,state);return true;}
    if(cancel||message.resources?.length){await this.cancel(state);return false;}
    if(message.rawContentType==='text'&&(isTranslationDeliveryRequest(text)||isRetranslationRequest(text))){await this.begin(message,state);return true;}
    if(message.rawContentType==='text'&&isWorkflowHelp(text)){await this.tell(message,workflowHelp(state,{now:this.now(),text}));return true;}
    if(message.rawContentType==='text'&&localWorkflowRequest(text))return false;
    if(['停止翻译','暂停翻译','取消翻译','别翻译了','不要继续翻译'].includes(text)
      || (text==='先停一下'&&p&&['awaiting','approved','running'].includes(p.phase))){await this.cancel(state);await this.tell(message,'已停止后续翻译和复核，已有文件保留；已发往云端的内容无法收回。');return true;}
    if(['译稿状态','翻译进度','译稿完成了吗'].includes(text)){await this.showStatus(message,state);return true;}
    const quoted=!!p?.delivery?.prompt_message_id&&message.replyToMessageId===p.delivery.prompt_message_id;
    if(!Object.hasOwn(translationConsentReplies,text)&&!quoted)return false;
    if(!p||p.phase!=='awaiting'||p.planId!==(state.pendingCuration?.planId??state.activeCurationRun?.planId)){
      await this.tell(message,'这条翻译确认已失效或已处理。请重新说“需要完整中文译稿”，不会沿用旧授权。');return true;
    }
    const proof=this.policy.decide({consent:p.consent,delivery:p.delivery,message:consentMessageFromSdk(message)});
    if(!proof){
      const detail=this.now()>=p.consent.expires_at
        ? `${quoted?'你引用的是对应的翻译提示，但它':'这份翻译确认提示'}已在 ${humanTime(p.consent.expires_at)} 过期。不是授权措辞不对。请说“重新发送翻译确认”，再确认新消息；这次没有开始翻译。`
        : !quoted?'授权还未记录：这条回复没有关联到翻译确认消息。请长按以“请确认这份材料的翻译与复核”开头的消息，选择“回复”，输入“同意本份翻译和复核”；也可以说“重新发送翻译确认”。'
        : '这条回复未能通过授权核验（可能发送时间过早或过久，或没有使用明确确认文字）。没有开始翻译。请说“重新发送翻译确认”，再引用新消息回复“同意本份翻译和复核”。';
      await this.tell(message,detail);return true;
    }
    p.phase='deciding';await this.save();
    const receipt=await this.client('translation-proof',{proof});
    const expected=proof.kind==='allow_cloud_body_once'?'approved':'denied';
    if(receipt.consent_id!==p.consent.consent_id||receipt.state!==expected)throw Error('TRANSLATION_RECEIPT_MISMATCH');
    p.phase=expected;await this.save();
    if(expected==='denied'){await this.tell(message,'暂不翻译，不外发正文；已有入库内容不变。');return true;}
    if(this.running){await this.cancel(state);await this.tell(message,'已有另一份译稿正在处理。本次没有开始，请等完成后重新申请。');return true;}
    const running={pending:p,cancelled:false,executors:null};this.running=running;
    // Do not hold the incoming-message handler: owner stop/revoke remains live.
    running.promise=this.execute(message,state,running).catch(()=>{});
    return true;
  }
  async execute(message,state,run) {
    const p=run.pending;const id=p.consent.consent_id;
    const active=async()=>{
      if(run.cancelled||state.pendingTranslation!==p||p.planId!==(state.pendingCuration?.planId??state.activeCurationRun?.planId))throw Error('TRANSLATION_CANCELLED');
      const check=await this.client('translation-check',{consent_id:id});
      if(check.consent_id!==id||check.active!==true)throw Error('TRANSLATION_AUTHORITY_MISMATCH');
    };
    try{
      p.phase='running';p.translationMode=run.direct?'codex_direct':'dsh_review';await this.save();await active();
      if(!p.workflowId)await this.tell(message,'开始翻译这份材料。DSH 负责翻译和修改，Codex 负责复核；全部通过后再发译稿。需要中止可说“停止翻译”。');
      run.executors=await this.createExecutors({direct:!!run.direct});await active();
      for(;;){
        await active();const unit=await this.client('translation-pull',{consent_id:id});
        if(unit.consent_id!==id)throw Error('TRANSLATION_SOURCE_MISMATCH');
        if(unit.done===true)break;
        if(!Number.isSafeInteger(unit.index)||!Number.isSafeInteger(unit.total))throw Error('TRANSLATION_INVALID_UNIT');
        p.completedParts=unit.index;p.totalParts=unit.total;await this.save();
        if(!p.workflowId)await this.tell(message,`正在处理第 ${unit.index+1} / ${unit.total} 段。`);
        const checkpoint=async checkpoint=>{
            await active();const saved=await this.client('translation-record',{consent_id:id,index:unit.index,checkpoint});
            if(saved.consent_id!==id||saved.saved!==true)throw Error('TRANSLATION_SAVE_UNCERTAIN');
            p.currentStep=checkpoint.phase;await this.save();
            if(p.workflowId){
              const stage=run.direct?'direct':checkpoint.phase==='revising'?'5.2':checkpoint.phase==='reviewing'?checkpoint.revision?'5.3':'5.1':checkpoint.phase==='translating'?'5':null;
              if(stage){
                const remaining=run.direct?['direct','5.4','6']:stage==='5.2'?['5.2','5.3','5.4','6']:stage==='5.3'?['5.3','5.4','6']:stage==='5.1'&&unit.index+1===unit.total?['5.1','5.4','6']:['5','5.1','5.4','6'];
                await this.progress?.stage(state,stage,{remaining,detail:`第 ${unit.index+1} / ${unit.total} 段${checkpoint.revision?`；第 ${checkpoint.revision} / 2 轮修正`:''}`});
              }
            }else if(checkpoint.phase==='revising')await this.tell(message,`第 ${unit.index+1} 段发现需修正之处，DSH 正在按复核意见修改，之后会再次复核。`);
          };
        if(run.direct){
          await checkpoint({phase:'translating',revision:0});
          const translation=parseTranslation(await run.executors.translate(JSON.stringify({source:unit.source,knowledge_notes:unit.notes,dsh_draft:unit.dsh_draft})));
          await active();await checkpoint({phase:'direct_completed',revision:0,translation});
        }else {
          if(unit.resume){p.currentStep=unit.resume.phase;await this.save();}
          await translateAndReview({source:unit.source,notes:unit.notes??'',resume:unit.resume??null,allowOwnerDecision:!!p.workflowId,translate:run.executors.translate,review:run.executors.review,authorize:active,checkpoint});
        }
      }
      await active();const result=await this.client('translation-finish',{consent_id:id});
      if(run.cancelled||result.consent_id!==id||!['completed',...(p.workflowId?['awaiting_owner']:[])].includes(result.state)||result.formal_write!==false)throw Error('TRANSLATION_INCOMPLETE');
      p.phase='delivering';await this.save();
      if(p.workflowId)await this.progress?.stage(state,'5.4',{remaining:['5.4','6']});
      const sent=await this.sendPdf(message,id,result.pdf_base64);
      if(!sent?.messageId)throw Error('TRANSLATION_DELIVERY_UNCERTAIN');
      p.phase='completed';p.deliveryMessageId=sent.messageId;await this.save();
      if(p.workflowId){
        if(result.state==='awaiting_owner'){
          p.phase='awaiting_owner';await this.save();
          const prompt=await this.tell(message,`两轮修正后仍有未通过的部分，完整对照稿已发送。\n${result.issues.slice(0,4).map(x=>'- '+label(x).slice(0,240)).join('\n')}\n请引用回复本条：1 采用当前 DSH 稿（不等于机器复核通过）；2 允许 Codex Astra / medium 结合原文、笔记和 DSH 稿直接翻译。`);
          if(!prompt?.messageId)throw Error('TRANSLATION_DELIVERY_UNCERTAIN');
          await this.client('workflow-translation-delivery',{workflow_id:p.workflowId,consent_id:id,prompt_message_id:prompt.messageId});
          await this.progress?.set(state,{status:'waiting',remaining:['6'],detail:'采用当前稿还差挂接 1 步；选 Astra 直接翻译则还差翻译、发送、挂接 3 步。'});
        }else{
          await this.client('workflow-translation-delivery',{workflow_id:p.workflowId,consent_id:id,prompt_message_id:sent.messageId});
          await this.linkWorkflow(message,state);
        }
        return;
      }
      await this.tell(message,`原文与中文译文对照稿已发给你。DSH 完成翻译及必要修改，Codex 已对照原文逐段复核，本轮未发现待修正的译文错误。${result.source_warning_count?`有 ${result.source_warning_count} 处原文存疑，已保留原句、谨慎译文和说明，请注意核对。`:''}机器复核不能保证绝对无误。已有入库文件未改动，译稿尚未写入知识库。`);
    }catch(error){
      const cancelled=run.cancelled||p.phase==='cancelled';
      if(p.phase==='delivering')p.phase='delivery_uncertain';
      else if(p.phase!=='completed')p.phase=cancelled?'cancelled':'needs_attention';
      await this.save();await this.client('translation-invalidate',{consent_id:id}).catch(()=>{});
      if(p.workflowId){
        if(state.workflowV11?.id===p.workflowId&&!cancelled)state.workflowV11.phase='stopped';
        const step=p.currentStep==='reviewing'?'Codex 复核':p.currentStep==='revising'?'DSH 修正':'翻译处理';
        const reason=failureKind(error)==='timeout'?'调用超时':/TRANSLATION_NOT_AUTHORIZED/u.test(error.message)?'本次处理授权已到期或已停止':'未能完成';
        p.failedAt=this.now();p.failureReason=reason;
        p.failureMessage=p.phase==='completed'?'对照稿已发送，但挂接未完成。可说“重试挂接”；不会重新翻译。':p.phase==='delivery_uncertain'?'译稿已生成，但发送结果不确定。请先查询译稿，不要重新翻译。':run.direct?`Codex 直接翻译${reason}，已停止，已有结果保留。直接翻译分支暂不支持断点续接，请先核对保存记录；不会自动从头重译。`:
          `翻译已停止，不会自动继续。${p.totalParts?`第 ${(p.completedParts??0)+1} / ${p.totalParts} 段的`:''}${step}${reason}。\n已有译文、复核记录和已入库笔记均保留。回复“继续翻译”可从中断处续接，已通过的段落不重做；若当前译文已保存，只重新执行中断的复核。`;
        await this.save();
        await this.progress?.set(state,{status:'stopped',detail:p.failureMessage,next:p.phase==='needs_attention'&&!run.direct?'回复“继续翻译”从断点续接':'核对保存结果'});
        if(!cancelled){
          p.failureNoticeUncertain=true;await this.save();
          try{const sent=await this.tell(message,p.failureMessage);p.failureNoticeUncertain=!sent?.messageId;}catch{}
          await this.save();
        }
        return;
      }
      if(!cancelled)await this.tell(message,p.phase==='delivery_uncertain'?'译稿已生成，但飞书发送结果不确定。我不会自动重复发送，请先检查是否收到文件。':
        failureKind(error)==='timeout'?'翻译或复核调用超时，已暂停，没有自动重试，也不会输出未经完整复核的译稿。已有结果保留，已入库文件不变。':
        ['TRANSLATION_INVALID_SOURCE_WARNING','TRANSLATION_UNREVIEWED_SOURCE_WARNING'].includes(error.message)?'原文存疑标注未能与原句、谨慎译文或说明完整对应，已暂停交付；不会把未核对的标注当作通过。原件和已入库文件不变。':
        ['TRANSLATION_INVALID_RESULT','TRANSLATION_INVALID_REVIEW'].includes(error.message)?'翻译或复核返回的结果格式不符合约定，已暂停。这不等于已判定原文有错；没有发送不完整译稿，原件和已入库文件不变。':
        error.message==='TRANSLATION_REVIEW_NOT_PASSED'?`第 ${(p.completedParts??0)+1} 段经两轮修改后仍未通过复核，已暂停。这不是调用超时。\n${(error.reviewIssues??[]).slice(0,4).map(x=>'- '+label(x).slice(0,220)).join('\n')}\n不会作为完成稿发送，原件和已入库文件不变。`:
          p.phase==='completed'?'译稿文件已发送，但完成提示可能未送达。请查看收到的 PDF；不会重复翻译。':'翻译或复核未获得完整有效结果，已暂停，没有输出未经复核的译稿，也不会自动重发正文。原件和已入库文件不变。').catch(()=>{});
    }finally{await run.executors?.close().catch(()=>{});if(this.running===run)this.running=null;}
  }
}
