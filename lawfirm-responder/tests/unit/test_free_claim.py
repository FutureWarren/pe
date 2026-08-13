"""「免费咨询」这张牌：说得出口、问得住、且不会被自己的闸门整段拦掉。

律所方 2026-08-12 拍板主打免费法律咨询，并在我两次提出律协推广规则顾虑后
重申「你不要管，按我说的做」。落地上只有一条不让步：**只放行律所逐字定下的
那几句原话，费用闸门本身一个字不动。**

而这条落法自带一个陷阱，体检时发现它已经埋在七个地方：那句「免费」是硬编码的，
必须和 `approved_claims` **一字不差**才放行。谁把它润色一下，或者律所改了措辞，
出口闸门就会把**整条邀约**当成报价丢掉——地址、主任律师、带材料、免费四样一起没，
换成一句「这个得让律师来说比较准确」，恰好在最该请客户到所里的那一秒。

所以这一组守三件事：
  1. 每条含「免费」的话术都必须活着通过出口闸门（不被替换成兜底）；
  2. 客户直接问「你们咨询要钱吗」必须得到正面回答，不许含糊；
  3. 费用闸门没被这次改动拆松——「打三折」「代理费一万」照拦。
"""

from datetime import datetime

import pytest

from responder.compliance import forbidden
from responder.compliance.guard import guard
from responder.config import Settings
from responder.engine import rules
from responder.models import (
    Action,
    Category,
    ClientStatus,
    GroupProfile,
    IncomingMessage,
)
from responder.reply import templates
from responder.service import Pipeline
from responder.store.db import Store

OPEN_KFID = "wk-free"
EXT = "wmFreeCustomer"
GID = f"kf:{OPEN_KFID}:{EXT}"


class Kf:
    def __init__(self):
        self.sent: list[str] = []

    def available(self):
        return True

    def servicer_list(self, kfid):
        return ["wei"]

    def send_text(self, kfid, ext, text):
        self.sent.append(text)
        return True

    def transfer(self, kfid, ext, userid):
        return True


class Snd:
    def send_direct_text(self, userid, text):
        return True


def make(tmp_path, **over):
    db = str(tmp_path / "f.db")
    store = Store(db)
    cfg = dict(
        mode="live", db_path=db, split_messages=False, split_delay_seconds=0,
        wecom_kf_secret="s", llm_answer_enabled=False, llm_refine_enabled=False,
        lead_brief_enabled=False, office_address="九峰路88号",
    )
    cfg.update(over)
    settings = Settings(**cfg)
    kf = Kf()
    return store, kf, Pipeline(store, sender=Snd(), settings=settings, kf_client=kf)


def kf_group() -> GroupProfile:
    return GroupProfile(
        client_status=ClientStatus.PROSPECT, group_id=GID,
        kf_open_kfid=OPEN_KFID, kf_external_userid=EXT, case_type="劳动仲裁",
    )


def msg(text, mid="m1") -> IncomingMessage:
    return IncomingMessage(
        msg_id=mid, group_id=GID, sender_id=EXT, content=text,
        msg_type="text", created_at=datetime.now(), sender_is_staff=False,
    )


# ------------------------------------------------ ① 话术活着穿过出口闸门
# 每条变体都要单独跑：`_pick` 按 seed 选，线上到底命中哪一条取决于 msg_id，
# 只测一条等于只测了三分之一。
SEEDS = [f"seed-{i}" for i in range(12)]


@pytest.mark.parametrize("seed", SEEDS)
def test_office_invite_survives_the_compliance_gate(seed):
    """**这是本组最要紧的一条。** 被拦掉的表现不是报错，是客户收到一句套话。"""
    settings = Settings(office_address="九峰路88号")
    text = templates.office_invite(kf_group(), seed=seed, settings=settings)
    result = guard(text, Action.HANDOFF, templates.safe_fallback(kf_group()))
    assert result.passed, f"邀约被闸门拦掉了：{result.violations}\n{text}"
    assert result.text == text
    # 邀约的四样东西一样都不能少
    assert "主任律师" in text
    assert "材料" in text
    assert "九峰路88号" in text
    assert templates.free_claim(settings) in text


@pytest.mark.parametrize("seed", SEEDS)
def test_winback_survives_the_compliance_gate(seed):
    settings = Settings(office_address="九峰路88号")
    text = templates.winback(kf_group(), True, seed=seed, settings=settings)
    result = guard(text, Action.HANDOFF, templates.safe_fallback(kf_group()))
    assert result.passed, f"挽留被闸门拦掉了：{result.violations}"


