"""P32 operating limits. Constructing a policy never authorizes network access."""
from dataclasses import asdict, dataclass
import hashlib
import re
import math

from .contracts import ContractError
from .registry import encoded


@dataclass(frozen=True)
class DailyPolicy:
    app_id: str
    chat_id: str
    account_id: str
    bot_id: str
    # Legacy finite grants retain their cumulative ceiling. Standing daily use
    # requires an explicit mode plus null ceiling; history is never discarded.
    model_turn_limit: int | None
    poll_seconds: int = 20
    http_per_minute: int = 60
    http_per_day: int = 20000
    http_confirmation_reserve: int = 200
    page_size: int = 50
    pages_per_tick: int = 10
    history_seconds: int = 7 * 86400
    overlap_seconds: int = 2
    retry_attempts: int = 15
    max_pending: int = 500
    max_asset_bytes: int = 32 * 1024 * 1024
    paid_model_cny: int = 0
    model_budget_mode: str = 'cumulative'
    temporary_reserve_percent: int | None = None
    temporary_reserve_until: float | None = None

    @property
    def standing_daily(self):
        return self.model_budget_mode == 'standing_daily'

    def model_limit_reached(self, count):
        return not self.standing_daily and count >= self.model_turn_limit

    def remaining_reserve(self, now):
        if self.temporary_reserve_until is not None and now < self.temporary_reserve_until:
            return self.temporary_reserve_percent
        return 5

    def __post_init__(self):
        for name, prefix in [('app_id', 'cli_'), ('chat_id', 'oc_'),
                             ('account_id', 'ou_'), ('bot_id', 'ou_')]:
            if not isinstance(getattr(self, name), str) or not re.fullmatch(
                    prefix + r'[A-Za-z0-9_-]+', getattr(self, name)):
                raise ContractError('daily policy requires explicit identity: ' + name)
        if self.model_budget_mode not in ('cumulative', 'standing_daily'):
            raise ContractError('explicit supported model budget mode required')
        if self.standing_daily:
            if self.model_turn_limit is not None:
                raise ContractError('standing daily policy requires a null cumulative ceiling')
        elif type(self.model_turn_limit) is not int or not 0 <= self.model_turn_limit <= 10000:
            raise ContractError('daily policy limit outside supported range: model_turn_limit')
        temporary = (self.temporary_reserve_percent, self.temporary_reserve_until)
        if temporary != (None, None):
            if (not self.standing_daily or type(temporary[0]) is not int or not 1 <= temporary[0] < 5
                    or type(temporary[1]) not in (int, float) or not math.isfinite(temporary[1]) or temporary[1] <= 0):
                raise ContractError('temporary reserve requires an explicit percentage and expiry')
        bounds = dict(poll_seconds=(15, 300),
                      http_per_minute=(10, 60), http_per_day=(1000, 20000),
                      http_confirmation_reserve=(20, 1000), page_size=(1, 50),
                      pages_per_tick=(1, 10), history_seconds=(60, 7 * 86400),
                      overlap_seconds=(1, 10), retry_attempts=(1, 15),
                      max_pending=(1, 500), max_asset_bytes=(1, 32 * 1024 * 1024),
                      paid_model_cny=(0, 0))
        for name, (low, high) in bounds.items():
            value = getattr(self, name)
            if type(value) is not int or not low <= value <= high:
                raise ContractError('daily policy limit outside supported range: ' + name)
        if self.http_confirmation_reserve >= self.http_per_day:
            raise ContractError('confirmation reserve must leave a working request allowance')

    @property
    def scope(self):
        return {k: getattr(self, k) for k in ('app_id', 'chat_id', 'account_id', 'bot_id')}

    @property
    def digest(self):
        value = asdict(self)
        if not self.standing_daily:
            for key in ('model_budget_mode', 'temporary_reserve_percent', 'temporary_reserve_until'):
                value.pop(key)  # Keep existing finite grants bound to their old digest.
        return hashlib.sha256(encoded(value).encode()).hexdigest()

    def retry_delay(self, attempt, retry_after=None):
        if type(attempt) is not int or not 1 <= attempt <= self.retry_attempts:
            raise ContractError('invalid durable retry attempt')
        delay = min(300, 5 * 2 ** min(attempt - 1, 6))
        if retry_after is not None:
            if type(retry_after) not in (int, float) or not 0 <= retry_after <= 86400:
                raise ContractError('invalid platform retry delay')
            delay = max(delay, retry_after)
        return delay
