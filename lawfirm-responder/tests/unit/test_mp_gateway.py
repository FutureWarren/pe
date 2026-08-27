"""微信公众号（服务号）通道：酷机时代售后的主通道。

选它而不是微信客服，是因为品牌方管控下企微开不了客服号，而酷机时代有一个
**已认证服务号 + 两万多关注用户**。这条路比企微客服少了整整一个环节：
**不用引流**——那两万人已经在里面了。

这一组守的是平台的三条硬限制。它们不是我们的策略，是微信的规矩：
超发不是「多发了一条」，是接口报错 + 号被标记，整个客服能力一起没。
"""

from datetime import datetime, timedelta

import pytest

from responder.gateway import mp

NOW = datetime(2026, 8, 26, 15, 0, 0)


# ------------------------------------------------------------ ① 验签
def test_a_valid_signature_passes():
    token, ts, nonce = "kuji-token", "1756200000", "abc123"
    import hashlib
    sig = hashlib.sha1("".join(sorted([token, ts, nonce])).encode()).hexdigest()
    assert mp.verify(token, sig, ts, nonce) is True


def test_a_forged_signature_is_rejected():
    assert mp.verify("kuji-token", "deadbeef", "1756200000", "abc123") is False


def test_no_token_configured_means_the_door_is_shut():
    """**留空 = 接入口关闭，不是放行。**

    不验签等于把一个公网地址敞开：任何人都能伪造客户消息灌进来、
    骗走我们的客服消息额度、让 AI 对着一个伪造的「客户」说话。
    与抖音那条通道口径一致（默认拒绝）。
    """
    assert mp.verify("", "anything", "1756200000", "abc") is False


# ------------------------------------------------------------ ② XML 解析
def test_a_text_message_is_parsed():
    """**公众号回调是 XML，不是 JSON**，且字段首字母大写。

    这是跟企微最容易搞混的一处。
    """
    xml = """<xml>
      <ToUserName><![CDATA[gh_kuji]]></ToUserName>
      <FromUserName><![CDATA[oABC123]]></FromUserName>
      <CreateTime>1756200000</CreateTime>
      <MsgType><![CDATA[text]]></MsgType>
      <Content><![CDATA[我那台什么时候能到啊]]></Content>
      <MsgId>24000001</MsgId>
    </xml>"""
    m = mp.parse(xml)
    assert m is not None
    assert m.openid == "oABC123"
    assert m.is_text and m.content == "我那台什么时候能到啊"
    assert m.msg_id == "24000001"


def test_a_follow_event_is_parsed():
    xml = """<xml>
      <FromUserName><![CDATA[oXYZ789]]></FromUserName>
      <CreateTime>1756200000</CreateTime>
      <MsgType><![CDATA[event]]></MsgType>
      <Event><![CDATA[subscribe]]></Event>
    </xml>"""
    m = mp.parse(xml)
    assert m is not None and m.is_event and m.event == "subscribe"


def test_broken_xml_never_raises():
    """**解析失败绝不抛异常。**

    微信收不到 200 就会重推，重推三次之后它认为我们的服务挂了。
    一条读不懂的消息不值得把整条通道拖下水。
    """
    assert mp.parse("这不是 XML") is None
    assert mp.parse("<xml><FromUserName></FromUserName></xml>") is None


def test_the_same_message_pushed_twice_dedupes_to_one_key():
    """微信在没收到及时响应时**会重推同一条消息**，MsgId 相同。

    不去重的话客户一句话会被回三遍，而那三遍还各吃掉一条 5 条额度里的份额。
    """
    xml = ("<xml><FromUserName><![CDATA[oA]]></FromUserName>"
           "<MsgType><![CDATA[text]]></MsgType><Content><![CDATA[在吗]]></Content>"
           "<MsgId>24000001</MsgId><CreateTime>1756200000</CreateTime></xml>")
    assert mp.parse(xml).dedupe_key == mp.parse(xml).dedupe_key


def test_events_without_a_msgid_still_get_a_stable_key():
    xml = ("<xml><FromUserName><![CDATA[oA]]></FromUserName>"
           "<MsgType><![CDATA[event]]></MsgType><Event><![CDATA[subscribe]]></Event>"
           "<CreateTime>1756200000</CreateTime></xml>")
    k = mp.parse(xml).dedupe_key
    assert k and k == mp.parse(xml).dedupe_key


# ------------------------------------------------------------ ③ 额度（最要紧）
def test_a_customer_who_never_spoke_cannot_be_messaged():
    """微信不允许我们主动发起客服消息。"""
    b = mp.budget(None, 0, now=NOW)
    assert b.can_send is False
    assert "主动发起" in b.reason


def test_within_the_window_five_messages_are_allowed():
    b = mp.budget(NOW - timedelta(hours=2), 0, now=NOW)
    assert b.remaining == 5


def test_each_sent_message_eats_one():
    b = mp.budget(NOW - timedelta(hours=2), 3, now=NOW)
    assert b.remaining == 2


def test_the_quota_runs_out_and_says_so():
    b = mp.budget(NOW - timedelta(hours=2), 5, now=NOW)
    assert b.can_send is False
    assert "额度已用完" in b.reason


