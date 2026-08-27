"""零售侧的装配点：一条真实消息进来，一条真实回复出去。

在这个文件出现之前，`responder/retail/` 是一个**没有人调用的库**——
每一块都单独测得过，合起来一条客户消息也处理不了。这一层把它们接上真实通道
（第一期是微信公众号，见 `gateway/mp.py`），并补上只有「接上线」之后才会出现的
四件事：

1. **额度记账**：微信客服消息是 48 小时 / 5 条。算错的代价不是少发一条，
   是接口报错 + 号被标记。所以发之前先算，算不出额度就**不发也不装作发了**。
2. **重复回执防治**：转人工的回执是「我叫同事来看一下」。客户接着说三句，
   旧写法就回三句一模一样的话——既烧掉 5 条额度里的 3 条，读起来也正是
   「这是个机器人」最典型的样子。**一次转人工只回一次执。**
3. **模式门控**：影子模式下全流程照跑、回复照样入库，只是不外发。
   上线第一周要的就是这个——先看它会说什么，再决定让不让它说。
4. **静默失败的出口**：额度用尽 / 发送失败 / 数据源不可用，三种都是
   「后台一切正常而客户什么也没收到」。每一种都落一条运维小记 + 计数器。

## 这里**不**过律所那道合规闸门

`compliance/forbidden.py` 拦的是承诺结果、预测判决、**提及任何费用金额**——
那是为律所写的。零售侧报价恰恰是本职工作，套上去等于把整条链路关死。
零售的出口闸门是另一件东西：`catalog.audit()`，逐个核对回复里的金额
是不是刚刚查出来的那个。两者的位置相同、判据相反，不要混。
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from responder.models import IncomingMessage
from responder.retail import replier
from responder.retail.sources import Sources

logger = logging.getLogger(__name__)

# 一次转人工之后，多久之内不再重复发回执。
# **3 分钟，不是 10 分钟。** 要压住的是「客户连发三四句补充」那个爆发，
# 它就发生在一两分钟里。窗口开得太长，五分钟后他问的另一件事
# 也会被一起吞掉——那不再是防复读，是让他收不到回音。
RECEIPT_QUIET_SECONDS = 180

# 一个客户的待办最多留几条。
TODO_KEEP = 10

_MEDIA_ZH = {"voice": "语音", "image": "图片", "video": "视频",
             "shortvideo": "小视频", "location": "位置", "link": "链接"}


@dataclass
class Inbound:
    """一条进来的客户消息，已从各通道的报文里剥出来。"""

    channel: str            # "mp"（公众号）/ 将来的 "kf" 等
    user_key: str           # openid / external_userid
    text: str
    msg_id: str
    is_staff: bool = False  # 真人（销售/客服）发的
    from_event: bool = False  # 关注/扫码/点菜单触发（额度是 1 分钟 3 条）
    at: datetime | None = None
    # 客户发的不是文字（语音/图片/视频…）。**不是「空消息」**——
    # 它是有内容的，只是我们读不了，两者的正确处理完全相反。
    media: str = ""

    @property
    def group_id(self) -> str:
        return f"{self.channel}:{self.user_key}"


@dataclass
class Result:
    """处理结果。`mode` 是排查时第一个要看的字段。"""

    reply: str = ""
    mode: str = "silent"    # live / shadow / blocked / failed / silent
    escalated: bool = False
    staff_note: str = ""
    reason: str = ""
    intent: str = ""
    audit_failed: bool = False
    category: str = ""      # 入库用；转人工回执统一记 "receipt"，见 _receipt_too_soon

    @property
    def delivered(self) -> bool:
        return self.mode == "live" and bool(self.reply)


class RetailPipeline:
    """把一条零售消息走完全程。

    `sender` 只需要一个 `send_text(user_key, text) -> bool`。公众号是
    `gateway.mp.MpClient`；换通道只换这一个对象，上面四件事一行不用改。
    """

    def __init__(
        self,
        store,
        *,
        sources: Sources | None = None,
        sender=None,
        mode: str = "shadow",
        takeover_seconds: int = 1800,
        store_hint: str = "",
        after_sale_default: bool = True,
    ) -> None:
        self.store = store
        self.sources = sources or Sources()
        self.sender = sender
        self.mode = mode
        self.takeover_seconds = takeover_seconds
        self.store_hint = store_hint
        # 酷机时代的客流主要是**线下成交之后**加过来的，所以默认按售后口径分意图
        # （「什么时候到」在售后是「我买的那台」，在售前是「你们什么时候有货」，
        # 两句话字面几乎一样，判错一次就是答非所问）。
        self.after_sale_default = after_sale_default

    # ------------------------------------------------------------------ 主流程
    def handle(self, msg: Inbound, *, now: datetime | None = None) -> Result:
        """`now` 是**处理时刻**，`msg.at` 是客户**发出**的时刻。

        两者刻意分开。混成一个的后果很具体：队列积压、进程重启、
        企微/微信补推——消息晚几十分钟甚至几十小时才轮到处理，
        而按「消息自己的时间」算窗口，`now - msg.at` 恒等于零，
        **48 小时那道闸就永远不会响**，直到接口开始报错才发现。
        窗口要拿真实的此刻去比。
        """
        now = now or datetime.now()
        gid = msg.group_id

        fresh = self.store.save_message(IncomingMessage(
            msg_id=msg.msg_id, group_id=gid, sender_id=msg.user_key,
            sender_is_staff=msg.is_staff, content=msg.text,
            msg_type="event" if msg.from_event else "text",
            created_at=msg.at or now,
        ))
        if not fresh:
            # 微信收不到及时响应会重推同一条（MsgId 相同）。不去重的话
            # 客户一句话被回三遍，而那三遍各吃掉一条额度。
            self.store.bump("retail_dupe")
            return Result(reason="重复回调，已忽略")

        if msg.is_staff:
            # 真人说话只做一件事：把接管时钟拨到现在。上面 save_message
            # 已经落库，`last_staff_reply_at` 立刻生效，AI 从下一条起让位。
            return Result(reason="真人发言，AI 让位")

        if msg.from_event:
            # 关注/扫码/点菜单：**不由我们回**。公众号后台自己配了关注回复
            # （酷机时代那个号还挂在云盛 ERP 上，模板消息也在那边发），
            # 我们再发一条，客户连着看到两句招呼——那正是「一看就知道是机器人」
            # 最典型的样子。与律所侧 `kf_welcome_on_enter` 默认关闭同一条理由：
            # **一个窗口只该有一个人在说话。**
            self.store.bump("retail_event")
            return Result(reason="关注/菜单事件，由公众号后台的自动回复负责")

        if msg.media:
            return self._unreadable(msg, now)

        out = replier.handle(
            msg.text,
            customer_key=msg.user_key,
            catalog=self.sources.catalog(),
            book=self.sources.book(),
            after_sale=self.after_sale_default,
            staff_replied_at=self.store.last_staff_reply_at(gid),
            now=now,
            takeover_seconds=self.takeover_seconds,
            store_hint=self.store_hint,
        )

        result = Result(
            reply=out.reply, escalated=out.escalate, staff_note=out.staff_note,
            reason=out.reason, intent=out.intent, audit_failed=out.audit_failed,
        )
        if out.audit_failed:
            self.store.bump("retail_audit_blocked")

        result.category = "receipt" if out.escalate else (result.intent or "retail")

        # 真人就在场时（standin 判了让位：不说话、也不用叫人）什么都不做。
        # 这时候再往待办上记一行「客户又说了什么」是多余的——那个人正看着屏幕。
        speechless = not out.reply and not out.escalate

        if not speechless and self._receipt_too_soon(gid, now):
            # **刚说完「我叫同事来看一下」，AI 就该安静几分钟。**
            #
            # 演示时抓到的那一幕：客户说「我要退货」（转人工），十五秒后补一句
            # 「买了才三天，还没拆封」——那是同一件事的下半句。可意图识别是
            # 无状态的，它按字面把这句判成了另一类，于是 AI 兴高采烈地答了
            # 一段跟退货毫无关系的话。**再多的词表也修不掉这个**：
            # 客户的补充说的是什么，本来就要看上一句才知道。
            #
            # 所以规则不在词表上，在时序上：**转人工之后的这几分钟属于那个人。**
            # 同理，给销售的那一行这时只搬原话、不下判断——一个不可信的意图标签
            # 印在待办上，比没有标签更容易把人带偏。
            # 代价是他这时真问了个新问题（「对了你们几点关门」）也要等一等，
            # 但他刚被告知有人要来，安静几分钟说得通；答错一段话，
            # 他会当场知道对面是台机器。
            return self._defer(msg, result, now)

        if out.escalate:
            self._notify_staff(gid, out.staff_note, now)

        if not result.reply:
            self._log(gid, msg.msg_id, result, now)
            return result

        return self._deliver(msg, result, now)

    def _defer(self, msg: Inbound, result: Result, now: datetime) -> Result:
        """把这一句让给刚接手的真人：不回客户，但**原话一定送到销售那边**。"""
        said = " ".join(msg.text.split())[:60] or f"一条{_MEDIA_ZH.get(msg.media, '消息')}"
        result.staff_note = f"（接上条）客户又说：{said}"
        self._notify_staff(msg.group_id, result.staff_note, now)
        result.reply = ""
        result.mode = "silent"
        result.reason += " → 刚转过人工，这几分钟让给真人"
        self.store.bump("retail_deferred_to_human")
        self._log(msg.group_id, msg.msg_id, result, now)
        return result

    def _unreadable(self, msg: Inbound, now: datetime) -> Result:
        """客户发来语音/图片/视频——有内容，但我们读不了。

        **不能沉默。** 沉默正是这套系统存在的理由要消灭的东西：客户发了东西
        没人应声，他不会想「机器人读不了语音」，他只会觉得没人管。
        也不能猜内容——对着一条没听过的语音答话，是这类系统出丑最快的方式。

        所以给一句实话 + 一条销售待办，并且**走回执那一档去重**：
        客户连发四张照片时只应该收到一句，不是四句。
        """
        what = _MEDIA_ZH.get(msg.media, "消息")
        result = Result(
            reply=f"您发的{what}我这边看不了，麻烦您打几个字说一下，"
                  f"或者留个方便的时间，我让同事给您回个电话。",
            escalated=True, intent=f"media:{msg.media}", category="receipt",
            reason=f"客户发的是{what}，读不了，转人工",
            staff_note=f"客户发了一条{what}，AI 读不了，需要你看一下。",
        )
        self.store.bump("retail_unreadable")
        if self._receipt_too_soon(msg.group_id, now):
            return self._defer(msg, result, now)
        self._notify_staff(msg.group_id, result.staff_note, now)
        return self._deliver(msg, result, now)

    # ------------------------------------------------------------------ 发送
    def _deliver(self, msg: Inbound, result: Result, now: datetime) -> Result:
        gid = msg.group_id

        if self.mode != "live":
            result.mode = "shadow"
            self._log(gid, msg.msg_id, result, now)
            return result

        b = self._budget(gid, now, from_event=msg.from_event)
        if not b.can_send:
            # **这是最贵的一种失败**：库里一切正常，客户一个字没收到。
            # 所以它既要落回复行（mode='blocked'，控制台看得见），
            # 也要变成销售那边的一条提醒——现在只有人能救这一句。
            result.mode = "blocked"
            self._log(gid, msg.msg_id, result, now)
            self.store.bump("retail_no_budget")
            self.store.set_note(
                f"retail_blocked:{gid}",
                f"{now:%m-%d %H:%M} 有一条回复没能发出去：{b.reason}。"
                f"原文：{result.reply[:60]}",
            )
            self._notify_staff(
                gid, f"⚠️ 客户问了「{result.intent or '未分类'}」，"
                     f"但{b.reason}，AI 没能回复他。需要你直接联系。", now,
            )
            return result

        ok = False
        if self.sender is not None:
            try:
                ok = bool(self.sender.send_text(msg.user_key, result.reply))
            except Exception:                          # noqa: BLE001
                logger.exception("retail send failed: %s", gid)
                ok = False

        result.mode = "live" if ok else "failed"
        self._log(gid, msg.msg_id, result, now)
        if not ok:
            self.store.bump("retail_send_failed")
            self.store.set_note(
                f"retail_send_failed:{gid}",
                f"{now:%m-%d %H:%M} 发送失败，客户没收到：{result.reply[:60]}",
            )
        return result

    def _budget(self, gid: str, now: datetime, *, from_event: bool):
        from responder.gateway import mp

        last = self.store.last_customer_message_at(gid)
        # 额度按「客户最后一次开口」起算——每次他说话窗口和条数一起重置。
        since = last or (now - timedelta(seconds=1))
        return mp.budget(last, self.store.sent_parts_since(gid, since),
                         now=now, from_event=from_event)

    # ------------------------------------------------------------------ 留痕
    def _log(self, gid: str, msg_id: str, result: Result,
             now: datetime | None = None) -> None:
        self.store.save_reply(
            msg_id=msg_id, group_id=gid, text=result.reply,
            mode=result.mode, passed=not result.audit_failed,
            category=result.category or result.intent or "retail", parts=1,
            created_at=now,
        )
        self.store.bump(f"retail_{result.mode}")

    def _notify_staff(self, gid: str, note: str, now: datetime) -> None:
        """给销售留一条待办。**追加，不是覆盖。**

        覆盖写过一版，演示时当场露馅：客户连问了四件事，计数器记着 4 次转人工，
        而销售那边只剩最后一件——前三件安静地被后一条盖掉了。这正是
        「系统自认为做完了，那头什么都没发生」那一类失败，而且它比沉默更坏，
        因为待办**看起来是有的**，没有人会想到去数它够不够。

        第一期落在运维小记里（控制台可查），不推企微——公众号这条通道上
        「谁该收到这一条」还没定（见 docs/retail-kuji.md）。**先让它存在**：
        一个查得到的待办远胜于一条没人知道的漏单。
        """
        if not note:
            return
        key = f"retail_todo:{gid}"
        old = [ln for ln in (self.store.get_note(key) or "").split("\n") if ln.strip()]
        line = f"{now:%m-%d %H:%M} " + " ".join(note.split())
        self.store.set_note(key, "\n".join([line, *old[:TODO_KEEP - 1]]))
        self.store.bump("retail_escalated")

    def _receipt_too_soon(self, gid: str, now: datetime) -> bool:
        """刚回过一次「我叫同事来」，就别再回第二次。

        判据刻意**只看回执那一类**（category="receipt"）：AI 两分钟前正常答过
        一个信息类问题，不该压住现在这句回执——那会让一个问退款的客户
        彻底收不到回音。

        """
        last = self.store.last_reply_at(gid, "receipt")
        if last is None:
            return False
        return (now - last).total_seconds() < RECEIPT_QUIET_SECONDS
