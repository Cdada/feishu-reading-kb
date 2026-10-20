const instructionMessageTypes = new Set(['text', 'post']);

export function hasExplicitInstruction(message) {
  return instructionMessageTypes.has(message?.rawContentType)
    && Boolean((message?.content || '').trim());
}

export function attachmentAction({ routingMode, explicitInstruction }) {
  if (explicitInstruction) return 'follow-instruction';
  return routingMode === 'operational' ? 'stage-for-parameters' : 'stage-for-selection';
}

export function isConversionParameterReply(text) {
  const value = (text || '').trim();
  if (/^(?:好的?[，,\s]*)?(?:请)?(?:直接|默认|按默认(?:参数)?)?(?:开始)?(?:执行|处理|转换)[。！!\s]*$/u.test(value)) return true;
  if (/^(?:好的?[，,\s]*)?(?:请)?(?:用|交给)?\s*(?:DSH|DeepSeek(?:\s*Harness)?|深度求索)\s*(?:来)?(?:执行|处理|转换)?[。！!\s]*$/iu.test(value)) return true;
  return /(?:中文|汉语|简体|繁体|英文|英语|English|日文|日语|日本語|扫描件|扫描版|图片型|OCR|电子文本|可复制文字|数字文本|一般文本|普通文本)/iu.test(value);
}
