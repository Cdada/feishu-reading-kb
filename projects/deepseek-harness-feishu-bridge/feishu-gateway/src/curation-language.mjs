// Narrow local intent routing. Never grants authority; uncertain language asks
// a question instead of starting a cloud turn. Used only for authenticated text
// in the current material's workflow, not document contents or model output.
export function curationIntent(value) {
  const text = String(value ?? '').normalize('NFKC').trim();
  if (!text || text.length > 500 || /[\n{}]|文档(?:中|里)|原文(?:中|里)|提示词|忽略.*规则|[“「『]/u.test(text)) return 'unknown';
  if (/另一|别的|上一份|上份|其他(?:材料|资料|文件)/u.test(text)) return 'unknown';
  if (/(?:不要|不用|别|无需|不想|不需要).{0,10}(?:预览|看|发|整理|修改|继续)/u.test(text)) return 'unknown';
  if (/(?:修改|改正|修正|更正|补充|重写|重新整理|重新翻译|继续整理|继续处理|继续核对|改一下|改成|改为)/u.test(text)) return 'revise';
  if (/(?:状态|进度|处理到哪|整理到哪|完成了吗|完成了没|处理好了吗|整理好了吗|入库了吗)/u.test(text)) return 'status';
  const object = /预览|整理结果|整理成果|整理稿|整理好的(?:材料|资料|文件)|PDF/iu.test(text);
  const request = /看|查|给|发|提供|生成|重发|打开|打不开|下载|在哪|哪里|收到|找不到|获取/u.test(text);
  if ((object && request) || /^(?:请|麻烦)?(?:再发(?:一下|一次|一遍)|发给我看看)[。!?！?吧好吗\s]*$/u.test(text)) return 'preview';
  return 'unknown';
}
