"""运维指令：让服务器主动去仓库取活干，结果回传到企业微信。

## 为什么要有这个

律所侧没有 SSH，而运维侧（含 Claude）出网受组织策略限制，够不着这台服务器
——两边都到不了对方。结果是每一件运维小事都得让律所方在浏览器上手点：
点升级、点自检、翻 DevTools 找令牌。今天为了取一个令牌折腾了半小时。

方向反过来就解开了：**服务器够得着 GitHub**。代码升级已经这么干了
（每 5 分钟自查远端分支），这里把同一招推广到运维动作：

    Claude 提交 ops/commands.json  →  服务器下一轮拉取时执行  →  结果发到企微

## 安全边界

不扩大任何权限。服务器**本来就**在自动执行这个分支上的代码（auto_update），
谁能往这个分支推提交，谁就已经能让它执行任意代码。一份声明式的指令清单
严格弱于那个能力：指令名走白名单，参数不进 shell。

真正要守的是另一条：**仓库是公开的，指令文件里绝不能出现密钥**。
所以「重置令牌」不接受指定值——由服务器随机生成，只发到企微私信里，
不落任何公开位置。

## 幂等

每条指令有唯一 id，执行过就入库，重复拉取不再执行。这一条不能省：
auto_update 每 5 分钟跑一轮，少了它，一条「重置令牌」会每五分钟重置一次。
"""

import json
import logging
import secrets
from pathlib import Path

from responder.compliance import forbidden
from responder.config import Settings, persist_setting

logger = logging.getLogger(__name__)

COMMANDS_FILE = "ops/commands.json"

# 好记又不好猜：常用汉语拼音词 + 随机数字。生成的令牌要发到企微里给人看，
# 一串 base64 抄起来容易错，而错一次就是又一轮排查。
_WORDS = (
    "songjiang", "jiufeng", "pinggao", "chayuan", "qingchen", "yuzhou",
    "lanting", "beichen", "hanmo", "zhuyun", "shuimo", "tingyu",
)


def _new_token() -> str:
    a, b = secrets.choice(_WORDS), secrets.choice(_WORDS)
    return f"{a}-{b}-{secrets.randbelow(900) + 100}"


