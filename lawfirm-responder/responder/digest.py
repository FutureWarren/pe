"""每日战报：把昨天发生的事推到管理员眼前，而不是等他来查。

控制台是给「在系统里干活的人」用的——律师看自己的单、点已联系。
所主任要的是另一样东西：一份**推过来**的摘要。指望他每天主动打开一个网页
去看几个数字，这件事不会持续超过一周；而一条早上九点到的企微消息会。

内容只回答三个问题，多一个字都是噪音：
  昨天进了多少人？有多少是真线索？谁在跟、跟上了没有？

**AI 说了什么不进战报**（律所方原话：「不想看到那么多 AI 对话，那么乱」）。
"""

import logging
from datetime import datetime, timedelta

from responder.config import Settings
from responder.store.db import Store

logger = logging.getLogger(__name__)


def _window(days: int, now: datetime | None = None) -> tuple[str, str, str]:
    """战报窗口：默认「昨天 0 点到今天 0 点」。

    早上九点推的时候，「今天」只有九个小时且大半没发生，
    拿它跟完整的前一天比毫无意义——所以战报永远说完整的昨天。
    """
    now = now or datetime.now()
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    start = today - timedelta(days=days)
    label = "昨天" if days == 1 else f"近 {days} 天"
    return start.isoformat(), today.isoformat(), label


def build_digest(
    store: Store, settings: Settings, days: int = 1, now: datetime | None = None
) -> str:
    since, until, label = _window(days, now)
    agg = store.lead_stats(since=since, until=until)
    st = agg["by_status"]
    staff = store.staff_performance(since=since, until=until)

    lines = [f"【{label}战报】{settings.office_name}"]
    total = agg["total"]
    if not total:
        lines.append(f"\n{label}没有新线索进来。")
        # 说清楚「没线索」和「系统坏了」的区别——否则连着几天零线索时，
        # 没人分得清是淡季还是通道断了，而后者每多一天都是真金白银
        lines.append("（若这不符合预期，请检查客服通道与二维码是否正常）")
        return "\n".join(lines)

    lines += [
        "",
        f"新线索 {total} 条 · 留了联系方式 {agg['with_contact']} 条"
        f"（{round(agg['with_contact'] * 100 / total)}%）",
        f"强意愿 P0 {agg['p0']} 条 · 待跟进 {st.get('new', 0)} 条"
        f" · 已联系 {st.get('contacted', 0)} 条 · 已成交 {st.get('converted', 0)} 条",
    ]

    if staff:
        lines += ["", "分律师："]
        for s in staff:
            bits = [f"分到 {s['assigned']}", f"跟进 {s['handled']}"]
            if s["converted"]:
                bits.append(f"成交 {s['converted']}")
            if s["avg_hours"] is not None:
                bits.append(
                    "响应 <1 小时" if s["avg_hours"] < 1 else f"响应 {s['avg_hours']} 小时"
                )
            line = f"· {s['name']}：{'，'.join(bits)}"
            # 未联系的 P0 单独点出来：这是整份战报里唯一需要管理员
            # 当场做点什么的一项，藏在一堆数字里等于没写
            if s["p0_pending"]:
                line += f"  ⚠️ 还有 {s['p0_pending']} 条 P0 没联系"
            lines.append(line)

    if agg.get("unassigned"):
        lines.append(f"\n⚠️ {agg['unassigned']} 条线索还没派给任何人")

    base = settings.public_base_url.rstrip("/")
    if base:
        lines += ["", f"完整表格与看板：{base}/ui"]
    return "\n".join(lines)


def digest_target(store: Store, settings: Settings) -> str:
    return (
        settings.daily_digest_userid
        or settings.default_notify_userid
        or next(
            (x["userid"] for x in store.list_lawyers(active_only=True)
             if x.get("role") == "admin" and x.get("userid")),
            "",
        )
    )
