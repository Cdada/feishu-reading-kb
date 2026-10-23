"""Read-only subscription admission before durable business-call reservation."""
import json
import os
from pathlib import Path
import subprocess

from .contracts import ContractError
from .daily_store import Deferred
from .pilot_text import MAX_BYTES, TEXT_ENVIRONMENT
from .registry import encoded


def require_subscription(authority, store):
    if not store.policy.standing_daily:
        return
    now = store.now()
    key = 'subscription-admission'
    prior = store.get(key) or {}
    if prior.get('rules_sha256') == authority.digest and prior.get('retry_at', 0) > now:
        raise Deferred(prior['reason'], prior['retry_at'])
    config = authority.config
    guide = json.loads(Path(config['guide_plan']).read_bytes())
    frozen = authority.check()
    packet = dict(mode='subscription_probe', cwd=str(authority.runtime.worktree),
        codexBinary=config['codex_binary'], rulesPath=str(authority.path), rulesSha=authority.digest,
        accountSha=guide['account_sha256'], files=frozen)
    script = Path(config['text_script']).with_name('daily-subscription.mjs')
    if str(script) not in frozen:
        raise ContractError('subscription admission script must be frozen')
    if os.environ.get('KB_V21_OFFLINE_TEST') == '1':
        raise ContractError('real subscription admission disabled in offline tests')
    try:
        result = subprocess.run([config['text_node'], str(script)], input=encoded(packet).encode(),
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=60,
            env={k: os.environ[k] for k in TEXT_ENVIRONMENT if k in os.environ})
        if result.returncode or len(result.stdout) > MAX_BYTES:
            raise OSError('subscription probe failed')
        response = json.loads(result.stdout)
        if response.get('state') not in ('ready', 'waiting'):
            raise ValueError('invalid subscription probe')
    except (OSError, subprocess.TimeoutExpired, ValueError, TypeError) as error:
        response = dict(state='waiting', reason='subscription_check_unavailable')
    authority.check()
    if response['state'] != 'ready':
        reason = ('subscription_quota_wait' if response.get('reason') == 'quota_threshold_reached'
            else 'subscription_check_unavailable')
        value = dict(reason=reason, retry_at=now+300, rules_sha256=authority.digest,
            reserve_percent=store.policy.remaining_reserve(now))
        store.put(key, value)
        raise Deferred(reason, value['retry_at'])
    store.put(key, None)
