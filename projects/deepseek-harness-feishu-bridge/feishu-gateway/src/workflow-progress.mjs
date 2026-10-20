// Persist facts, not estimates. The current unfinished stage counts as remaining.
export const stages = Object.freeze({
  '1':['初始文件名入库核查','本机台账（不调用模型）'],
  '2':['已有处理进度核查','本机台账（不调用模型）'],
  '3':['文件初步格式转换','DSH · 深度思考开启；本机转换服务'],
  '4.1':['知识笔记编写与复核','Codex · GPT-6 Astra / medium'],
  '4.2':['知识笔记入库','本机写入服务（等待你批准）'],
  '5':['DSH 首轮翻译','DSH · DeepSeek Flash / high'],
  '5.1':['Codex 翻译复核','Codex · GPT-5.6 Sol / high'],
  '5.2':['DSH 修正译稿','DSH · DeepSeek Flash / high'],
  '5.3':['Codex 再次复核','Codex · GPT-5.6 Sol / high'],
  '5.4':['提供原文＋中文对照稿','本机排版与飞书发送'],
  'direct':['Codex 直接翻译','Codex · GPT-6 Astra / medium'],
  '6':['译文挂接回知识库','本机写入服务'],
});
export const purposeNames = {1:'仅转换',2:'知识笔记入库',3:'先入库，再翻译并挂接'};
export function initialStages(purpose) {
  return purpose===1?['1','2','3']:purpose===2?['1','2','3','4.1','4.2']:
    purpose===3?['1','2','3','4.1','4.2','5','5.1','5.4','6']:[];
}
const clean=x=>String(x??'').replace(/[\u0000-\u001f\u007f-\u009f\u202a-\u202e\u2066-\u2069]/gu,' ').slice(0,500);
export function progressText(p) {
  const current=stages[p.stage]??['等待用途选择','本机网关'];
  return [`资料：${clean(p.filename)}`,`用途：${purposeNames[p.purpose]??'待选择'}`,
    `当前：${p.stage??''} ${current[0]}`,`执行：${p.worker??current[1]}`,
    `状态：${({running:'处理中',waiting:'等待你回复',stopped:'已暂停',complete:'已完成'})[p.status]??'尚未开始'}`,
    p.purpose?`距离完成：还有 ${p.remaining.length} 个步骤（含当前未完成步骤；需要修正时另计）`:'距离完成：选择用途后显示',
    p.detail?clean(p.detail):'',p.next?`下一步：${clean(p.next)}`:'',
    p.status==='waiting'?'需要你回复；已有成果保留。':p.status==='running'?'暂不需要回复。':''].filter(Boolean).join('\n');
}
export class WorkflowProgress {
  constructor({save,create,update}) {Object.assign(this,{save,create,update});this.queue=Promise.resolve();}
  async set(state, changes) {
    const workflow=state.workflowV11;
    const task=async()=>{
      if(!workflow||state.workflowV11!==workflow)return;
      const p=state.workflowV11.progress;
      Object.assign(p,changes);await this.save();
      const text=progressText(p);
      // Failed updates preserve the one-card id, never create duplicates.
      try {
        if(p.messageId)await this.update(p.messageId,text);
        else if(!p.deliveryAttempted){
          p.deliveryAttempted=true;await this.save();
          const id=await this.create(state.workflowV11.chatId,text);
          if(!id)throw Error('PROGRESS_DELIVERY_UNCERTAIN');
          p.messageId=id;await this.save();
        }
      }catch{p.deliveryUncertain=true;await this.save();}
    };
    this.queue=this.queue.then(task,task);return this.queue;
  }
  async stage(state,stage,{done=[],remaining,...changes}={}) {
    const p=state.workflowV11?.progress;if(!p)return;
    await this.set(state,{stage,status:'running',detail:'',next:'',worker:null,
      remaining:remaining??p.remaining.filter(x=>!done.includes(x)),...changes});
  }
}