@pytest.mark.parametrize("seed", SEEDS)
def test_greeting_opener_survives_the_compliance_gate(seed):
    text = templates.greeting_opener(kf_group(), seed=seed, settings=Settings())
    result = guard(text, Action.ANSWER, templates.safe_fallback(kf_group()))
    assert result.passed, f"开场白被闸门拦掉了：{result.violations}"


@pytest.mark.parametrize("seed", SEEDS)
def test_consult_is_free_survives_the_compliance_gate(seed):
    text = templates.consult_is_free(kf_group(), seed=seed, settings=Settings())
    result = guard(text, Action.HANDOFF, templates.safe_fallback(kf_group()))
    assert result.passed, f"「咨询免费」的正面回答被拦掉了：{result.violations}"


def test_wording_comes_from_config_not_from_seven_hardcoded_places():
    """律所改一次授权措辞，所有话术跟着走——不需要有人记得去改七个地方。"""
    custom = Settings(approved_claims="首次咨询不要钱|免费问")
    assert templates.free_claim(custom) == "首次咨询不要钱"
    for text in (
        templates.office_invite(kf_group(), seed="s", settings=custom),
        templates.winback(kf_group(), True, seed="s", settings=custom),
    ):
        assert "首次咨询不要钱" in text
        # 闸门必须拿**同一份**配置去比对，否则话术与闸门会悄悄错开
        assert guard(text, Action.HANDOFF, templates.safe_fallback(kf_group()),
                     settings=custom).passed


def test_revoking_the_authorization_silently_drops_the_claim():
    """律所收回授权（配置清空）时，话术不该替它许一个它没许过的承诺。"""
    none = Settings(approved_claims="")
    assert templates.free_claim(none) == ""
    text = templates.office_invite(kf_group(), seed="s", settings=none)
    assert "免费" not in text
    # 其余三样照常，句子仍然通顺
    assert "主任律师" in text and "材料" in text
    assert guard(text, Action.HANDOFF, templates.safe_fallback(kf_group()),
                 settings=none).passed


# ------------------------------------------------ ② 客户一问就得有话
def test_asking_whether_consultation_is_free_gets_a_straight_answer(tmp_path):
    """广告位天天喊免费、客户一问就含糊，他不会问第二遍，直接去问下一家。"""
    store, kf, p = make(tmp_path)
    store.upsert_group(kf_group())

    d = p.handle(msg("你们咨询要钱吗"))

    assert "fee:consult-free" in d.reasons
    assert kf.sent, "客户问了最要紧的那一句，不能不答"
    text = kf.sent[0]
    assert templates.free_claim(p.settings) in text
    assert "得律师" not in text, "这一句必须正面答，不许推给律师"


@pytest.mark.parametrize("q", [
    "咨询收费吗", "咨询是免费的吗", "问一下要钱吗", "免费的吗",
    "你们咨询收不收费", "聊聊要花钱吗",
])
def test_the_many_ways_customers_ask_it(q):
    _, cat, _, reasons = rules.classify(q, is_one_on_one=True)
    assert "fee:consult-free" in reasons, q
    # 仍然算「问了收费」→ 信号层的 fee → 转人工。问价的人正在比较，
    # 而比较时听到的是不是真人，直接决定他去谁那儿。
    assert cat == Category.FEE


@pytest.mark.parametrize("q", [
    "律师费怎么算", "代理费多少钱", "打官司大概多少钱", "风险代理怎么收",
])
def test_asking_about_case_fees_still_goes_to_a_human_without_a_number(q):
    """案子怎么收费照旧承接、绝不报价——这一层一个字都没动。"""
    _, cat, _, reasons = rules.classify(q, is_one_on_one=True)
    assert "fee:consult-free" not in reasons, q
    assert cat == Category.FEE


def test_group_chat_does_not_use_the_free_answer():
    """群里的客户已经委托了，「咨询免费」那句对他没有意义，照旧承接。"""
    _, _, _, reasons = rules.classify("咨询要钱吗", is_one_on_one=False)
    assert "fee:consult-free" not in reasons


# ------------------------------------------------ ③ 费用闸门没被拆松
@pytest.mark.parametrize("bad", [
    "我们代理费一万块", "可以给您打三折", "按标的额百分之十收",
    "咨询费 500 元", "首次咨询免费，之后每小时 800",
])
def test_the_fee_gate_is_untouched(bad):
    """授权的那句能说，别的关于钱的说法一句也漏不过去。"""
    assert forbidden.check(bad), f"这句该被拦下来：{bad}"


def test_the_authorized_phrase_alone_still_passes():
    assert forbidden.check("咨询是免费的") == []
