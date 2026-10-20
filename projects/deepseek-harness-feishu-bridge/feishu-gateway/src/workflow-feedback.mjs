import {humanTime} from './user-facing-text.mjs';

// Diagnose infrastructure separately from model abstention. Never echo raw
// exceptions: they may contain request text, credentials or provider payloads.
export function failureKind(error) {
  for(let e=error,depth=0;e&&depth<5;e=e.cause,depth++) {
    if(['ETIMEDOUT','UND_ERR_CONNECT_TIMEOUT','UND_ERR_HEADERS_TIMEOUT','CODEX_TIMEOUT'].includes(e.code)
      || e.name==='TimeoutError' || /超时|\btimeout\b|timed out/iu.test(e.message??''))return 'timeout';
  }
  return 'failed';
}
export function intentFailureText(error) {
  return failureKind(error)==='timeout'
    ? '意图识别调用超时，暂时没有拿到结果，不是你的表达有问题。本条请求没有进入正文处理、外发或入库；我不会自动重试。你可以稍后重试，或先问我“下一步怎么操作”。'
    : '意图识别服务调用失败，暂时无法处理这条请求，不是判断你表达不清楚。本条请求没有进入正文处理、外发或入库；请稍后重试。';
}
export function workflowFailureText(error,{writeAttempt=false,readOnly=false}={}) {
  const reason=failureKind(error)==='timeout'?'调用超时':'服务调用或结果核验失败';
  if(writeAttempt)return `本次入库或撤回${reason}，执行结果尚未确认。请勿重复批准，先查询“状态”核对是否已经完成；我不会自动重试。`;
  if(readOnly)return `本次查看或查询${reason}，暂时没有拿到完整结果。这次查看没有执行正文整理或正式入库；请稍后再查询状态或查看预览。`;
  return `本次知识库操作${reason}，已停止后续步骤，未自动重试。已有结果保留，请先查询状态，再决定是否继续。`;
}
const normalize=text=>String(text??'').normalize('NFKC').trim().replace(/[。！？?!]+$/u,'');
export function isWorkflowHelp(text) {
  text=normalize(text);
  return /^(?:(?:请|能否|可以)?(?:告诉我|解释一下)?(?:这个|这条|这份|本份)?(?:翻译|正文|入库)?(?:授权|确认|操作)?(?:提示|回复|消息)?(?:是)?什么意思|(?:我)?(?:要|应该|该)?引用(?:回复)?(?:的是)?哪(?:一)?(?:个|条)(?:消息|提示)?|本份翻译提示是什么[？?]\s*我要引用的是哪一个|(?:我)?(?:现在|接下来|下一步)?(?:该|应该|要)?怎么(?:操作|确认|回复|继续)|下一步(?:怎么操作|做什么)|帮助)$/u.test(text);
}
export function workflowHelp(state,{now=Math.floor(Date.now()/1000),text=''}={}) {
  if(state.workflowV11){
    const w=state.workflowV11;
    if(state.pendingTranslation?.phase==='awaiting_owner')return '完整对照稿已经发出，但还有未通过复核的部分。请引用对应选择提示：回复 1 采用当前 DSH 稿；回复 2 让 Astra 直接翻译。采用后会挂接到已经入库的知识笔记。';
    if(w.phase==='choosing')return '请一次回复用途数字和识别说明，例如“2 无”或“3 英文＋PDF”。用途 2/3 包含本份相应的云端正文处理许可，之后不重复问；笔记正式入库仍需你确认预览。';
    if(w.phase==='existing')return '有多个同名记录。请回复“复用 1”等选择对应记录，或说“作为新文件处理”；不会替你猜测是哪一个版本。';
    if(state.pendingPublication?.phase==='awaiting')return '请先看入库预览。可以直接告诉我修改意见；满意后引用本轮确认提示回复“确认本次入库”。这只批准当前版本笔记，全文译稿按已选用途在入库后处理。';
    if(w.phase==='complete')return w.choice.purpose===1?'转换成果已发送，本次没有入库。':'本份用途已经完成。可以查看笔记、查询进度；有完成译稿时说“重发译稿”，不会重新翻译。原文、笔记和采用译稿通过本份资料目录关联。';
    if(w.progress.status==='stopped')return '流程已暂停，已有成果保留，没有自动重发正文。可以先查询“进度”或查看已保存预览；如果明确需要从头重译已入库资料，请说“重新翻译当前材料”。不确定的旧调用需要先诊断。';
    return '正在按你选定的用途处理。进度卡显示当前阶段及剩余步骤；通常不用回复，遇到待补充事项或入库预览会单独提醒。可以查询“进度”，或说“取消本份流程”。';
  }
  const translation=state.pendingTranslation;
  const otherWaiting=state.pendingContentConsent?.phase==='awaiting'||state.pendingPublication?.phase==='awaiting';
  if(translation?.phase==='awaiting'&&otherWaiting&&!/翻译|译稿|正文|入库/u.test(text))return '当前既有翻译确认，也有正文处理或入库确认。它们不是同一项授权。你想了解翻译怎么确认，还是正文处理、正式入库怎么确认？这次询问不会批准任何操作。';
  if(translation && !/正文|入库/u.test(text) && (/翻译|译稿/u.test(text)||!otherWaiting)) {
    const p=translation;
    if(p.phase==='completed')return '原文与中文译文对照稿已经完成。你可以说“重发译稿”，直接使用已有 DSH 译文，不重新翻译或让 Codex 改写；译稿不会自动写入知识库。只有明确说“重新翻译当前材料”才会准备新的翻译确认。';
    if(p.phase==='running')return '译稿正在翻译和复核，你暂时不需要确认。可以问“翻译进度”，或者说“停止翻译”。';
    if(p.phase!=='awaiting'||now>=p.consent?.expires_at)return '旧的翻译确认提示已过期或停止，继续回复它不会开始翻译。你可以直接说“重新发送翻译确认”，我会为当前材料准备新提示；仍需你单独确认后才外发正文。';
    return `请找到以“请确认这份材料的翻译与复核”开头的那条消息，长按消息（电脑上右键），选择“回复”，再输入“同意本份翻译和复核”。\n确认截止：${humanTime(p.consent.expires_at)}。也可以说“重新发送翻译确认”，不用翻找旧消息。单纯询问不会授权或开始翻译。`;
  }
  if(state.pendingContentConsent?.phase==='awaiting') {
    if(now>=state.pendingContentConsent.consent?.expires_at)return '正文授权提示已过期。请说“重新发送正文授权”，我会列出新的处理范围，再请你确认；不会自动外发。';
    return '机器人在等你确认是否允许把这份资料的正文交给云端整理，还没有因此批准入库。请长按以“请单独确认这份材料的正文处理范围”开头的消息，选择“回复”，输入“允许本次正文外发”。也可以说“重新发送正文授权”。如果只想看已保存内容，可以说“先给我看看资料”。';
  }
  if(state.pendingPublication?.phase==='awaiting')return '请先打开已发送的“入库预览.pdf”。核对后，长按同一轮的入库确认消息，选择“回复”，输入“确认本次入库”。过期时请说“查看最终入库预览”，我会准备新的确认；查看不等于批准。';
  if(state.activeCurationRun?.phase==='published'||state.lastPublication)return '这份资料已经入库。你可以说“查看入库预览”“查询状态”，或者“请给这份材料补一个中文译本”。申请译本只会先准备授权提示，不会直接外发正文或改动已入库内容。';
  return '你可以先说“查看预览”或“查询状态”。需要整理时说“开始正文处理”，我会先列出处理范围，请你确认；整理完成后先发预览，再由你单独批准入库。询问怎么操作不会触发整理。';
}

