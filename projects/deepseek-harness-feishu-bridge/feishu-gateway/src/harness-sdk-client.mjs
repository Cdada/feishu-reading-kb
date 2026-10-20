import { spawn } from 'node:child_process';
import { StringDecoder } from 'node:string_decoder';
import { executorEnvironment } from './consent-client.mjs';
import { readFile } from 'node:fs/promises';
import {
  extractDeliveryFiles,
  extractDeliveryFilesFromContent,
  uniqueDeliveryFiles,
} from './delivery-files.mjs';
import { extractLocalConversionResults, uniqueLocalConversionResults } from './local-conversion-results.mjs';

const defaultRequestTimeoutMs = 60_000;
const maxProtocolLineBytes = 2 * 1024 * 1024;

const unquoteYamlScalar = (value) => {
  const trimmed = value.trim();
  if (trimmed.startsWith('"') && trimmed.endsWith('"')) {
    try {
      return JSON.parse(trimmed);
    } catch {
      return trimmed.slice(1, -1);
    }
  }
  if (trimmed.startsWith("'") && trimmed.endsWith("'")) {
    return trimmed.slice(1, -1).replaceAll("''", "'");
  }
  return trimmed;
};

export const readHarnessModelSelection = async (settingsFile) => {
  const settings = await readFile(settingsFile, 'utf8');
  const selection = {};
  let inDefaultModel = false;

  for (const line of settings.split(/\r?\n/u)) {
    if (/^agent-default-model:\s*$/u.test(line)) {
      inDefaultModel = true;
      continue;
    }
    if (/^[^\s#][^:]*:/u.test(line)) {
      inDefaultModel = false;
      continue;
    }
    if (!inDefaultModel) continue;
    const match = line.match(/^\s+(provider|model|reasoningEffort):\s*(.*?)\s*$/u);
    if (match?.[1] && match[2]) selection[match[1]] = unquoteYamlScalar(match[2]);
  }

  if (!selection.provider || !selection.model) {
    throw new Error('Harness 设置中缺少默认 provider 或 model');
  }
  return selection;
};

export class HarnessSdkClient {
  constructor(options) {
    this.options = options;
    this.child = null;
    this.startPromise = null;
    this.selection = null;
    this.stdoutBuffer = '';
    this.stdoutDecoder = new TextDecoder('utf-8', { fatal: true });
    this.stderrDecoder = new StringDecoder('utf8');
    this.stderrTail = '';
    this.nextRequestId = 1;
    this.pendingRequests = new Map();
    this.activeRuns = new Map();
    this.intentionalStop = false;
  }

  getStatus() {
    return {
      connected: this.child !== null && this.child.exitCode === null,
      selection: this.selection,
      activeSessionCount: this.activeRuns.size,
    };
  }

  async start() {
    if (this.child !== null && this.child.exitCode === null) return;
    if (this.startPromise !== null) return this.startPromise;
    this.startPromise = this.startProcess();
    try {
      await this.startPromise;
    } finally {
      this.startPromise = null;
    }
  }

  async startProcess() {
    this.selection = this.options.selection ?? await readHarnessModelSelection(this.options.settingsFile);
    if (this.options.reasoningEffort) this.selection = {...this.selection, reasoningEffort:this.options.reasoningEffort};
    this.intentionalStop = false;
    this.stdoutBuffer = '';
    this.stdoutDecoder = new TextDecoder('utf-8', { fatal: true });
    this.stderrDecoder = new StringDecoder('utf8');
    this.stderrTail = '';

    const runtimeArgs = [
      'node',
      '--expose-internals',
      this.options.harnessBinary,
      '--profile',
      'sdk',
      ...(this.options.attachmentPatch
        ? ['--patch', this.options.attachmentPatch]
        : []),
    ];
    const args = [
      'exec',
      '-i',
      '-w', this.options.workspace,
      this.options.container,
      ...(this.options.containerTokenFile
        ? [
          '/bin/sh',
          '-c',
          'LOCAL_CONVERSION_MCP_TOKEN="$(cat "$1")" || exit 70; export LOCAL_CONVERSION_MCP_TOKEN; shift; exec "$@"',
          'dsh-sdk-bootstrap',
          this.options.containerTokenFile,
          ...runtimeArgs,
        ]
        : runtimeArgs),
    ];

    const child = this.options.launch ? await this.options.launch() : spawn(this.options.dockerBinary, args, {
      shell: false,
      stdio: ['pipe', 'pipe', 'pipe'],
      env: executorEnvironment(),
    });
    this.child = child;

    child.stdout.on('data', (chunk) => this.acceptStdout(chunk));
    child.stderr.on('data', (chunk) => {
      this.stderrTail = `${this.stderrTail}${this.stderrDecoder.write(chunk)}`.slice(-2_000);
    });
    child.on('error', (error) => this.handleProcessExit(error));
    child.on('close', (code, signal) => {
      const suffix = this.stderrTail.trim().split('\n').at(-1);
      const detail = suffix ? `：${suffix.slice(0, 300)}` : '';
      this.handleProcessExit(new Error(`Harness SDK 进程已退出（${code ?? signal ?? '未知'}）${detail}`));
    });

    try {
      const params = {
        cwd: this.options.workspace,
        provider: this.selection.provider,
        model: this.selection.model,
        ...(this.options.maxTokens ? {maxTokens: this.options.maxTokens} : {}),
        ...(this.selection.reasoningEffort
          ? { reasoningEffort: this.selection.reasoningEffort }
          : {}),
      };
      const result = await this.request('initialize', params, defaultRequestTimeoutMs);
      if (result?.serverInfo?.name !== 'deepseek-harness-sdk-runtime') {
        throw new Error('Harness SDK 初始化返回了未知服务');
      }
    } catch (error) {
      child.kill('SIGKILL');
      throw error;
    }
  }

  acceptStdout(chunk) {
    try {
      this.stdoutBuffer += this.stdoutDecoder.decode(chunk, { stream: true });
    } catch {
      this.child?.kill('SIGKILL');
      this.handleProcessExit(new Error('Harness SDK 返回了无效 UTF-8 数据，已停止以避免乱码'));
      return;
    }
    if (Buffer.byteLength(this.stdoutBuffer, 'utf8') > maxProtocolLineBytes) {
      this.child?.kill('SIGKILL');
      this.handleProcessExit(new Error('Harness SDK 协议行超过安全上限'));
      return;
    }

    for (;;) {
      const newline = this.stdoutBuffer.indexOf('\n');
      if (newline === -1) break;
      const line = this.stdoutBuffer.slice(0, newline).trim();
      this.stdoutBuffer = this.stdoutBuffer.slice(newline + 1);
      if (!line) continue;
      try {
        this.handleMessage(JSON.parse(line));
      } catch {
        this.child?.kill('SIGKILL');
        this.handleProcessExit(new Error('Harness SDK 返回了无效协议数据'));
        return;
      }
    }
  }

  handleMessage(message) {
    if (Object.hasOwn(message, 'id') && !Object.hasOwn(message, 'method')) {
      const pending = this.pendingRequests.get(message.id);
      if (!pending) return;
      this.pendingRequests.delete(message.id);
      clearTimeout(pending.timeout);
      if (Object.hasOwn(message, 'error')) {
        pending.reject(new Error(message.error?.message || 'Harness SDK 请求失败'));
      } else {
        pending.resolve(message.result);
      }
      return;
    }

    if (message.method === 'session.status') {
      const run = this.activeRuns.get(message.params?.sessionId);
      if (!run) return;
      if (message.params.status === 'running') run.sawRunning = true;
      if (!this.options.strictCompletion && message.params.status === 'idle' && run.sawRunning && !run.sawTurnEnd) {
        this.finishRun(message.params.sessionId, null, run.text);
      }
      return;
    }

    if (message.method !== 'session.event') return;
    const { sessionId, event } = message.params ?? {};
    const run = this.activeRuns.get(sessionId);
    if (!run || !event) return;
    run.onEvent?.(event);

    if (event.type === 'turn/start') {
      run.turnStarted = true;
      return;
    }

    if (event.type === 'assistant/message' && run.turnStarted) {
      const text = event.data?.message?.content
        ?.filter((block) => block?.type === 'text')
        .map((block) => block.text)
        .join('')
        .trim();
      if (text) run.text = text;
      return;
    }

    if (event.type === 'tool/call' && run.turnStarted) {
      const callId = event.data?.callId;
      if (typeof callId === 'string') run.toolNames.set(callId, event.data?.name ?? '');
      run.onProgress?.(`正在调用本机工具：${event.data?.name ?? '未知工具'}`);
      return;
    }

    if (event.type === 'tool/result' && run.turnStarted) {
      extractDeliveryFiles(event.data, run.deliveryFiles);
      extractDeliveryFilesFromContent(event.data?.message?.content, run.deliveryFiles);
      extractLocalConversionResults(event.data, run.conversionResults);
      return;
    }

    if (event.type === 'turn/end' && run.turnStarted) {
      run.sawTurnEnd = true;
      const reason = event.data?.reason;
      if (reason?.kind === 'completed') {
        this.finishRun(sessionId, null, run.text);
      } else {
        const diagnostic = reason?.error?.message || reason?.kind || '未知终止原因';
        this.finishRun(sessionId, new Error(`DeepSeek 任务未完成：${diagnostic}`));
      }
    }
  }

  request(method, params, timeoutMs) {
    const child = this.child;
    if (child === null || child.exitCode !== null || !child.stdin.writable) {
      return Promise.reject(new Error('Harness SDK 尚未连接'));
    }

    const id = this.nextRequestId++;
    return new Promise((resolve, reject) => {
      const timeout = setTimeout(() => {
        this.pendingRequests.delete(id);
        reject(new Error(`Harness SDK 请求超时：${method}`));
      }, timeoutMs);
      this.pendingRequests.set(id, { resolve, reject, timeout });
      child.stdin.write(`${JSON.stringify({ jsonrpc: '2.0', id, method, params })}\n`, (error) => {
        if (!error) return;
        const pending = this.pendingRequests.get(id);
        if (!pending) return;
        this.pendingRequests.delete(id);
        clearTimeout(pending.timeout);
        pending.reject(error);
      });
    });
  }

  async prompt(sessionId, content, timeoutMs, { onProgress, onEvent } = {}) {
    await this.start();
    if (this.activeRuns.has(sessionId)) {
      throw new Error('当前 DeepSeek 会话仍在处理上一条任务');
    }

    let resolveRun;
    let rejectRun;
    const completion = new Promise((resolve, reject) => {
      resolveRun = resolve;
      rejectRun = reject;
    });
    const timeout = setTimeout(() => {
      this.finishRun(sessionId, new Error(`DeepSeek 任务超时（${Math.round(timeoutMs / 60_000)} 分钟）`));
      this.child?.kill('SIGKILL');
    }, timeoutMs);
    this.activeRuns.set(sessionId, {
      resolve: resolveRun,
      reject: rejectRun,
      timeout,
      text: '',
      sawRunning: false,
      turnStarted: false,
      sawTurnEnd: false,
      toolNames: new Map(),
      deliveryFiles: [],
      conversionResults: [],
      onProgress,
      onEvent,
    });

    try {
      const contentBlocks = typeof content === 'string'
        ? [{ type: 'text', text: content }]
        : content;
      if (!Array.isArray(contentBlocks) || contentBlocks.length === 0) {
        throw new Error('DeepSeek 输入不包含有效内容');
      }
      await this.request(
        'session/prompt',
        { sessionId, contentBlocks },
        defaultRequestTimeoutMs,
      );
      return await completion;
    } catch (error) {
      this.finishRun(sessionId, error);
      throw error;
    }
  }

  finishRun(sessionId, error, text = '') {
    const run = this.activeRuns.get(sessionId);
    if (!run) return;
    this.activeRuns.delete(sessionId);
    clearTimeout(run.timeout);
    if (error) {
      run.reject(error);
      return;
    }
    if (!text.trim()) {
      run.reject(new Error('DeepSeek 任务已结束，但没有可返回的文本'));
      return;
    }
    run.resolve({
      text: text.trim(),
      deliveryFiles: uniqueDeliveryFiles(run.deliveryFiles),
      conversionResults: uniqueLocalConversionResults(run.conversionResults),
    });
  }

  handleProcessExit(error) {
    if (this.child === null) return;
    this.child = null;
    this.selection = null;
    for (const pending of this.pendingRequests.values()) {
      clearTimeout(pending.timeout);
      pending.reject(error);
    }
    this.pendingRequests.clear();
    for (const sessionId of [...this.activeRuns.keys()]) {
      this.finishRun(sessionId, error);
    }
    if (!this.intentionalStop) console.error(`Harness SDK 连接中断：${error.message}`);
  }

  async close() {
    const child = this.child;
    if (child === null) return;
    this.intentionalStop = true;
    const exited = new Promise((resolve) => {
      const timeout = setTimeout(() => {
        child.kill('SIGKILL');
      }, 5_000);
      child.once('close', () => {
        clearTimeout(timeout);
        resolve();
      });
    });
    try {
      await this.request('shutdown', undefined, 5_000);
    } catch {
      child.kill('SIGKILL');
    }
    await exited;
  }
}
