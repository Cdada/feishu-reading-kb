import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import { RouterState } from '../src/router-state.mjs';

test('save recovers after an actual filesystem failure', async () => {
  const directory = await mkdtemp(join(tmpdir(), 'feishu-router-recovery-'));
  try {
    const blocker = join(directory, 'blocker');
    await writeFile(blocker, 'synthetic');
    const state = new RouterState(join(blocker, 'state.json'));
    state.chat('chat').executor = 'codex';
    await assert.rejects(state.save());
    state.path = join(directory, 'state.json');
    await state.save();
    assert.equal(JSON.parse(await readFile(state.path, 'utf8')).chats.chat.executor, 'codex');
  } finally {
    await rm(directory, { recursive: true, force: true });
  }
});

test('each save captures state at call time', async () => {
  const directory = await mkdtemp(join(tmpdir(), 'feishu-router-snapshot-'));
  try {
    const state = new RouterState(join(directory, 'state.json'));
    state.chat('chat').executor = 'dsh';
    const pending = state.save();
    state.chat('chat').executor = 'codex';
    await pending;
    assert.equal(JSON.parse(await readFile(state.path, 'utf8')).chats.chat.executor, 'dsh');
  } finally {
    await rm(directory, { recursive: true, force: true });
  }
});

test('legacy chat state receives safe routing defaults without losing pending work', async () => {
  const directory = await mkdtemp(join(tmpdir(), 'feishu-router-state-'));
  const path = join(directory, 'state.json');
  const state = new RouterState(path);
  state.data.chats.chat = { executor: 'codex', pendingAttachments: [{ fileName: 'a.pdf' }] };

  const chat = state.chat('chat');
  assert.equal(chat.routingMode, 'testing');
  assert.equal(chat.executor, 'codex');
  assert.equal(chat.pendingAttachments.length, 1);
  assert.equal(chat.codexThreadWorkspace, null);
  assert.equal(chat.pendingDuplicateConfirmation, null);

  await state.save();
  assert.equal(JSON.parse(await readFile(path, 'utf8')).chats.chat.routingMode, 'testing');
  await rm(directory, { recursive: true, force: true });
});
