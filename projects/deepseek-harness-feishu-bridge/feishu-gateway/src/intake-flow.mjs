import {initialStages,progressText} from './workflow-progress.mjs';
import {failureKind} from './workflow-feedback.mjs';

export function parseIntakeChoice(text) {
  const value=String(text??'').trim();
  // Codes stay deterministic. Natural-language requests must name a purpose;
  // a question, negation or vague acknowledgement never grants body access.
  if(/[?？“”「」]|不(?:要|需)|暂不|能否|是否|怎么|为什么|另一|其他|第二份|上次|之前|确认|批准|授权|外发|查看|看看|查询/u.test(value))return null;
  const code=value.match(/^([123])(?:\s|，|,|$)/u)?.[1];
  let purpose=code?Number(code):null;
  if(!purpose){
    if(/(?:仅|只).*(?:转换|转格式)/u.test(value))purpose=1;
    else if(/入库|放入知识库/u.test(value))purpose=/翻译/u.test(value)?3:2;
  }
  if(!purpose)return null;
  const language=/英文|英语/iu.test(value)?'英文':/中文|汉语/u.test(value)?'中文':/日文|日语/u.test(value)?'日文':/韩文|韩语/u.test(value)?'韩文':'自动';
  const document_profile=/古籍|竖排|文言/u.test(value)?'classical':/扫描|OCR/iu.test(value)?'scanned':'general';
  return {purpose,language,document_profile};
}
export const intakePrompt = filename => [
  `已收到《${filename}》。请一次选好用途和识别说明：`,
  '1 仅转换格式；2 整理知识笔记并入库；3 先入库，再全文翻译并挂接。',
  '回复示例：“2 无”“3 英文＋PDF”“2 中文＋扫描件”。“无”表示不补充识别说明；请同时写用途数字。',
  '可处理：PDF（电子版/扫描版）、图片（PNG/JPEG/WebP/TIFF/HEIC）、图片 ZIP、Word（DOC/DOCX）、PPTX、TXT/Markdown、XLSX/CSV，以及网页归档（webarchive）。图片 ZIP 按同一文件名规则中的编号排序，最多 200 张，合为一篇文章；不支持混装文件或加密 ZIP。旧 PPT/XLS 需人工处理；GIF 请先转为 PNG。古籍、竖排、繁体请注明。代码不走知识库。',
  '“无”时转换识别沿用中文、一般文本默认参数；原文是否中文会在本机核对，不把默认 OCR 参数当作原文语言证明。',
  '选择 2：允许本份原文由 OpenAI 云端整理笔记；选择 3：另允许 DeepSeek 翻译、OpenAI 复核，云端会收到本份原文、笔记及译文，不脱敏，可能保留处理记录。不会发送其他文件。',
  '选择后不再重复询问同一正文外发许可；笔记预览满意后，仍需你确认正式入库。可以随时停止后续处理。',
].join('\n');

