import { spawn } from 'node:child_process';
import { StringDecoder } from 'node:string_decoder';
import { executorEnvironment } from './consent-client.mjs';
import {
  extractDeliveryFiles,
  extractDeliveryFilesFromText,
  uniqueDeliveryFiles,
} from './delivery-files.mjs';
import { extractLocalConversionResults, uniqueLocalConversionResults } from './local-conversion-results.mjs';
import { extractCurationResults, uniqueCurationResults } from './kb-curation-results.mjs';

const requestTimeoutMs = 60_000;
const maxProtocolBufferBytes = 8 * 1024 * 1024;
const allowedTools = {
  local_conversion: [
    'inspect_submission', 'submit_conversion', 'confirm_duplicate_submission',
    'get_task_status', 'wait_task_update', 'cancel_task', 'get_quality_report',
    'list_result_files', 'prepare_delivery',
  ],
  kb_curation: [
    'inspect_candidate', 'create_curation_plan', 'get_curation_plan',
    'get_interest_profile', 'set_archive_target', 'prepare_plan_preview',
    'prepare_archive_preview', 'check_archive_preview',
    'classify_candidate_artifacts',
    'get_curation_run', 'read_next_curation_chunk', 'save_curation_chunk',
    'submit_curation_draft', 'review_curation_draft',
  ],
};

export class CodexAppServerClient {
  constructor(options) {
    this.options = options;
    this.child = null;
    this.startPromise = null;
    this.stdoutBuffer = '';
    this.stdoutDecoder = new TextDecoder('utf-8', { fatal: true });
    this.stderrDecoder = new StringDecoder('utf8');
    this.stderrTail = '';
    this.nextRequestId = 1;
    this.pendingRequests = new Map();
    this.activeTurns = new Map();
    this.busyThreads = new Set();
    this.startingTurns = new Map();
    this.threadIds = new Set();
    this.restrictedConfig = null;
    this.intentionalStop = false;
    this.processGeneration = 0;
    this.pinnedProcess = null;
  }

  getStatus() {
    return {
      connected: this.child !== null && this.child.exitCode === null,
      activeTurnCount: this.busyThreads.size,
      threadCount: this.threadIds.size,
    };
  }

  async start() {
    this.assertPinnedProcess();
    if (this.startPromise) return this.startPromise;
    if (this.child !== null && this.child.exitCode === null) return;
    this.startPromise = this.startProcess();
    try {
      await this.startPromise;
    } finally {
      this.startPromise = null;
    }
  }

  async startProcess() {
    await this.options.beforeStart?.();
    // A verified single-run client cannot restart with inherited authorization.
    if (this.pinnedProcess) throw new Error('已核验的 Codex 进程不可替换，请停止本次生成');
    this.intentionalStop = false;
    this.stdoutBuffer = '';
    this.stdoutDecoder = new TextDecoder('utf-8', { fatal: true });
    this.stderrDecoder = new StringDecoder('utf8');
    this.stderrTail = '';
    const child = spawn(this.options.codexBinary ?? 'codex', [...(this.options.processArgs ?? []), 'app-server', '--stdio'], {
      cwd: this.options.cwd,
      shell: false,
      stdio: ['pipe', 'pipe', 'pipe'],
      env: this.options.processEnvironment ?? executorEnvironment(),
    });
    this.child = child;
    child.stdout.on('data', (chunk) => this.acceptStdout(chunk));
    child.stderr.on('data', (chunk) => {
      this.stderrTail = `${this.stderrTail}${this.stderrDecoder.write(chunk)}`.slice(-4_000);
    });
    child.on('error', (error) => {
      if (this.child === child) this.handleProcessExit(error);
    });
    child.on('close', (code, signal) => {
      if (this.child !== child) return;
      const last = this.stderrTail.trim().split('\n').at(-1);
      const detail = last ? `：${last.slice(0, 300)}` : '';
      this.handleProcessExit(new Error(`Codex app-server 已退出（${code ?? signal ?? '未知'}）${detail}`));
    });

    try {
      await this.request('initialize', {
        clientInfo: { name: 'feishu_kb_gateway', title: '飞书知识库网关', version: '0.2.0' },
        capabilities: { experimentalApi: true },
      });
      this.notify('initialized', {});
      const configResult = await this.request('config/read', { includeLayers: false, cwd: this.options.cwd });
      await this.options.checkConfig?.(configResult?.config ?? {});
      this.restrictedConfig = this.buildRestrictedConfig(configResult?.config ?? {});
    } catch (error) {
      child.kill('SIGKILL');
      throw error;
    }
  }

