// Choosing a deliverable is not permission to send body text to a new provider.
export const translationReplies = new Map([
  ['需要完整中文译稿', true], ['需要完整译稿', true], ['需要译稿', true], ['要全文翻译', true],
  ['不需要译稿，继续整理', false], ['不需要译稿', false], ['不要译稿', false], ['不用翻译', false],
]);
export const translationUnavailable = '已记下你希望获得完整中文译稿。目前 DeepSeek 子 Agent 的模型连接检查没有通过，翻译还不能开始；不会自动换成其他模型，也没有为翻译外发正文。已有原文、笔记和入库结果保持不变。你可以说“不需要译稿，继续整理”，先完成不含翻译的入库流程。';

export function renderTranslationChoice(offer) {
  const title = String(offer.source_filename ?? '').replace(/[\u0000-\u001f\u007f-\u009f\u202a-\u202e\u2066-\u2069]/gu, ' ').slice(0, 300);
  return [`资料：${title}`,
    offer.language === 'non_chinese' ? '这份材料主要不是中文。你需要一份完整中文译稿吗？'
      : offer.language === 'chinese' ? '这份材料主要是中文，但计划中包含翻译。你仍需要一份完整中文译稿吗？'
        : '这份材料可能包含外文，或者语言还不能确定。你需要一份完整中文译稿吗？',
    '默认交付原文与中文译文逐段对照 PDF，使用 DSH 完成的译稿，保留标题、段落、数字和术语；不是摘要，也不替换原文。查看、重发和对照排版不重新翻译，除非你明确提出重译。',
    '由 DSH 调用 DeepSeek V4.1 Flash 翻译，Codex 复核；有问题交回 DSH 修改后再复核，最多修改两轮。全部通过后输出译稿，仍有问题就暂停。',
    '你可以直接说：“需要完整中文译稿”或“不需要译稿，继续整理”，也可以用自己的话表达。选择需要不等于同意正文外发；执行前还会单独请你确认 DeepSeek 和 OpenAI 的处理范围。'].join('\n');
}
