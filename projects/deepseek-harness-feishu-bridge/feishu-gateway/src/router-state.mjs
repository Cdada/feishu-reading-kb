import { mkdir, readFile, rename, writeFile } from 'node:fs/promises';
import { dirname } from 'node:path';
import { randomUUID } from 'node:crypto';

const emptyState = () => ({ version: 1, chats: {} });

export class RouterState {
  constructor(path) {
    this.path = path;
    this.data = emptyState();
    this.writeChain = Promise.resolve();
  }

  async load() {
    try {
      const parsed = JSON.parse(await readFile(this.path, 'utf8'));
      if (parsed?.version === 1 && parsed.chats && typeof parsed.chats === 'object') {
        this.data = parsed;
      }
    } catch (error) {
      if (error?.code !== 'ENOENT') throw error;
    }
    return this;
  }

  chat(chatId) {
    const current = this.data.chats[chatId] ?? {};
    const defaults = {
      routingMode: 'testing',
      executor: 'dsh',
      pendingAttachments: [],
      codexThreadId: null,
      codexThreadWorkspace: null,
      codexCurationThreadId: null,
      codexCurationThreadWorkspace: null,
      lastCandidateTask: null,
      pendingCuration: null,
      pendingContentConsent: null,
      pendingTranslationChoice: null,
      pendingTranslation: null,
      pendingPublication: null,
      pendingWorkflowAction: null,
      activeCurationRun: null,
      lastPublication: null,
      pendingFallback: null,
      pendingDuplicateConfirmation: null,
    };
    for (const [key, value] of Object.entries(defaults)) if (!Object.hasOwn(current, key)) current[key] = value;
    this.data.chats[chatId] = current; // Stable identity for background curation callbacks.
    return this.data.chats[chatId];
  }

  async save() {
    const snapshot = `${JSON.stringify(this.data, null, 2)}\n`;
    // A failed write is reported to its caller but must not poison future saves.
    this.writeChain = this.writeChain.catch(() => {}).then(async () => {
      await mkdir(dirname(this.path), { recursive: true, mode: 0o700 });
      const temporary = `${this.path}.${process.pid}.${randomUUID()}.tmp`;
      await writeFile(temporary, snapshot, { mode: 0o600, flag: 'wx' });
      await rename(temporary, this.path);
    });
    return this.writeChain;
  }
}