  buildRestrictedConfig(config) {
    const mcpServers = {};
    const allowedMcpServers = new Set(this.options.allowedMcpServers ?? ['local_conversion']);
    const autoApprovedMcpServers = new Set(this.options.autoApprovedMcpServers ?? ['local_conversion']);
    for (const name of Object.keys(config.mcp_servers ?? {})) {
      mcpServers[name] = { enabled: allowedMcpServers.has(name) };
    }
    for (const name of allowedMcpServers) {
      if (!allowedTools[name]) throw new Error(`未配置 MCP 工具白名单：${name}`);
      mcpServers[name] = {
        ...(mcpServers[name] ?? {}),
        enabled: true,
        enabled_tools: [...allowedTools[name]],
        ...(autoApprovedMcpServers.has(name) ? { default_tools_approval_mode: 'approve' } : {}),
      };
    }
    const plugins = {};
    for (const name of Object.keys(config.plugins ?? {})) plugins[name] = { enabled: false };
    return {
      ...(this.options.ephemeral ? { project_doc_max_bytes: 0 } : {}),
      ...(this.options.model ? { model: this.options.model } : {}),
      ...(this.options.effort ? { model_reasoning_effort: this.options.effort } : {}),
      include_apps_instructions: false,
      web_search: 'disabled',
      agents: { enabled: false },
      features: {
        shell_tool: false, unified_exec: false, apps: false, hooks: false,
        browser_use: false, computer_use: false, in_app_browser: false,
        multi_agent: false, multi_agent_v2: false, image_generation: false,
        view_image: false,
      },
      mcp_servers: mcpServers,
      plugins,
    };
  }

  acceptStdout(chunk) {
    try {
      // Pipe chunks are byte boundaries, not character boundaries.
      this.stdoutBuffer += this.stdoutDecoder.decode(chunk, { stream: true });
    } catch {
      this.child?.kill('SIGKILL');
      this.handleProcessExit(new Error('Codex app-server 返回了无效 UTF-8 数据，已停止以避免乱码'));
      return;
    }
    if (Buffer.byteLength(this.stdoutBuffer, 'utf8') > maxProtocolBufferBytes) {
      this.child?.kill('SIGKILL');
      this.handleProcessExit(new Error('Codex app-server 协议缓冲区超过安全上限'));
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
        this.handleProcessExit(new Error('Codex app-server 返回了无效协议数据'));
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
      if (message.error) pending.reject(new Error(message.error.message ?? 'Codex 请求失败'));
      else pending.resolve(message.result);
      return;
    }
    if (Object.hasOwn(message, 'id') && Object.hasOwn(message, 'method')) {
      this.handleServerRequest(message);
      return;
    }
    const params = message.params ?? {};
    if (message.method === 'model/rerouted' && this.options.model) {
      const error = new Error(`Codex 未按指定模型 ${this.options.model} 执行，已停止；不会自动换模型`);
      for (const run of this.activeTurns.values()) {
        clearTimeout(run.timeout);
        run.reject(error);
      }
      this.activeTurns.clear();
      this.processGeneration += 1;
      void this.close();
      return;
    }
    const turnId = params.turnId ?? params.turn?.id;
    const run = turnId ? this.activeTurns.get(turnId) : null;
    // Notifications can precede registration even after turn/start responds.
    const starting = this.startingTurns.get(params.threadId);
    if (!run && turnId && starting) {
      starting.bytes += Buffer.byteLength(JSON.stringify(message), 'utf8');
      if (starting.messages.length >= 1_024 || starting.bytes > maxProtocolBufferBytes) {
        this.child?.kill('SIGKILL');
        this.handleProcessExit(new Error('Codex 启动回合通知超过安全上限'));
        return;
      }
      starting.messages.push(message);
      return;
    }
    if (message.method === 'item/started' && run && params.item?.type === 'mcpToolCall') {
      run.onProgress?.(`正在调用${this.options.progressLabel ?? '本机工具'}：${params.item.tool}`);
      return;
    }
    if (message.method === 'item/completed' && run) {
      const item = params.item;
      if (item?.type === 'agentMessage' && item.text?.trim()) {
        if (item.phase === 'final_answer' || !run.finalText) run.finalText = item.text.trim();
      }
      if (item?.type === 'mcpToolCall') {
        run.toolCalls.push({ server: item.server, tool: item.tool, status: item.status });
        extractDeliveryFiles(item.result?.structuredContent, run.deliveryFiles);
        extractLocalConversionResults(item.result, run.conversionResults);
        extractCurationResults(item.result, run.curationResults);
        if (item.status === 'failed' || item.error || item.result?.isError) run.transportErrors = (run.transportErrors ?? 0) + 1;
        if (run.maxCurationErrors && !run.aborting && uniqueCurationResults(run.curationResults).filter((x) => x.ok === false).length + (run.transportErrors ?? 0) >= run.maxCurationErrors) {
          run.aborting = true;
          this.activeTurns.delete(turnId); clearTimeout(run.timeout);
          void this.close().finally(() => run.reject(new Error('本次知识库工具多次拒绝请求，已停止盲目重试；已保存结果保留，请核对失败原因后继续')));
          return;
        }
        for (const block of item.result?.content ?? []) {
          if (block?.type === 'text') extractDeliveryFilesFromText(block.text, run.deliveryFiles);
        }
      }
      return;
    }
    if (message.method === 'turn/completed' && run) {
      this.activeTurns.delete(turnId);
      clearTimeout(run.timeout);
      if (params.turn?.status === 'completed') {
        run.resolve({
          text: run.finalText || 'Codex 已完成任务。',
          deliveryFiles: uniqueDeliveryFiles(run.deliveryFiles),
          conversionResults: uniqueLocalConversionResults(run.conversionResults),
          curationResults: uniqueCurationResults(run.curationResults),
          toolCalls: run.toolCalls,
        });
      } else {
        run.reject(new Error(params.turn?.error?.message ?? `Codex 任务未完成：${params.turn?.status ?? '未知状态'}`));
      }
    }
  }

