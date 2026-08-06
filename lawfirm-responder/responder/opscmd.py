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
            if not cid or not op or self.store.command_done(cid):
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
            logger.warning("运维指令结果无处可发：%s", text[:120])
            return False
        try:
            return self.sender.send_direct_text(target, f"【系统维护】\n{text}") is not False
        except Exception:
            logger.exception("运维指令结果发送失败")
            return False

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
            return (
                "没有可送达的接收人，令牌**未改动**。\n"
                "请先在指令里指定 to（企微 userid），"
                "或配置 RESPONDER_DEFAULT_NOTIFY_USERID。"
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

    def _op_report(self, item: dict) -> str:
        """把关键状态汇报一遍——远程排障时省掉一整轮来回提问。"""
        s = self.settings
        laws = self.store.list_lawyers(active_only=True)
        groups = self.store.list_groups()
        kf_ok = bool(self.kf_client and self.kf_client.available())
        return "\n".join([
            f"运行模式：{s.mode}",
            f"微信客服通道：{'正常' if kf_ok else '未配置'}",
            f"进线事件累计：{self.store.count_event_messages()} 条",
            f"律师名册：{len(laws)} 位",
            f"会话档案：{len(groups)} 个",
            f"对外地址：{s.public_base_url or '（未记录）'}",
        ])
