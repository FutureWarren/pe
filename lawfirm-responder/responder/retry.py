"""有限次重试 + 逐次拉长间隔 + 放弃时留下看得见的痕迹。

## 为什么需要这个

2026-08-12 体检查出三处「无限重试」，它们的共同点是：**失败没有代价上限**，
而重试本身反过来把系统弄坏了。

- **加急提醒**：一位律师离职被移出应用可见范围、或备用接收人写错一个字母，
  升级提醒就会每 10 秒重试上百次。而这个循环占用的正是**处理所有客户消息的
  那唯一一条线程**——十几分钟里全所客户都收不到回复，各项指标看着还都正常。
  同时把企微应用打到限流，之后交接单、督办、每日战报、告警**全部一起静默失效**。
  等于系统亲手拆掉「出事有人知道」这条链。
- **静默挽留**：判重用的是「有没有实发过」，失败不算实发，于是每 10 秒重发一次、
  连打 24 小时（约 8000 次），还会把抖音那 6 条的发送配额算成早就用光——
  最要紧的「要电话」和「邀约到所」反而发不出去。
- **自动升级**：往分支推一个起不来的版本，服务器每 5 分钟重启两次、
  每次一分多钟不可用，**永远出不来**。

## 三条规矩

1. **有限次**：试够就停。一件事试五次还不成，第六次也不会成，
   而每一次都在挤占客户消息的处理时间。
2. **逐次拉长**：1 分钟、2 分钟、4 分钟……对端在限流时，密集重试只会延长限流。
3. **放弃时留痕**：落一条控制台看得见的小记。**静默地放弃是最坏的结果**——
   系统自认为处理完了，而那件事根本没发生，没有任何人知道。

计数用 `counters` 表（`Store.bump` / `Store.counter`），跨重启还在：
只放内存的话，自动升级一重启就清零，「有限次」形同虚设。
"""

import logging
from datetime import datetime

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 5
BASE_SECONDS = 60
CAP_SECONDS = 3600


def _key(kind: str, ident: str) -> str:
    return f"retry:{kind}:{ident}"


def should_try(
    store, kind: str, ident: str, *,
    max_attempts: int = MAX_ATTEMPTS, base_seconds: int = BASE_SECONDS,
    now: datetime | None = None,
) -> bool:
    """现在该不该再试一次。

    返回 False 有两种情况，调用方通常不必区分：还没到下次重试的时间，
    或者已经试够了（后者会由 `give_up` 落痕，这里不重复落）。
    """
    row = store.counter(_key(kind, ident))
    if row is None:
        return True
    n = row.get("n") or 0
    if n >= max_attempts:
        return False
    last = row.get("at")
    if not last:
        return True
    try:
        elapsed = ((now or datetime.now()) - datetime.fromisoformat(last)).total_seconds()
    except (TypeError, ValueError):
        return True
    return elapsed >= min(base_seconds * (2 ** (n - 1)), CAP_SECONDS)


def record_failure(store, kind: str, ident: str) -> int:
    """记一次失败，返回累计次数。"""
    key = _key(kind, ident)
    store.bump(key)
    row = store.counter(key) or {}
    return row.get("n") or 0


def succeeded(store, kind: str, ident: str) -> None:
    """成了就清零——下次再出问题时，重试次数该从头算。"""
    store.reset_counter(_key(kind, ident))


def exhausted(
    store, kind: str, ident: str, *, max_attempts: int = MAX_ATTEMPTS
) -> bool:
    row = store.counter(_key(kind, ident))
    return bool(row and (row.get("n") or 0) >= max_attempts)


def give_up(store, kind: str, ident: str, human_reason: str) -> None:
    """放弃时落一条控制台看得见的小记。

    `human_reason` 要写成一句所主任读得懂、并且知道该做什么的话——
    「escalation send failed」对他等于没说。
    """
    when = datetime.now().strftime("%m-%d %H:%M")
    try:
        store.set_note(f"gaveup:{kind}:{ident}", f"{when} {human_reason}")
    except Exception:
        logger.exception("give_up note failed: %s/%s", kind, ident)
    logger.error("gave up after retries: %s/%s — %s", kind, ident, human_reason)