  handleServerRequest(message) {
    const approvalMethods = new Set([
      'item/commandExecution/requestApproval',
      'item/fileChange/requestApproval',
      'applyPatchApproval',
      'execCommandApproval',
    ]);
    if (approvalMethods.has(message.method)) {
      this.respond(message.id, { decision: 'decline' });
      return;
    }
    if (message.method === 'item/tool/requestUserInput') {
      this.respond(message.id, { answers: {} });
      return;
    }
    this.respondError(message.id, -32601, `飞书知识库 Profile 不支持服务请求：${message.method}`);
  }

  pinCurrentProcess() {
    this.assertPinnedProcess();
    if (!this.child || this.child.exitCode !== null || this.child.signalCode != null
        || this.child.killed || !this.child.stdin.writable) {
      throw new Error('Codex 进程尚未就绪，无法绑定本次核验');
    }
    this.pinnedProcess = { child: this.child, generation: this.processGeneration };
  }

  assertPinnedProcess() {
    const pinned = this.pinnedProcess;
    if (pinned && (this.child !== pinned.child || this.processGeneration !== pinned.generation
        || this.child.exitCode !== null || this.child.signalCode != null
        || this.child.killed || !this.child.stdin.writable)) {
      throw new Error('已核验的 Codex 进程发生变化，已停止本次生成');
    }
  }

  write(message) {
    // Recheck after asynchronous source/window guards, immediately before I/O.
    this.assertPinnedProcess();
    if (!this.child || this.child.exitCode !== null || !this.child.stdin.writable) {
      throw new Error('Codex app-server 尚未连接');
    }
    this.child.stdin.write(`${JSON.stringify(message)}\n`);
  }

  async request(method, params, timeoutMs = requestTimeoutMs) {
    await this.options.beforeRequest?.(method, params);
    const id = this.nextRequestId++;
    return new Promise((resolve, reject) => {
      const timeout = setTimeout(() => {
        this.pendingRequests.delete(id);
        reject(new Error(`Codex app-server 请求超时：${method}`));
      }, timeoutMs);
      this.pendingRequests.set(id, { resolve, reject, timeout });
      try {
        this.write({ jsonrpc: '2.0', id, method, params });
      } catch (error) {
        clearTimeout(timeout);
        this.pendingRequests.delete(id);
        reject(error);
      }
    });
  }

  notify(method, params) {
    this.write({ jsonrpc: '2.0', method, params });
  }

  respond(id, result) {
    this.write({ jsonrpc: '2.0', id, result });
  }

  respondError(id, code, message) {
    this.write({ jsonrpc: '2.0', id, error: { code, message } });
  }

  threadParams() {
    return {
      ...(this.options.model ? { model: this.options.model } : {}),
      ...(this.options.modelProvider ? { modelProvider: this.options.modelProvider } : {}),
      cwd: this.options.cwd,
      approvalPolicy: 'never',
      approvalsReviewer: 'user',
      sandbox: 'read-only',
      config: this.restrictedConfig,
      developerInstructions: this.options.developerInstructions,
      ...(this.options.baseInstructions ? { baseInstructions: this.options.baseInstructions } : {}),
    };
  }

  async verifyThreadProfile(result) {
    if ((this.options.model && result?.model !== this.options.model)
      || (this.options.effort && result?.reasoningEffort !== this.options.effort)
      || (this.options.modelProvider && result?.modelProvider !== this.options.modelProvider)) {
      await this.close();
      throw new Error('Codex 返回的模型或思考档位与指定设置不一致，已停止；不会自动换模型');
    }
  }