def test_past_forty_eight_hours_nothing_can_be_sent():
    """**超窗口不是「少发一条」，是接口直接拒。**

    而且这时候重试是纯浪费——等客户再开口窗口才重置。
    """
    b = mp.budget(NOW - timedelta(hours=49), 0, now=NOW)
    assert b.can_send is False
    assert "48 小时" in b.reason


def test_the_event_window_is_much_tighter():
    """关注/扫码/点菜单触发的是另一套：1 分钟、3 条。

    这一条直接决定「客户扫码关注时能不能立刻打招呼」——
    按 48 小时那套算会以为还有时间，实际上一分钟就关门了。
    """
    ok = mp.budget(NOW - timedelta(seconds=30), 0, now=NOW, from_event=True)
    assert ok.remaining == 3
    late = mp.budget(NOW - timedelta(seconds=90), 0, now=NOW, from_event=True)
    assert late.can_send is False
    assert "1 分钟" in late.reason or "分钟窗口" in late.reason


def test_the_platform_limits_are_not_ours_to_tune():
    """这两个常量是平台规则，不是我们的策略参数。改它等于改微信的规矩。"""
    assert mp.WINDOW_SECONDS == 48 * 3600
    assert mp.MAX_PER_WINDOW == 5


def test_our_own_split_cap_leaves_headroom():
    """分条上限定 3 而不是 5：一轮回复占满额度的话，
    客户追问时我们一个字都发不出去。"""
    from responder.config import Settings
    assert Settings().mp_split_max_parts < mp.MAX_PER_WINDOW


# ------------------------------------------------------------ ④ 错误码
@pytest.mark.parametrize("code,must_contain", [
    (45015, "48 小时"),
    (45047, "5 条"),
    (48001, "已认证的服务号"),
    (40003, "openid"),
])
def test_error_codes_say_what_to_do(code, must_contain):
    hint = mp.err_hint({"errcode": code})
    assert hint and must_contain in hint


def test_the_window_error_tells_you_not_to_retry():
    """45015 重试一万次也没用——必须说清楚，否则就会有人加重试逻辑。"""
    assert "别重试" in mp.err_hint({"errcode": 45015})


def test_an_unknown_code_stays_silent():
    assert mp.err_hint({"errcode": 999999}) == ""
    assert mp.err_hint(None) == ""


# ------------------------------------------------------------ ⑤ 被动回复
def test_passive_reply_builds_valid_xml():
    """被动回复不消耗那 5 条额度——第一期不走，但接口留着。"""
    xml = mp.passive_text("oABC", "gh_kuji", "已经发出来了，走的顺丰。")
    assert "<ToUserName><![CDATA[oABC]]></ToUserName>" in xml
    assert "顺丰" in xml
    import xml.etree.ElementTree as ET
    assert ET.fromstring(xml).findtext("MsgType") == "text"


# ------------------------------------------------------------ ⑥ 调用凭据
# 酷机时代那个服务号**同时授权给了云盛 ERP**（模板消息在那边发）。
# 谁去刷 access_token 都会把对方的顶掉，而症状是两边间歇性报 40001、
# 重试有时又好了——看起来像网络抖动，实际上是两个系统在抢同一把钥匙。
class _Resp:
    def __init__(self, payload):
        self._p = payload

    def json(self):
        return self._p


def _client(monkeypatch, *, stable=None, plain=None):
    from responder.config import Settings

    calls: list[str] = []

    def fake_post(url, **_kw):
        calls.append(url)
        return _Resp(stable if stable is not None else {"errcode": 48001})

    def fake_get(url, **_kw):
        calls.append(url)
        return _Resp(plain or {"access_token": "PLAIN", "expires_in": 7200})

    monkeypatch.setattr(mp.requests, "post", fake_post)
    monkeypatch.setattr(mp.requests, "get", fake_get)
    c = mp.MpClient(Settings(mp_app_id="wx", mp_app_secret="s"))
    return c, calls


def test_the_token_comes_from_the_stable_endpoint(monkeypatch):
    """`force_refresh=false` 时它返回当前有效的那一个，**不会让别人的失效**。"""
    c, calls = _client(monkeypatch, stable={"access_token": "STABLE", "expires_in": 7200})
    assert c._access_token() == "STABLE"
    assert any("stable_token" in u for u in calls)
    assert not any(u.endswith("/cgi-bin/token") for u in calls)


def test_an_account_without_the_stable_endpoint_still_works(monkeypatch):
    """老账号万一没有这个接口，要回落——回落之后那个坑重新打开，所以要留痕。"""
    c, calls = _client(monkeypatch)
    assert c._access_token() == "PLAIN"
    assert any(u.endswith("/cgi-bin/token") for u in calls)


def test_the_token_is_cached_until_shortly_before_it_expires(monkeypatch):
    """每发一条消息都去换一次凭据，等于每发一条就跟同号的另一套系统抢一次。"""
    c, calls = _client(monkeypatch, stable={"access_token": "STABLE", "expires_in": 7200})
    c._access_token()
    n = len(calls)
    c._access_token()
    assert len(calls) == n