export class IntakeFlow {
  constructor({client,ownerId,save,send,download,convert,deliver,curation,translation,progress,stopConversion=async()=>{},classifyArtifacts=null,
    ingestionMode='v1',custodyIntake=null}) {
    if(!['v1','v2.1-dev'].includes(ingestionMode))throw Error('unsupported ingestion mode');
    if(ingestionMode==='v2.1-dev'&&custodyIntake?.mode!=='v2.1-dev')throw Error('V2.1 requires the isolated custody adapter');
    if(ingestionMode==='v1'&&custodyIntake)throw Error('custody adapter requires explicit V2.1 mode');
    Object.assign(this,{ingestionMode,custodyIntake});
    Object.assign(this,{client,ownerId,save,send,download,convert,deliver,curation,translation,progress,stopConversion,classifyArtifacts});this.jobs=new Set();
  }
  tell(m,text){return this.send(m.chatId,{text},{replyTo:m.messageId});}
  async handle(m,state) {
    if(this.ingestionMode==='v2.1-dev'&&this.custodyIntake.pilot){
      // Python checks the local fixed app/group/person before downloading.
      // This branch remains mock-only; it never reaches V1 consent or sends.
      if(m.resources?.length){
        const attachments=await this.custodyIntake.receive(m);
        state.workflowV21={schema_version:'workflow-intake/v2.1',attachments,phase:'pilot_staged'};
        await this.save();
        await this.custodyIntake.processNext();
      }
      return true;
    }
    if(m.senderId!==this.ownerId||m.chatType!=='p2p')return true;
    if(this.ingestionMode==='v2.1-dev'){
      if(m.resources?.length){
        const attachments=await this.custodyIntake.receive(m);
        state.workflowV21={schema_version:'workflow-intake/v2.1',attachments,phase:'staged_waiting_s7'};
        await this.save();return true;
      }
      // S1–S5 never fall through into V1 source_relpath/conversion/consent handlers.
      return true;
    }
    const text=(m.content??'').trim();let w=state.workflowV11;
    if(m.resources?.length){
      const uploadId=m.sourceUploadId??m.messageId;
      if(w?.uploadId===uploadId){await this.tell(m,`《${w.filename}》已经接收，未重复创建任务。\n${w.phase==='choosing'?intakePrompt(w.filename):progressText(w.progress)}`);return true;}
      if(!w&&((state.activeCurationRun&&!['published','cancelled','superseded'].includes(state.activeCurationRun.phase))||this.translation.running)){
        await this.tell(m,'上一份资料仍在处理中，请先完成或停止，再上传下一份。');return true;
      }
      // Never cancel or relabel the active document when another upload arrives.
      if(w&&!['complete','cancelled'].includes(w.phase)){
        await this.tell(m,`《${w.filename}》还没有完成。本次新文件没有开始处理，也没有加入自动队列。请先完成或明确“取消本份流程”；之后可引用新文件回复“开始处理”，无需重新上传。`);return true;
      }
      if(m.resources.length!==1){await this.tell(m,'请一次发送一份资料，避免用途和确认串到其他文件。');return true;}
      if(/\.(?:py|js|mjs|cjs|ts|tsx|jsx|java|c|cpp|h|rs|go|sh|swift)$/iu.test(m.resources[0].fileName??'')){
        await this.tell(m,'这是一份代码文件，按你的设置不进入知识库；请走 GitHub。');return true;
      }
      await this.tell(m,'正在本机按文件名查重，核对是否已经入库；不会读取正文或调用云端模型。');
      const [attachment]=await this.download(m);const {encodedData,...saved}=attachment;
      const filename=attachment.originalFileName??attachment.fileName;
      const lookup=await this.client('workflow-lookup',{filename});
      const registered=await this.client('workflow-register',{actor_id:m.senderId,chat_id:m.chatId,upload_message_id:uploadId,filename,source_relpath:attachment.sourceRelpath});
      // Do not resurrect a replayed upload after the first selection.
      if(registered.state!=='received'){await this.tell(m,'这条文件消息已经处理过，请查看当前进度；没有重复执行。');return true;}
      if(state.pendingContentConsent?.consent?.consent_id)await this.client('invalidate',{consent_id:state.pendingContentConsent.consent.consent_id});
      state.pendingContentConsent=null;state.pendingTranslationChoice=null;
      w=state.workflowV11={id:registered.workflow_id,filename,attachment:saved,chatId:m.chatId,uploadId,
        phase:'choosing',matches:lookup.matches,progress:{filename,stage:'1',remaining:[],status:'running'}};
      await this.progress.set(state,{});
      await this.progress.stage(state,'2',{status:'waiting',next:'选择用途及识别说明'});
      state.pendingAttachments=[];state.pendingWorkflowAction=null;
      state.pendingPublication=null;state.pendingFallback=null;state.pendingDuplicateConfirmation=null;
      state.pendingTranslation=null;state.activeCurationRun=null;state.pendingCuration=null;state.lastCandidateTask=null;state.lastPublication=null;
      if(lookup.matches.length){
        const lines=lookup.matches.map((x,i)=>`${i+1}. ${x.publication_id?'知识笔记已入库':x.run_id?'已转换，保存有笔记处理记录，尚未入库':'已转换，尚未入库'}：${x.filename}`);
        if(lookup.matches.length===1){
          w.reuse=lookup.matches[0];
          await this.tell(m,`找到同名记录：${lines[0]}。\n${w.reuse.publication_id?'这份知识笔记已完成入库。可以回复“对这份资料提问”（问答功能尚待接入），或选择用途 3 补充译稿。':'可以接着完成未完成的阶段，不重复转换。'}\n如果同名但内容不同，请说“作为新文件处理”。\n${intakePrompt(filename)}`);
        }else{
          w.phase='existing';
          await this.tell(m,`找到多个同名处理记录（同名不保证内容一致）：\n${lines.join('\n')}\n回复“复用 1”选择对应记录；若是不同内容请说“作为新文件处理”。`);
        }
      }else await this.tell(m,(lookup.received_before?'以前收到过同名文件，但未找到可复用的转换成果。\n':'')+intakePrompt(filename));
      await this.save();return true;
    }
    if(!w)return false;
    if(['状态','进度','当前进度','到哪一步了','处理进度'].includes(text)){await this.tell(m,progressText(w.progress));return true;}
    if(['取消本份流程','取消','停止','取消转换','停止正文处理','暂停整理','先停一下','停止翻译','暂停翻译','取消翻译'].includes(text)){
      const converting=w.progress.stage==='3'&&w.phase==='running';w.phase='cancelled';await this.save();
      await this.client('workflow-cancel',{workflow_id:w.id});await this.curation.cancel(state);await this.translation.cancel(state);
      if(converting)await this.stopConversion();
      await this.progress.set(state,{status:'stopped',detail:'已按你的要求停止；已有文件保留。'});await this.tell(m,'已取消本份后续处理，已有成果保留；已外发的内容不能收回。');return true;
    }
    if(w.phase==='existing'){
      const match=text.match(/^复用\s*(\d+)$/u);
      if(match&&w.matches[Number(match[1])-1]){
        w.reuse=w.matches[Number(match[1])-1];w.phase='choosing';await this.save();
        await this.tell(m,(w.reuse.publication_id?'这份知识笔记已入库。回复“对这份资料提问”进入问答入口；如需要译稿，可以选择用途 3。\n':'将复用转换成果，继续尚未完成的阶段。\n')+intakePrompt(w.filename));return true;
      }
      if(text==='作为新文件处理'){w.phase='choosing';w.reuse=null;await this.save();await this.tell(m,intakePrompt(w.filename));return true;}
      await this.tell(m,'请用“复用 1”等选择一条记录，或说“作为新文件处理”。');return true;
    }
    if(text==='对这份资料提问'&&w.reuse?.publication_id){await this.tell(m,'这份资料已找到。当前 1.1 负责收集与处理，知识库检索问答尚未接入；不会伪装已经检索到答案，也不会重新整理。');return true;}
    if(w.phase==='choosing'){
      if(text==='作为新文件处理'){w.reuse=null;await this.save();await this.tell(m,intakePrompt(w.filename));return true;}
      const choice=parseIntakeChoice(text);
      if(!choice){await this.tell(m,'请同时写用途数字，例如“2 无”或“3 英文＋PDF”；这次没有开始处理。');return true;}
      if(choice.purpose===3&&choice.language==='中文'){
        await this.tell(m,'用途 3 是非中文全文译为中文。你标注的是中文，请改选“2 中文”，或更正原文语言；不会擅自改用途。');return true;
      }
      const selected=await this.client('workflow-select',{workflow_id:w.id,actor_id:m.senderId,chat_id:m.chatId,message_id:m.messageId,
        issued_at:Math.floor(m.createTime/1000),...choice,reuse_task_id:w.reuse?.source_task_id??null});
      if(selected.workflow_id!==w.id)throw Error('WORKFLOW_SOURCE_MISMATCH');
      w.choice=choice;w.phase='running';w.progress.purpose=choice.purpose;w.progress.remaining=initialStages(choice.purpose).filter(x=>!['1','2'].includes(x));await this.save();
      this.launch(m,state);return true;
    }
    if(w.phase==='fallback'&&text==='使用 Codex 转换'){w.executor='codex';w.phase='running';await this.save();this.launch(m,state);return true;}
    if(w.phase==='converted_ready'){
      if(['继续整理这份资料','继续处理当前资料','使用已转换稿继续整理'].includes(text)&&w.sourceTaskId&&!state.activeCurationRun){
        w.phase='running';await this.save();this.launch(m,state);return true;
      }
      await this.tell(m,'这份已转换正文已经接好，无需重新上传或识别。回复“继续整理这份资料”，将按原来的用途编写中文知识笔记；正式入库仍等你确认。');return true;
    }
    if(['fallback','stopped'].includes(w.phase)&&!w.sourceTaskId&&/\.zip$/iu.test(w.filename)&&text==='重新处理图片压缩包'){
      if(w.executor){await this.tell(m,'本份曾使用转换备份端，请先核对该任务是否完成；不会切换执行端重复提交。');return true;}
      w.executor=null;w.phase='running';await this.save();this.launch(m,state);return true;
    }
    if(w.phase==='duplicate'&&['复用已有成果','作为新版本转换'].includes(text)){
      w.duplicateAction=text==='复用已有成果'?'reuse_existing':'convert_as_new_version';w.phase='running';await this.save();this.launch(m,state);return true;
    }
    if(['artifact_question','text_required'].includes(w.phase)&&['重新检查转换结果','继续处理当前资料'].includes(text)){
      // Resume only the pre-body classification stop, never an uncertain cloud
      // call. Conversion is reused and the backend rechecks the original scope.
      if(!w.sourceTaskId||state.activeCurationRun){await this.tell(m,'已有正文处理记录，请先核对进度；没有重新执行。');return true;}
      w.phase='running';await this.save();this.launch(m,state);return true;
    }
    if(w.phase==='text_required'){
      await this.tell(m,'文件用途已经识别，不需要你解释技术文件。目前缺少可核对的完整文字正文，需要先补做本机文字识别或正文转换；没有开始编写笔记。已有成果保留。');return true;
    }
    if(w.phase==='artifact_question'&&text){
      if(/[?？]|什么意思|看看|查看|为什么|怎么/u.test(text)||!/(?:正文|过程|说明|辅助|附件|不入库)/u.test(text)){
        await this.tell(m,'目前是在判断转换包里哪些文件是资料正文、哪些只是处理说明。你可以说“README 是过程说明，不作为正文入库”；查询和评价不会触发重新处理。');return true;
      }
      w.artifactFeedback=text.slice(0,4000);w.phase='running';await this.save();this.launch(m,state);return true;
    }
    if(w.phase==='running'&&w.progress.stage==='3'){await this.tell(m,'正在转换本份文件。可以查询“进度”或说“取消本份流程”；不会另开任务。');return true;}
    return false;
  }
  launch(m,state) {
    const w=state.workflowV11;
    const job=(async()=>{
      try{
        if(w.convertedHandoff&&w.sourceTaskId)await this.progress.stage(state,'4.1',{done:['1','2','3'],detail:'复用你指定的已转换正文，正在核对资料关联；不重新识别。'});
        else {
          await this.progress.stage(state,'3',{worker:w.executor==='codex'?'Codex 转换备份＋本机转换服务':null});
          if(/\.zip$/iu.test(w.filename))await this.progress.set(state,{detail:'正在本机检查图片 ZIP，按文件名编号排序并识别；整个压缩包只作为一篇文章处理。'});
        }
        const conversion=w.sourceTaskId?{taskId:w.sourceTaskId,deliveryFiles:w.deliveryFiles??[]}:
          w.reuse&&w.choice.purpose!==1?{taskId:w.reuse.source_task_id,deliveryFiles:[]}:
          await this.convert(m,state,w);
        if(state.workflowV11!==w||w.phase==='cancelled')return;
        w.sourceTaskId=conversion.taskId;w.deliveryFiles=conversion.deliveryFiles;await this.save();
        const bound=await this.client('workflow-bind',{workflow_id:w.id,source_task_id:w.sourceTaskId});
        if(state.workflowV11!==w||w.phase==='cancelled')return;
        if(bound.workflow_id!==w.id||bound.source_task_id!==w.sourceTaskId)throw Error('WORKFLOW_SOURCE_MISMATCH');
        w.planId=bound.plan_id;state.lastCandidateTask={taskId:w.sourceTaskId};
        state.pendingCuration=bound.plan_id?{planId:bound.plan_id,sourceTaskId:w.sourceTaskId}:null;
        await this.save();
        if(w.choice.purpose===1){
          if(!conversion.deliveryFiles?.length)throw Error('CONVERSION_DELIVERY_NOT_AVAILABLE');
          await this.deliver(m,conversion.deliveryFiles);
          w.phase='complete';await this.progress.set(state,{status:'complete',remaining:[],detail:'转换成果已发送。'});return;
        }
        if(bound.publication_id){
          state.lastPublication={publication_id:bound.publication_id,plan_id:bound.plan_id};
          state.activeCurationRun={runId:bound.run_id,planId:bound.plan_id,sourceTaskId:w.sourceTaskId,phase:'published'};
          await this.published(m,state,state.lastPublication);return;
        }
        if(bound.run_state==='review_ready'){
          state.activeCurationRun={runId:bound.run_id,planId:bound.plan_id,sourceTaskId:w.sourceTaskId,consentId:bound.existing_consent_id,phase:'review_ready'};
          await this.progress.stage(state,'4.2',{done:['3','4.1'],status:'waiting',detail:'沿用已完成的知识笔记，不重新编写。'});
          await this.curation.present(m,state);return;
        }
        await this.progress.stage(state,'4.1',{done:['3'],detail:'转换完成，准备按原文编写中文知识笔记。'});
        let metadata=await this.client('workflow-metadata',{workflow_id:w.id});
        if(state.workflowV11!==w||w.phase==='cancelled')return;
        if(await this.holdForText(state,metadata)){
          await this.tell(m,'转换辅助文件已自动识别并保留，不需要你判断用途。但这份转换结果还没有可核对的完整文字正文，需要先补做本机文字识别或正文转换。没有开始编写知识笔记，也没有正式入库。');return;
        }
        if(this.classifyArtifacts){
          if(metadata.undecided.length){
            await this.classifyArtifacts(m,state,w,metadata);
            if(state.workflowV11!==w||w.phase==='cancelled')return;
            metadata=await this.client('workflow-metadata',{workflow_id:w.id});
            if(metadata.undecided.length){
              w.phase='artifact_question';await this.progress.set(state,{status:'waiting',detail:'有文件仅凭名称和位置无法判断是否正文。'});
              await this.tell(m,`这些文件的用途还不确定：${metadata.undecided.map(x=>x.name).join('、')}。请告诉我哪些是正文、哪些是转换过程说明；不会把它们混入知识笔记。`);return;
            }
          }
        }
        const grant=await this.client('workflow-grant',{workflow_id:w.id,operation:'curation'});
        if(state.workflowV11!==w||w.phase==='cancelled')return;
        await this.curation.approved({message:m,state,pending:{planId:w.planId,sourceTaskId:w.sourceTaskId,consent:grant.consent,runId:grant.run_id}});
      }catch(error){
        if(w.phase==='cancelled')return;
        if(error.message==='CONVERSION_DUPLICATE_REQUIRES_CONFIRMATION'){
          w.phase='duplicate';await this.progress.set(state,{status:'waiting',detail:'转换服务发现同内容的已有成果，请选择复用还是新版本转换。'});
          await this.tell(m,'转换服务发现同内容的已有资料。可回复“复用已有成果”或“作为新版本转换”；没有重新转换。');return;
        }
        w.phase=w.progress.stage==='3'&&!w.sourceTaskId&&!w.executor?'fallback':'stopped';
        await this.progress.set(state,{status:'stopped',detail:failureKind(error)==='timeout'?'当前阶段调用超时，未自动重试。':'当前阶段未完成，已有成果保留。'});
        await this.tell(m,(failureKind(error)==='timeout'?'当前阶段调用超时。':'本份流程未能继续。')+(w.phase==='fallback'?'如果 DSH 不可用，可回复“使用 Codex 转换”。':'请查询进度后核对；不会自动重发正文。'));
      }
    })();this.jobs.add(job);void job.finally(()=>this.jobs.delete(job));
  }
  async holdForText(state,metadata){
    if(!(metadata.conversion_text_checks??[]).some(x=>x.state!=='ready'))return false;
    const w=state.workflowV11;w.phase='text_required';
    await this.progress.stage(state,'3',{status:'stopped',remaining:initialStages(w.choice.purpose).filter(x=>!['1','2'].includes(x)),
      detail:'辅助文件已识别；转换正文不完整，需要补做本机文字识别或正文转换。尚未编写笔记。',next:'补齐文字正文后重新检查转换结果'});
    return true;
  }
  async published(m,state,result) {
    const w=state.workflowV11;if(!w||w.phase==='cancelled'||w.planId!==result.plan_id)return;
    w.publicationId=result.publication_id;await this.save();
    if(w.choice.purpose===2){w.phase='complete';await this.progress.set(state,{stage:'4.2',status:'complete',remaining:[],detail:'知识笔记已正式入库。'});return;}
    // Send a separate transition message, not just a quiet card update. Persist
    // the attempt first: an uncertain delivery must never spam on replay.
    if(!w.translationHandoffNoticeAttempted){
      w.translationHandoffNoticeAttempted=true;await this.save();
      try{
        const sent=await this.tell(m,'知识笔记已入库。接下来自动处理全文翻译和复核，完成后发送中英对照稿，并挂接回知识库。\n你现在不需要操作；如需你选择，或发生调用超时、处理失败，我会另行提醒。');
        w.translationHandoffNoticeUncertain=!sent?.messageId;
      }catch{w.translationHandoffNoticeUncertain=true;}
      await this.save();
    }
    await this.progress.stage(state,'5',{done:['3','4.1','4.2'],next:'翻译完成后对照原文复核'});
    try{await this.translation.beginWorkflow(m,state,w);}
    catch(error){w.phase='stopped';await this.progress.set(state,{status:'stopped',detail:'知识笔记已入库，后续翻译启动或交付未完成。'});
      await this.tell(m,failureKind(error)==='timeout'?'知识笔记已入库，后续步骤调用超时。已有成果保留。':'知识笔记已入库，但后续翻译步骤未完成。已有成果保留，不会重复入库或自动重发正文。');}
  }
  async offerTranslation(m,state){
    const old=state.workflowV11;if(!old?.publicationId||old.choice.purpose!==2)return;
    const value=await this.client('workflow-register',{actor_id:m.senderId,chat_id:m.chatId,upload_message_id:'continue:'+m.messageId,
      filename:old.filename,source_relpath:old.attachment.sourceRelpath});
    if(value.state!=='received')throw Error('WORKFLOW_REPLAY');
    state.workflowV11={...old,id:value.workflow_id,phase:'choosing',choice:null,
      reuse:{source_task_id:old.sourceTaskId,publication_id:old.publicationId},
      progress:{...old.progress,purpose:null,stage:'2',status:'waiting',remaining:[],detail:'已有知识笔记继续保留；只补充翻译用途。'}};
    await this.progress.set(state,{});
    await this.tell(m,'知识笔记已经入库，不会重新编写。原用途 2 没有授权 DeepSeek 翻译，因此补充一次用途选择：回复“3 无”或“3 英文＋PDF”。\n选择 3 允许本份原文、知识笔记及译文交给 DeepSeek 云端翻译和 OpenAI 云端复核，不脱敏，可能保留处理记录；译稿完成后挂接回本份知识库资料，不改写原笔记。');
  }
  async recover(chats){
    for(const state of Object.values(chats)){
      const w=state.workflowV11;if(!w||['complete','cancelled','choosing','existing'].includes(w.phase))continue;
      if(w.phase==='converted_ready'){
        await this.progress.set(state,{status:'waiting',detail:'已接入你指定的转换正文，无需重传或识别。',next:'回复“继续整理这份资料”开始编写笔记'});continue;
      }
      if(state.pendingTranslation?.workflowId===w.id&&state.pendingTranslation.phase==='needs_attention'){
        w.phase='stopped';
        await this.progress.set(state,{status:'stopped',detail:state.pendingTranslation.failureMessage??'翻译已停止，已有分段结果保留；不会自动继续，也没有从头重译。',next:state.pendingTranslation.translationMode==='codex_direct'?'核对保存结果':'回复“继续翻译”从断点续接'});continue;
      }
      const interruptedClassification=w.phase==='stopped'&&w.progress.stage==='4.1';
      if((['artifact_question','text_required'].includes(w.phase)||interruptedClassification)&&w.sourceTaskId&&!state.activeCurationRun){
        // A restart may refresh local readiness, but must not start cloud work.
        try{
          let metadata;
          for(let attempt=0;attempt<3;attempt++){
            try{metadata=await this.client('workflow-metadata',{workflow_id:w.id});break;}
            catch(error){if(attempt===2)throw error;await new Promise(resolve=>setTimeout(resolve,1000));}
          }
          if(metadata.classification_retry_allowed!==false&&(!interruptedClassification||metadata.classification_retry_allowed===true)){
            if(await this.holdForText(state,metadata))continue;
            w.phase='artifact_question';
            await this.progress.set(state,{status:'waiting',detail:'已有转换成果保留；回复“重新检查转换结果”继续核对，不需要重新上传。'});continue;
          }
        }catch{
          if(!interruptedClassification){
            await this.progress.set(state,{status:'stopped',detail:'本机校验服务暂未就绪；已有成果保留，回复“重新检查转换结果”重查。没有重新转换或调用模型。'});continue;
          }
        }
      }
      const waiting=state.pendingTranslation?.phase==='awaiting_owner'||state.activeCurationRun?.phase==='review_ready';
      w.phase=waiting?'running':'stopped';
      await this.progress.set(state,{status:waiting?'waiting':'stopped',detail:waiting?'已保存待确认结果；没有自动重新处理。':'机器人重启，未完成的调用已暂停；没有自动重发正文。'});
    }
  }
}