  async ensureThread(existingThreadId = null) {
    if (this.busyThreads.has(existingThreadId)) throw new Error('Codex 当前任务仍在执行，请等待完成后再试');
    await this.start();
    if (existingThreadId) {
      // Any resume failure needs attention, not a new sidebar task.
      const result = await this.request('thread/resume', {
        threadId: existingThreadId,
        excludeTurns: true,
        ...this.threadParams(),
      });
      const id = result?.thread?.id ?? existingThreadId;
      await this.verifyThreadProfile(result);
      if (id !== existingThreadId) throw new Error('Codex 恢复返回了不同的任务编号，已拒绝替换');
      this.threadIds.add(id);
      return id;
    }
    const result = await this.request('thread/start', {
      ...this.threadParams(),
      ephemeral: this.options.ephemeral === true,
      ...(this.options.ephemeral ? { dynamicTools: [], environments: [] } : {}),
      serviceName: this.options.serviceName ?? 'feishu-kb-gateway',
    });
    const threadId = result?.thread?.id;
    await this.verifyThreadProfile(result);
    if (!threadId) throw new Error('Codex 未返回任务编号');
    this.threadIds.add(threadId);
    return threadId;
  }

  async prompt(threadId, text, { timeoutMs = 10 * 60_000, requestId, onProgress, maxCurationErrors = null, outputSchema, effort } = {}) {
    if (this.options.effort && effort && effort !== this.options.effort) {
      throw new Error('本次思考档位与工作流固定设置不一致，已拒绝执行');
    }
    effort = this.options.effort ?? effort;
    if (this.busyThreads.has(threadId)) throw new Error('Codex 当前任务仍在执行，请等待完成后再试');
    this.busyThreads.add(threadId);
    try {
      await this.start();
      const generation = this.processGeneration;
      const early = { messages: [], bytes: 0 };
      this.startingTurns.set(threadId, early);
      let result;
      try {
        result = await this.request('turn/start', {
          threadId,
          ...(this.options.model ? { model: this.options.model } : {}),
          input: [{ type: 'text', text }],
          approvalPolicy: 'never',
          approvalsReviewer: 'user',
          sandboxPolicy: { type: 'readOnly', networkAccess: false },
          clientUserMessageId: requestId ?? null,
          ...(outputSchema ? { outputSchema } : {}),
          ...(effort ? { effort } : {}),
        });
        if (!result?.turn?.id) throw new Error('Codex 未返回回合编号');
        if (generation !== this.processGeneration) throw new Error('Codex 执行端在启动回合时退出，请先核对任务状态');
      } catch (error) {
        // Start outcome is uncertain: stop, never automatically retry a write.
        await this.close();
        throw error;
      } finally {
        this.startingTurns.delete(threadId);
      }
      const turnId = result.turn.id;
      return await new Promise((resolve, reject) => {
        const timeout = setTimeout(() => {
          this.activeTurns.delete(turnId);
          void (async () => {
            try {
              await this.request('turn/interrupt', { threadId, turnId }, 5_000);
            } catch { /* Stop the dedicated process even if interrupt fails. */ }
            await this.close();
            throw new Error('Codex 任务超时，已停止执行端；请先核对本机任务状态，勿直接重复提交');
          })().catch(reject);
        }, timeoutMs);
        this.activeTurns.set(turnId, {
          resolve, reject, timeout, finalText: '', deliveryFiles: [],
          toolCalls: [], conversionResults: [], curationResults: [], onProgress, maxCurationErrors,
        });
        for (const message of early.messages) this.handleMessage(message);
      });
    } finally {
      this.startingTurns.delete(threadId);
      this.busyThreads.delete(threadId);
    }
  }

  handleProcessExit(error) {
    if (!this.child) return;
    this.processGeneration += 1;
    this.child = null;
    for (const pending of this.pendingRequests.values()) {
      clearTimeout(pending.timeout);
      pending.reject(error);
    }
    this.pendingRequests.clear();
    for (const run of this.activeTurns.values()) {
      clearTimeout(run.timeout);
      run.reject(error);
    }
    this.activeTurns.clear();
    if (!this.intentionalStop) console.error(`Codex app-server 连接中断：${error.message}`);
  }

  async close() {
    const child = this.child;
    if (!child) return;
    this.intentionalStop = true;
    child.stdin.end();
    await new Promise((resolve) => {
      const timeout = setTimeout(() => child.kill('SIGKILL'), 5_000);
      child.once('close', () => {
        clearTimeout(timeout);
        resolve();
      });
    });
  }
}
