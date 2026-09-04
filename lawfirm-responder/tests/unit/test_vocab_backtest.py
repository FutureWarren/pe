"""词表回测的守门测试（语料在 tests/data/kf_backtest.jsonl）。

律所方 2026-08-10 的两条要求，落成两个不许退步的数字：
  ①「让 AI 偏向自由回复，不要给它设那么多限制」→ 客户看到的处置必须全对，
    尤其**一条都不许漏答**；
  ②「问地址 / 问联系方式 / 问收费这类敏感问题直接转人工」→ 叫人判断必须全对。

为什么钉成 100% 而不是 95%：这份语料是**手写的、每条都有明确业务理由**的，
不是抽样标注。有一条对不上就说明词表真的漏了一种说法，
而漏掉的那一条在生产里是**静默**发生的——客户被晾着，后台一切正常。
"""

import json
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
DATA = ROOT / "tests/data/kf_backtest.jsonl"


def test_backtest_corpus_is_intact():
    rows = [json.loads(x) for x in DATA.read_text(encoding="utf-8").splitlines() if x.strip()]
    assert len(rows) >= 100, "语料不该缩水——每删一条就少守一种说法"
    for r in rows:
        assert r["speak"] in {"answer", "handoff", "urgent", "greeting", "silence", "identity"}
        assert r["human"] in (0, 1)
        assert r["why"], "每条都要写清为什么该这样——没有理由的标注改起来没人敢动"
    # 敏感三件套是律所方点名的，必须在语料里
    texts = " ".join(r["text"] for r in rows)
    for anchor in ("地址", "联系", "收费"):
        assert anchor in texts


def test_kf_backtest_is_clean():
    """回测脚本必须零失败。失败信息里已经写清了是哪一句、漏在哪个方向。"""
    r = subprocess.run(
        [sys.executable, "scripts/backtest_kf.py"],
        cwd=ROOT, capture_output=True, text=True,
    )
    assert r.returncode == 0, r.stdout + r.stderr


def test_sensitive_questions_pull_in_a_human():
    """律所方点名的三类：问地址 / 问联系方式 / 问收费 —— 一律叫真人。"""
    from responder.engine import priority, signals

    for text in ("你们地址在哪", "怎么联系你们", "你们收费多少",
                 "你们周末上班吗", "留个电话给我", "免费咨询吗"):
        why = priority.wants_human(signals.detect(text)[1])
        assert why, f"「{text}」该叫人却没叫"


def test_asking_for_the_address_still_gets_an_answer():
    """叫人**不等于**不答。地址就在配置里，让客户等一个律师回电话是荒唐的——
    正确做法是当场把地址给他，同时把会话转过去。"""
    from responder.engine import rules
    from responder.models import Action

    for text in ("你们地址在哪", "你们在几楼", "坐地铁怎么到你们所"):
        action, _, _, reasons = rules.classify(text, is_one_on_one=True)
        assert action == Action.ANSWER, text
        assert any(r.startswith("office-fact") for r in reasons), text


def test_ai_never_talks_money_by_itself():
    """「免费」也是一种承诺。问到钱一律承接，AI 不自己开口——
    包括「不要钱」这种听起来最友善的说法。"""
    from responder.engine import rules
    from responder.models import Action, Category

    for text in ("你们收费多少", "免费咨询吗", "咨询要钱吗", "能不能便宜点",
                 "你们收几个点", "可以风险代理吗"):
        action, category, _, _ = rules.classify(text, is_one_on_one=True)
        assert (action, category) == (Action.HANDOFF, Category.FEE), text


def test_our_own_marketing_copy_is_not_a_fee_question():
    """「【婚姻家事】律师一对一30分钟免费法律咨询」是我们自己的商品名。
    把它当成客户在问价，会让每一条导入的历史客资都白加一笔费用分——
    评分整体虚高，而没有任何地方看得出来。"""
    from responder.engine import rules, signals

    copy = "【婚姻家事】律师一对一30分钟免费法律咨询"
    assert "fee" not in signals.detect(copy)[1]
    assert not any(r.startswith("fee") for r in rules.classify(copy, is_one_on_one=True)[3])


def test_asking_our_business_hours_is_not_asking_our_rate():
    """回测揪出来的：「你们几点下班」被 FEE_BROAD 的「点」吃掉，
    于是客户问营业时间，AI 回一段「费用得律师了解情况后才能给准数」。"""
    from responder.engine import rules
    from responder.models import Action

    action, _, _, reasons = rules.classify("你们几点下班", is_one_on_one=True)
    assert action == Action.ANSWER
    assert any(r.startswith("office-fact") for r in reasons)


def test_statements_get_answered_in_the_intake_window():
    """「公司拖欠我三个月工资」没有问号，但他显然要一个答复。

    群聊维持原样：那里承办律师在场，AI 对着一句陈述抢答是越界。
    """
    from responder.engine import rules
    from responder.models import Action

    for text in ("公司拖欠我三个月工资", "公司一直不给我交社保"):
        assert rules.classify(text, is_one_on_one=True)[0] == Action.ANSWER, text
        assert rules.classify(text)[0] == Action.SILENCE, f"群聊不该变：{text}"


def test_the_two_wordlists_cannot_drift_apart():
    """规则层和信号层判的是同一批说法。各写一套的代价量过：
    规则层认得「留个电话给我」，信号层不认得，于是 AI 承接得好好的，
    **转接却一次也不触发**——客户在要人，系统看不见。"""
    from responder.engine import priority, rules, signals

    assert {k for k, _ in priority.WANTS_HUMAN} == signals.HOT_SIGNALS
    for text in ("留个电话给我", "我想直接跟律师聊", "怎么签合同",
                 "我下午过来找你们行吗", "你们做过类似的案子吗"):
        assert rules.classify(text, is_one_on_one=True)[0].value == "handoff", text
        assert priority.wants_human(signals.detect(text)[1]), text
