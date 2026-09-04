"""OpenClaw 桥接：手与大脑之间那一小段。

这一段代码很短，但它承担的是整条链上最容易静默出事的部分：
消息丢了没人知道、发失败了却销了账、那头停了两边都不报错。
所以测的全是失败路径。
"""

import importlib.util
import pathlib
import sys

import pytest

_PATH = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "openclaw_bridge.py"
_spec = importlib.util.spec_from_file_location("openclaw_bridge", _PATH)
bridge = importlib.util.module_from_spec(_spec)
sys.modules["openclaw_bridge"] = bridge
_spec.loader.exec_module(bridge)


class FakeBrain:
    def __init__(self, replies=None, boom=False):
        self._replies = replies if replies is not None else []
        self.boom = boom
        self.acked = []
        self.inbound_calls = []

    def inbound(self, **kw):
        self.inbound_calls.append(kw)
        if self.boom:
            raise TimeoutError("大脑连不上")
        return list(self._replies)

    def ack(self, ids):
        self.acked.extend(ids)

    def heartbeat(self):
        pass


class FakeHand(bridge.OpenClawAdapter):
    def __init__(self, fail_after=None):
        super().__init__(send_url="http://x", token="")
        self.sent = []
        self.fail_after = fail_after

    def send(self, to, text):
        if self.fail_after is not None and len(self.sent) >= self.fail_after:
            return False
        self.sent.append((to, text))
        return True


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(bridge, "SEND_GAP_SECONDS", 0)


# ------------------------------------------------------------ 字段归一
def test_reads_the_common_field_names():
    """平台改版加个前缀是常事。写死一个键，会在某次升级后
    **静默地**变成「一条消息都收不到」。"""
    hand = bridge.OpenClawAdapter()
    for payload in (
        {"from": "u1", "text": "在吗"},
        {"sender": "u1", "content": "在吗"},
        {"chatId": "u1", "message": "在吗"},
        {"peer": "u1", "body": {"text": "在吗"}},
    ):
        got = hand.parse_inbound(payload)
        assert got["external_id"] == "u1" and got["content"] == "在吗", payload


def test_unknown_sender_is_refused_not_guessed():
    """认不出发信人就别猜——猜错会把两个客户的对话混成一个人。"""
    assert bridge.OpenClawAdapter().parse_inbound({"text": "在吗"}) is None


# ------------------------------------------------------------ 正常路径
def test_replies_are_sent_and_acked_in_order():
    brain = FakeBrain([{"id": 1, "text": "第一句"}, {"id": 2, "text": "第二句"}])
    hand = FakeHand()

    out = bridge.handle_one(brain, hand, {"from": "u1", "text": "拖欠工资"})

    assert out == {"ok": True, "sent": 2, "queued": 2}
    assert [t for _, t in hand.sent] == ["第一句", "第二句"]
    assert brain.acked == [1, 2]


def test_silence_is_a_valid_outcome():
    """空数组不是出错，是「这条不需要回」——客户只发了个表情，
    或者律师已经接手了，AI 该闭嘴。"""
    brain, hand = FakeBrain([]), FakeHand()
    out = bridge.handle_one(brain, hand, {"from": "u1", "text": "[微笑]"})
    assert out == {"ok": True, "sent": 0, "queued": 0}
    assert hand.sent == [] and brain.acked == []


# ------------------------------------------------------------ 失败路径
def test_a_failed_send_is_never_acked():
    """销了账那句话就永远不会再出现，而客户根本没收到。"""
    brain = FakeBrain([{"id": 1, "text": "第一句"}, {"id": 2, "text": "第二句"}])
    hand = FakeHand(fail_after=1)

    bridge.handle_one(brain, hand, {"from": "u1", "text": "拖欠工资"})

    assert brain.acked == [1], "只该销掉真发出去的那条"


def test_unreachable_brain_reports_failure_so_the_message_is_retried():
    """吞掉异常 = 客户那句话消失了，而两边后台都看不出问题。"""
    brain, hand = FakeBrain(boom=True), FakeHand()
    out = bridge.handle_one(brain, hand, {"from": "u1", "text": "拖欠工资"})
    assert out["ok"] is False
    assert hand.sent == []


def test_a_broken_ack_does_not_lose_the_reply():
    """销账失败只意味着下轮重发一句，不该让整条链报错。"""
    brain = FakeBrain([{"id": 1, "text": "您好"}])
    hand = FakeHand()

    def boom(ids):
        raise RuntimeError("网络抖了一下")

    brain.ack = boom
    assert bridge.handle_one(brain, hand, {"from": "u1", "text": "你好"})["ok"] is True
    assert hand.sent, "话已经发到客户那儿了，这一点不受销账失败影响"


# ------------------------------------------------------------ 可换手
def test_the_openclaw_specific_bits_are_all_in_one_class():
    """换影刀、换自研脚本，只该改这一个类——这是不被任何一家工具
    绑死的原因，也是这套多渠道方案的前提。"""
    src = _PATH.read_text(encoding="utf-8")
    body = src.split("class OpenClawAdapter", 1)[1].split("\n# ---", 1)[0]
    assert "OPENCLAW_SEND_URL" in body
    # 换手的成本 = 要实现的方法数。收发主逻辑只许用这两个，多一个就多一处要改。
    flow = src.split("def handle_one", 1)[1].split("\ndef serve", 1)[0]
    used = {line.split("hand.", 1)[1].split("(", 1)[0]
            for line in flow.splitlines() if "hand." in line}
    assert used == {"parse_inbound", "send"}, f"手的接口应当只有两个方法，实际用到 {used}"


# ------------------------------------------------------------ 主动发起
class PollBrain(FakeBrain):
    def __init__(self, waiting, outbox):
        super().__init__()
        self._waiting = waiting
        self._outbox = outbox

    def pending(self, channel=""):
        return list(self._waiting)

    def outbox(self, external_id, channel=""):
        return list(self._outbox.get(external_id, []))


def test_proactive_messages_get_delivered_without_the_customer_speaking():
    """客户聊一半不说话了，系统会生成一句挽留。可对外部渠道来说，
    那句话排进发件箱之后没有任何人会来取——除非客户自己再开口，
    而他要是再开口，挽留本身就没意义了。"""
    brain = PollBrain(
        [{"external_id": "u1", "channel": "meituan", "count": 1}],
        {"u1": [{"id": 7, "text": "刚才的事您还需要了解吗"}]},
    )
    hand = FakeHand()

    assert bridge.deliver_pending(brain, hand) == 1
    assert hand.sent == [("u1", "刚才的事您还需要了解吗")]
    assert brain.acked == [7]


def test_proactive_send_failure_is_not_acked():
    brain = PollBrain(
        [{"external_id": "u1", "channel": "meituan", "count": 1}],
        {"u1": [{"id": 7, "text": "在吗"}]},
    )
    hand = FakeHand(fail_after=0)

    assert bridge.deliver_pending(brain, hand) == 0
    assert brain.acked == []


def test_a_conversation_without_an_external_id_is_skipped_not_crashed():
    """老数据可能没有 ext_user_id。跳过一条，别拖垮整轮。"""
    brain = PollBrain([{"external_id": "", "count": 3}], {})
    assert bridge.deliver_pending(brain, FakeHand()) == 0


def test_unreachable_brain_during_poll_does_not_crash_the_loop():
    class Dead(FakeBrain):
        def pending(self, channel=""):
            raise TimeoutError("连不上")

    assert bridge.deliver_pending(Dead(), FakeHand()) == 0
