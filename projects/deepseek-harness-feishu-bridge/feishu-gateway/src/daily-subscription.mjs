// Read subscription status only: no thread, prompt, tool or model turn.
import { resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { CodexAppServerClient } from './codex-app-server-client.mjs';
import { boundFile, standingReserve, checkSubscription, clientOptions, readPacketInput } from './pilot-reading-guide.mjs';

export async function probe(packet, { client, check }) {
  let outcome;
  try {
    await check(); await client.start(); client.pinCurrentProcess();
    const account = await client.request('account/read', { refreshToken: false });
    const limits = await client.request('account/rateLimits/read', {});
    const reserve = await check();
    checkSubscription(account, limits, packet.accountSha, reserve);
    outcome = { state: 'ready', modelCalls: 0, reservePercent: reserve };
  } catch (error) {
    outcome = { state: 'waiting', modelCalls: 0,
      reason: error?.code === 'quota_threshold_reached' ? error.code : 'subscription_check_unavailable' };
  } finally {
    try { await client.close(); } catch {
      outcome = { state: 'waiting', reason: 'subscription_check_unavailable', modelCalls: 0 };
    }
  }
  return outcome;
}

async function main() {
  if (process.env.KB_V21_OFFLINE_TEST === '1') throw new Error('offline');
  const packet = await readPacketInput(process.stdin);
  if (packet.mode !== 'subscription_probe' || packet.files?.[packet.rulesPath] !== packet.rulesSha) throw new Error('binding');
  const check = async () => {
    for (const [path, hash] of Object.entries(packet.files)) await boundFile(path, hash, false);
    return standingReserve(packet.rulesPath, packet.rulesSha);
  };
  const client = new CodexAppServerClient(clientOptions({ cwd: packet.cwd, codexBinary: packet.codexBinary, check }));
  process.stdout.write(JSON.stringify(await probe(packet, { client, check })) + '\n');
}
if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main().catch(() => { process.stdout.write(JSON.stringify({ state: 'waiting', reason: 'subscription_check_unavailable', modelCalls: 0 })+'\n'); process.exitCode=1; });
}