// High-precision convenience routes prepare requests only, never approvals.
export function localWorkflowRequest(text) {
  text=normalize(text);
  if(/^(?:请|麻烦)?(?:重发|(?:再|重新)(?:发|发送))(?:一下|一份)?(?:本份|这份|新的)?翻译(?:确认|授权)(?:提示|消息)?$/u.test(text))return 'translation_copy';
  if(/^(?:请|麻烦)?(?:再|重新)(?:发|发送)(?:一下|一份)?(?:本份|这份|新的)?正文授权(?:提示|消息)?$/u.test(text))return 'start_body';
  if(/^(?:请)?(?:给|帮我给)(?:这份|当前)(?:材料|资料|文件)(?:补|提供|做)(?:一份|一个)中文(?:译本|译稿)$/u.test(text))return 'translation_copy';
  if(/^(?:请问|查看|查询)?(?:翻译进度|译稿状态|译稿完成了吗|翻译完成了吗)$/u.test(text))return 'translation_status';
  if(/^(?:我(?:希望|想))?(?:请)?(?:先)?(?:查看|看看|看)(?:一下)?(?:这份|当前)?(?:资料|材料|文件)$/u.test(text))return 'preview';
  if(/^(?:请)?先给我看看(?:这份)?(?:资料|材料|文件)$/u.test(text))return 'preview';
  return null;
}

export function pendingConfirmationNote(state) {
  if(state.pendingContentConsent?.phase==='awaiting')return '\n另外，你先前申请的正文处理仍在等待单独授权。这次只是查看，不会替你确认；如果想先阅读，暂时不用回复授权消息。';
  return '';
}
