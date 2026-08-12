"""把律师名册同步成微信客服的「接待人」名单。

## 为什么会有这个模块

律所侧需要维护的名单**只有两份**（2026-08-12 律所方明确要求）：

1. **律师名册**——控制台「团队」页那一份。律师就是客服，人员进出只改这里。
2. **企微后台的名单**——「微信客服 → 升级服务」里那个部门/成员范围。
   这份要在企微管理后台点，我们的应用没有写它的权限（接口只读）。

而企微在这两份之外还有第三份：客服账号的**接待人**列表。转接会话时企微会校验
「这个人是不是该客服账号的接待人」，不是就当场拒（errcode 95021/48007 那一档），
表现出来是「客户什么也没收到」。

它不该成为第三份要人记着维护的名单——它的正确内容永远等于第一份。
所以这里把它做成**名册的一份自动副本**：名册一变就同步，另外每隔一段时间兜一次底
（有人在企微后台手动删过接待人，或某次同步正好赶上网络抖动）。

## 为什么不在保存律师时同步调用

企微接口会超时。同步调用意味着「企微抖一下 → 律所侧看到保存失败」，
而名册本身其实已经存好了。所以保存时只打一个脏标记，真正的同步交给后台线程——
慢几秒没有代价，保存失败有。
"""

import logging
from datetime import datetime

from responder.gateway import wecom_kf as kf_errors

logger = logging.getLogger(__name__)

DIRTY_NOTE = "roster_dirty"      # 名册有变动，下一轮后台线程去同步
SYNCED_NOTE = "servicers_synced"  # 上一次同步完成的时间与结果（控制台自检要显示）


def mark_dirty(store) -> None:
    """名册发生任何变动时打一个脏标记。吞掉异常：同步是锦上添花，
    不能反过来让「加一位律师」这种基本操作失败。"""
    try:
        store.set_note(DIRTY_NOTE, datetime.now().isoformat())
    except Exception:
        logger.exception("mark roster dirty failed")


def is_dirty(store) -> bool:
    try:
        return bool(store.get_note(DIRTY_NOTE))
    except Exception:
        return False


def clear_dirty(store) -> None:
    try:
        store.set_note(DIRTY_NOTE, "")
    except Exception:
        logger.exception("clear roster dirty failed")


def accounts_in_use(store) -> set[str]:
    """真正有客户从这里进来的客服账号。

    企微会把**企业名下所有**客服账号都列出来，其中很可能有跟我们无关的
    （真机 2026-08-09：律所另有一个「上海松沪律师事务所在线客服」，
    人工在接，从没交给自建应用管）。对那种账号做任何管理动作都会拿到
    48007，于是一个碰不着的账号把整块自检染成红色——
    **说错「坏了」和漏报一样贵**，人会跑去修一个没坏的东西。

    还没有任何会话时（刚上线）返回空集，调用方按「全都算」处理，
    否则第一天什么都检查不了。
    """
    return {g.get("kf_open_kfid") for g in store.list_groups()
            if g.get("kf_open_kfid")}


def sync(store, client, in_use: set[str] | None = None) -> dict:
    """把在职名册推成各客服账号的接待人。幂等，可以随便重跑。

    返回 `{"ok", "accounts": [...], "roster": [...], "skipped": [...]}`。
    调用方负责判断 client 可用；这里只管做事和如实汇报。

    加完**立刻回读一次列表**，以回读结果为准报成功与否——不信写接口自己说的话。
    企微的批量写接口会为每个 userid 单独返回一个 errcode，只看外层 errcode=0
    会把「一个都没加上」读成成功。
    """
    userids = [law["userid"] for law in store.list_lawyers(active_only=True)
               if law.get("userid")]
    if not userids:
        return {"ok": False, "accounts": [], "roster": [], "skipped": [],
                "error": "律师名册为空：先在「团队」页添加律师"}
    accounts = client.account_list()
    if not accounts:
        return {"ok": False, "accounts": [], "roster": userids, "skipped": [],
                "error": "取不到客服账号：检查 Secret 与企微可信 IP"}

    used = in_use or set()
    out, skipped = [], []
    for a in accounts:
        kfid = a.get("open_kfid", "")
        if used and kfid not in used:
            # 别去动一个没有客户从中进来的账号：多半根本没交给我们管，
            # 加不上是正常的，报红反而误导人去修一个没坏的东西
            skipped.append(a.get("name", "") or kfid)
            continue
        raw = client.servicer_add(kfid, userids)
        after = set(client.servicer_list(kfid))
        out.append({
            "open_kfid": kfid,
            "name": a.get("name", ""),
            "added": sorted(set(userids) & after),
            "failed": sorted(set(userids) - after),
            "error": raw.get("error", ""),
            # 48007/60030 这类错误码对律所侧等于乱码，翻成一句能照着点的中文
            "hint": raw.get("hint", "") or kf_errors.err_hint(raw),
        })
    ok = bool(out) and all(not a["failed"] for a in out)
    return {"ok": ok, "accounts": out, "roster": userids, "skipped": skipped,
            "error": ""}


def record(store, result: dict) -> None:
    """把同步结果留成一行小记，供控制台自检显示「上次什么时候同步的」。"""
    when = datetime.now().strftime("%m-%d %H:%M")
    if result.get("error"):
        text = f"{when} 未同步：{result['error']}"
    elif result.get("ok"):
        n = len(result.get("roster") or [])
        text = f"{when} 已同步 {n} 位"
    else:
        bad = sorted({u for a in result.get("accounts") or [] for u in a["failed"]})
        text = f"{when} 有人没加上：{'、'.join(bad)}" if bad else f"{when} 同步未完成"
    try:
        store.set_note(SYNCED_NOTE, text)
    except Exception:
        logger.exception("record servicer sync failed")