class Runner:
    """执行仓库里待办的运维指令，逐条把结果回传给管理员。

    store/pipeline/sender 由 worker 注入——需要它们才能做「加接待人」
    这类真正有用的动作，而不只是改配置。
    """

    def __init__(self, settings: Settings, store, sender=None, kf_client=None):
        self.settings = settings
        self.store = store
        self.sender = sender
        self.kf_client = kf_client

    # ---------------------------------------------------------- 调度
    def run_pending(self) -> list[dict]:
        """读指令文件，执行没执行过的，返回本轮结果（供日志与测试）。"""
        path = Path(self.settings.update_repo_dir) / COMMANDS_FILE
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except Exception:
            logger.exception("运维指令文件读不动: %s", path)
            return []

        out = []
        for item in data.get("commands", []) or []:
            cid = str(item.get("id", "")).strip()
            op = str(item.get("op", "")).strip()
            # `_` 前缀留给运维小记（见 Store.set_note），指令不许占用——
            # 否则一条 id 为 `_ops_error` 的指令会跟小记互相覆盖
            if not cid or cid.startswith("_") or not op or self.store.command_done(cid):
                continue
            rollback = None
            try:
                text = self._dispatch(op, item)
                if isinstance(text, tuple):  # (结果文案, 送达失败时的回滚动作)
                    text, rollback = text
                ok = True
            except Exception as e:  # 一条指令炸了不能拖垮后面的，也不能拖垮 worker
                logger.exception("运维指令失败: %s %s", cid, op)
                text, ok = f"「{op}」执行失败：{str(e)[:200]}", False

            delivered = self._notify(self._target(item), text)
            if rollback and not delivered:
                # 改了令牌却没人收到新令牌 = 把人锁在门外，比不改坏得多。
                # 撤销，且**不落库**——下一轮企微恢复了会自动重来。
                rollback()
                logger.warning("运维指令送达失败，已回滚并留待下轮重试: %s", cid)
                continue
            self.store.mark_command_done(cid, text[:500])
            out.append({"id": cid, "op": op, "ok": ok, "result": text})
        return out

    def _dispatch(self, op: str, item: dict):
        fn = getattr(self, f"_op_{op}", None)
        if fn is None:
            return f"不认识的指令「{op}」，已跳过"
        return fn(item)

    def _target(self, item: dict | None = None) -> str:
        """结果发给谁。指令可以指定 to，否则按兜底链找。

        名册为空、兜底接收人也没配是**很常见的初始状态**（今天就是），
        所以这个函数返回空串是正常分支，不是异常。
        """
        return (
            (item or {}).get("to", "")
            or self.settings.default_notify_userid
            or self.settings.bot_default_notify_userid
            or next(
                (x["userid"] for x in self.store.list_lawyers(active_only=True)
                 if x.get("userid")),
                "",
            )
        )

    def _notify(self, target: str, text: str) -> bool:
        """把结果发到管理员企业微信。返回**是否确实发出去了**。

        返回值不是装饰性的：重置令牌那条指令靠它决定要不要回滚。
        """
        if not target or self.sender is None:
            self.store.set_note("ops_error", "没有收件人（名册为空且未配兜底接收人）")
            logger.warning("运维指令结果无处可发：%s", text[:120])
            return False
        try:
            ok = self.sender.send_direct_text(target, f"【系统维护】\n{text}") is not False
        except Exception as e:
            logger.exception("运维指令结果发送失败")
            self.store.set_note("ops_error", f"发送异常：{str(e)[:160]}")
            return False
        # 失败原因要留在能被远程读到的地方：律所侧没有服务器日志，
        # 不落这一笔，「没收到」就永远只是「没收到」。
        self.store.set_note(
            "ops_error",
            "" if ok else (getattr(self.sender, "last_error", "") or "企微拒绝发送"),
        )
        return ok

    # ---------------------------------------------------------- 指令
    def _op_reset_admin_token(self, item: dict):
        """重置控制台访问令牌。返回 (文案, 回滚动作)。

        **不接受指定值**：指令文件在公开仓库里，写进去的令牌等于公开。
        由服务器随机生成，只走企微私信这一条路送到人手上。

        这条指令唯一的危险不是被人滥用，而是**改成功了但通知没送到**——
        旧令牌当场失效、新令牌没人知道，等于把律所锁在自己的系统外面，
        比不改坏得多。所以两道保险：没有收件人就什么都不做；
        发送失败就回滚。宁可这条指令白跑一轮，也不能留下一扇锁死的门。
        """
        if not self._target(item) or self.sender is None:
            # 返回一个空回滚，为的是**不把这条指令记成已执行**：
            # 「没能通知到任何人」是待办，不是完成。踩过这个坑——名册还空着时
            # 这条指令跑了一轮、什么也没做、却被记成做完，等收件人配好之后
            # 它再也不会执行，人就一直等不到那条令牌。
            return (
                "没有可送达的接收人，令牌**未改动**。\n"
                "请先在指令里指定 to（企微 userid），"
                "或配置 RESPONDER_DEFAULT_NOTIFY_USERID。",
                lambda: None,
            )
        old = self.settings.admin_token
        token = _new_token()
        self.settings.admin_token = token
        persist_setting("RESPONDER_ADMIN_TOKEN", token)
        from responder.console import api as console_api

        console_api._fails.clear()  # 顺手解掉因反复输错造成的锁定

        def rollback() -> None:
            self.settings.admin_token = old
            persist_setting("RESPONDER_ADMIN_TOKEN", old)

        base = self.settings.public_base_url.rstrip("/")
        lines = [f"控制台访问令牌已重置为：\n{token}", "", "旧令牌立即失效。"]
        if base:
            lines += ["", f"免输入登录链接（存成书签，点一下直接进）：\n{base}/ui#t={token}"]
        return "\n".join(lines), rollback

    def _op_add_kf_servicers(self, item: dict) -> str:
        """把名册里的律师加为客服接待人——会话转接的硬前置。"""
        if self.kf_client is None or not self.kf_client.available():
            return "微信客服未配置，没法加接待人"
        userids = [x["userid"] for x in self.store.list_lawyers(active_only=True)
                   if x.get("userid")]
        if not userids:
            return "律师名册是空的：先在控制台「团队」页把律师加进去"
        lines = []
        for a in self.kf_client.account_list():
            kfid = a.get("open_kfid", "")
            raw = self.kf_client.servicer_add(kfid, userids)
            after = set(self.kf_client.servicer_list(kfid))
            missing = sorted(set(userids) - after)
            name = a.get("name", "") or kfid
            lines.append(
                f"「{name}」已就位 {len(set(userids) & after)} 位"
                + (f"，没加上：{'、'.join(missing)}" if missing else "")
                + (f"（接口报错：{raw['error']}）" if raw.get("error") else "")
            )
        return "接待人添加结果：\n" + "\n".join(lines) if lines else "取不到客服账号"

    def _op_add_lawyer(self, item: dict) -> str:
        """把一位律师加进名册，并在兜底接收人为空时把他设为兜底接收人。

        这条指令解的是一个**静默失败**：名册为空、兜底接收人也没配时，
        线索照样入库、评分照样算，但那张交接单**一个人也发不到**——
        控制台里看着一切正常，律师那边什么都没有。
        实测过：客户留了电话，系统安安静静地把它存进了数据库，没人知道。

        userid 是企微成员账号（通讯录里那个「账号」字段），不是密钥。
        """
        userid = str(item.get("userid", "")).strip()
        if not userid:
            return "缺 userid，没加"
        self.store.upsert_lawyer(userid, {
            "name": str(item.get("name", "") or userid),
            "specialties": str(item.get("specialties", "")),
            "role": str(item.get("role", "lawyer")),
            "on_duty": True,
            "active": True,
        })
        lines = [f"已加入律师名册：{item.get('name', '') or userid}（{userid}）"]
        if not self.settings.default_notify_userid:
            self.settings.default_notify_userid = userid
            persist_setting("RESPONDER_DEFAULT_NOTIFY_USERID", userid)
            lines.append("并设为线索/提醒的兜底接收人（此前为空＝交接单发不出去）")
        return "\n".join(lines)

    def _op_digest(self, item: dict) -> str:
        """立刻推一份战报（同 worker 每日自动推的那份，供随时调阅/测试）。"""
        from responder.digest import build_digest

        return build_digest(self.store, self.settings, days=int(item.get("days", 1)))

    def _op_backfill_memory(self, item: dict) -> str:
        """给存量会话补建客户记忆。

        定时扫描只回看 24 小时（够用且便宜），但**功能上线之前**就已经安静下来的
        会话落在窗口外，永远等不到那一次扫描——它们恰恰是最早那批客户，
        最可能回访，也最该被记住。这条指令把历史补齐，跑一次就够。
        """
        from responder import memory

        done = 0
        for row in self.store.list_groups():
            group = self.store.get_group(row["group_id"])
            if group is None or group.memory:
                continue
            text = memory.build_customer_memory(self.store, group)
            if text:
                self.store.set_memory(group.group_id, text)
                done += 1
        return f"已为 {done} 个存量会话补建客户记忆"

    def _op_import_knowledge(self, item: dict) -> str:
        """把仓库里的问答文件导进知识库（一行一条，Tab 或逗号分隔）。

        为什么走仓库而不是控制台：那 70 条抖音话术要经我这边先筛一遍合规，
        筛完的结果本来就在仓库里，让律所方再手工粘一遍纯属白费。
        **一律落 draft**，和控制台导入同一条规矩——条目就是话术，须人审后生效。
        知识库文件是问答口径，不是密钥，放公开仓库没有问题。
        """
        rel = str(item.get("file", "ops/knowledge.tsv")).strip()
        if rel.startswith("/") or ".." in rel:  # 只许读仓库里的东西
            return f"文件路径不合法：{rel}"
        path = Path(self.settings.update_repo_dir) / rel
        try:
            raw = path.read_text(encoding="utf-8-sig")
        except FileNotFoundError:
            return f"没找到问答文件：{rel}"
        source = str(item.get("source", "douyin")).strip() or "douyin"
        added = skipped = flagged = 0
        for line in raw.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t") if "\t" in line else line.split(",", 1)
            if len(parts) < 2:
                skipped += 1
                continue
            q, a = parts[0].strip().strip('"'), parts[1].strip().strip('"')
            if not q or not a or q in ("问题", "标准问题", "question"):
                skipped += 1
                continue
            if self.store.add_knowledge(q, a, source=source, status="draft"):
                added += 1
                if forbidden.check(a):
                    flagged += 1
            else:
                skipped += 1
        tail = f"，其中 {flagged} 条踩了禁止事项需先改写" if flagged else ""
        return (
            f"知识库导入 {added} 条（跳过 {skipped} 行）{tail}。"
            "全部落「待审核」，请到控制台「知识库」逐条通过后才会被 AI 引用。"
        )

    def _op_report(self, item: dict) -> str:
        """把关键状态汇报一遍——远程排障时省掉一整轮来回提问。"""
        s = self.settings
        laws = self.store.list_lawyers(active_only=True)
        groups = self.store.list_groups()
        kf_ok = bool(self.kf_client and self.kf_client.available())
        from responder import ops as _ops

        lines = [
            f"当前版本：{_ops.current_commit(s.update_repo_dir) or '未知'}",
            f"运行模式：{s.mode}",
            f"微信客服通道：{'正常' if kf_ok else '未配置'}",
            f"进线事件累计：{self.store.count_event_messages()} 条",
            f"律师名册：{len(laws)} 位",
            f"会话档案：{len(groups)} 个",
            f"对外地址：{s.public_base_url or '（未记录）'}",
        ]
        # 后台线程死活。这条指令本身就跑在那个线程里，所以能收到这份汇报
        # 就说明它活着——但把心跳时间报出来仍有意义：它能区分
        # 「一直在跑」和「刚被看门狗救回来」。
        beat = self.store.get_note("worker_heartbeat")
        lines.append(f"后台线程心跳：{beat or '（无记录，版本可能还没更新）'}")
        if restarted := self.store.get_note("worker_restarted"):
            lines.append(f"⚠️ {restarted}")
        # **最关键的一条：客户的消息到底有没有进来。**
        # 「客户说没人理」有两种完全不同的原因——消息没送到我们这儿（回调断了），
        # 还是送到了但没被回复（判断/发送的问题）。没有这个时间戳只能靠猜，
        # 而排一轮就是几个小时。
        if kf_ok:
            for a in self.kf_client.account_list():
                kfid = a.get("open_kfid", "")
                n = len(self.kf_client.servicer_list(kfid))
                last = self.store.last_inbound_at(f"kf:{kfid}:")
                when = last.strftime("%m-%d %H:%M") if last else "从来没有过"
                lines.append(
                    f"  客服账号「{a.get('name', '') or kfid}」"
                    f"接待人 {n} 位 · 最近收到客户消息：{when}"
                )
        # 最近一通对话在企微那边归谁接。0/2/4 都意味着没人在接，
        # 客户发什么都石沉大海，而我们这边看不出任何异常。
        recent = [g for g in groups if g.get("kf_open_kfid") and g.get("kf_external_userid")]
        if kf_ok and recent:
            g = recent[-1]
            names = {0: "未处理（没人在接！）", 1: "智能助手接待（正常）",
                     2: "待接入人工池", 3: "人工接待中", 4: "已结束"}
            try:
                st = self.kf_client.service_state(
                    g["kf_open_kfid"], g["kf_external_userid"])
            except Exception:
                st = None
            lines.append(f"最近一通会话状态：{names.get(st, st if st is not None else '查不到')}")
        return "\n".join(lines)
