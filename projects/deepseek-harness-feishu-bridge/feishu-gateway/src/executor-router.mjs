const explicitCodex = /(?:^|[\s，。；：、]|请|让|用|交给|改用|切换到)(?:codex)(?:$|[\s，。；：、]|来|按|处理|执行)/iu;
const explicitDsh = /(?:^|[\s，。；：、]|请|让|用|交给|改用|切换到)(?:dsh|deepseek(?:\s+harness)?|深度求索)(?:$|[\s，。；：、]|来|按|处理|执行)/iu;

const normalizedCommand = (text) => text.trim().replace(/^\/(codex|deepseek|dsh)(?:\s+|$)/iu, '$1 ');

export function resolveExecutor(text, current = 'dsh') {
  const normalized = normalizedCommand(text || '');
  const codex = explicitCodex.test(normalized) || /^codex(?:\s|$)/iu.test(normalized);
  const dsh = explicitDsh.test(normalized) || /^(?:deepseek|dsh)(?:\s|$)/iu.test(normalized);
  if (codex && dsh) return { kind: 'ambiguous', executor: null };
  if (codex) return { kind: 'explicit', executor: 'codex' };
  if (dsh) return { kind: 'explicit', executor: 'dsh' };
  return { kind: 'default', executor: current === 'codex' ? 'codex' : 'dsh' };
}

export function isResetRequest(text) {
  return /^(?:\/(?:new|reset|deepseek-reset)|新建(?:一个)?会话|重新开始|重置(?:当前)?会话)[。！!\s]*$/iu.test((text || '').trim());
}

export function isStatusRequest(text) {
  return /^(?:\/status|状态|查看状态|现在什么状态)[？?。！!\s]*$/u.test((text || '').trim());
}

export function isFallbackAcceptance(text) {
  return /^(?:好的?[，,\s]*)?(?:改用|换成|切换到|交给|用)\s*codex\s*(?:来)?(?:处理|执行)?[。！!\s]*$/iu.test((text || '').trim());
}

export function isExecutorSelectionOnly(text) {
  return /^(?:请|好的?[，,\s]*)?(?:默认)?(?:改用|换成|切换到|交给|用)?\s*(?:codex|dsh|deepseek(?:\s+harness)?|深度求索)\s*(?:吧|处理|执行)?[。！!\s]*$/iu.test((text || '').trim());
}

export function resolveRoutingMode(text) {
  const value = (text || '').trim();
  if (/^(?:请|好的?[，,\s]*)?(?:进入|切换到|改成|设为)?\s*测试模式[。！!\s]*$/u.test(value)) return 'testing';
  if (/^(?:请|好的?[，,\s]*)?(?:进入|切换到|改成|设为)?\s*(?:正式运行|正式|运行)模式[。！!\s]*$/u.test(value)) return 'operational';
  return null;
}

export function resolveDuplicateAction(text) {
  const value = (text || '').trim();
  if (/^(?:好的?[，,\s]*)?(?:请)?(?:复用|使用)\s*(?:已有|原有|之前的?)?(?:成果|结果|候选资料包)?[。！!\s]*$/u.test(value)) return 'reuse_existing';
  if (/^(?:好的?[，,\s]*)?(?:请)?(?:作为|按)?\s*(?:新版本|新版)(?:重新)?转换[。！!\s]*$/u.test(value)
    || /^(?:好的?[，,\s]*)?(?:请)?重新转换(?:为)?新版本[。！!\s]*$/u.test(value)) return 'convert_as_new_version';
  if (/^(?:好的?[，,\s]*)?(?:请)?取消(?:本次|这个)?(?:任务|转换)?[。！!\s]*$/u.test(value)) return 'cancel';
  return null;
}
