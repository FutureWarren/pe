"""AI 层与管道的集成：LLM 复核接线、LLM 回答注入、共情开场、追问三级策略。"""

from datetime import datetime

from responder.config import Settings
from responder.engine import llm
from responder.models import Action, Category, ClientStatus, GroupProfile, IncomingMessage
from responder.reply import templates
from responder.service import Pipeline
from responder.store.db import Store


def make_pipeline(tmp_path, monkeypatch=None, mode="live") -> Pipeline:
    settings = Settings(mode=mode, db_path=str(tmp_path / "t.db"))
    store = Store(settings.db_path)
    store.upsert_group(
        GroupProfile(
            group_id="g1", name="劳动纠纷群", client_status=ClientStatus.PROSPECT,
            case_type="劳动仲裁", lawyer_name="王", lawyer_userid="wang",
            backup_userid="li", robot_webhook="rk",
        )
    )

    class Sender:
        def __init__(self):
            self.robot, self.group, self.direct = [], [], []

        def send_robot_text(self, w, t):
            self.robot.append(t)
            return True

        def send_group_text(self, c, t):
            self.group.append(t)
            return True

        def send_direct_text(self, u, t):
            self.direct.append(t)
            return True

    p = Pipeline(store, Sender(), settings)
    return p


def _msg(content, msg_id="m1"):
    return IncomingMessage(msg_id=msg_id, group_id="g1", sender_id="u1", content=content)


# ---------------------------------------------------------------- LLM 复核接线
def test_refine_overrides_default_silence(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setattr(
        llm, "refine",
        lambda *a, **kw: llm.Refined(Action.HANDOFF, Category.CASE_STATUS, 0.9, "案件新情况"),
    )
    p = make_pipeline(tmp_path)
    # 规则会判 default-silence 的陈述句
    d = p.handle(_msg("今天人事部又找我单独谈了话"))
    assert d.action == Action.HANDOFF
    assert any(r.startswith("llm-refine") for r in d.reasons)


def test_refine_low_confidence_keeps_silence(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setattr(
        llm, "refine",
        lambda *a, **kw: llm.Refined(Action.HANDOFF, Category.CASE_STATUS, 0.5, "拿不准"),
    )
    p = make_pipeline(tmp_path)
    d = p.handle(_msg("今天人事部又找我单独谈了话"))
    assert d.action == Action.SILENCE


def test_refine_not_called_on_rule_hits(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    calls = []
    monkeypatch.setattr(llm, "refine", lambda *a, **kw: calls.append(1))
    p = make_pipeline(tmp_path)
    p.handle(_msg("你们收费怎么算？"))  # 规则命中 fee，不该进模型
    p.handle(_msg("谢谢王律师"))  # chitchat 快速通道，不该进模型
    assert calls == []


def test_refine_skipped_without_key(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    called = []
    monkeypatch.setattr(llm, "refine", lambda *a, **kw: called.append(1))
    p = make_pipeline(tmp_path)
    d = p.handle(_msg("今天人事部又找我单独谈了话"))
    assert d.action == Action.SILENCE and called == []


# ---------------------------------------------------------------- LLM 回答注入
def test_llm_answer_injected_with_history_and_scaffold(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    seen = {}

    def fake_gen(question, model, **kw):
        seen.update(kw, question=question)
        return "按劳动争议调解仲裁法，仲裁一般四十五天内审结，复杂的可延长。"

    monkeypatch.setattr(llm, "generate_answer_body", fake_gen)
    p = make_pipeline(tmp_path)
    p.handle(_msg("之前的事先不说了", msg_id="m0"))  # 造一条历史
    d = p.handle(_msg("仲裁一般要多久出结果？", msg_id="m2"), seconds_unanswered=300)
    assert d.action == Action.ANSWER
    reply = p.store.list_replies("g1")[0]
    assert "四十五天" in reply["text"]
    assert "约个时间" in reply["text"]  # 未成交群转化收尾
    assert seen["case_type"] == "劳动仲裁"
    assert "之前的事先不说了" in seen["history_text"]  # 群聊上下文注入


def test_llm_answer_failure_falls_back(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setattr(llm, "generate_answer_body", lambda *a, **kw: None)
    p = make_pipeline(tmp_path)
    d = p.handle(_msg("仲裁一般要多久出结果？"), seconds_unanswered=300)
    assert d.action == Action.ANSWER
    assert "answer:fallback-no-llm" in d.reasons
    reply = p.store.list_replies("g1")[0]
    assert "已转达王律师" in reply["text"]


def test_llm_answer_ai_selfref_discarded(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setattr(
        llm, "generate_answer_body", lambda *a, **kw: "作为AI，我认为一般是三个月。"
    )
    p = make_pipeline(tmp_path)
    p.handle(_msg("仲裁一般要多久？"), seconds_unanswered=300)
    reply = p.store.list_replies("g1")[0]
    assert "作为AI" not in reply["text"]  # 判废 → 降级模板


# ---------------------------------------------------------------- 共情开场
def test_opening_variants():
    day = datetime(2026, 7, 23, 14, 0)
    night = datetime(2026, 7, 23, 23, 30)
    assert "别慌" in templates.answer_opening("我好害怕，一般会判几年", day)
    assert "这么晚" in templates.answer_opening("仲裁要多久？", night)
    assert "不踏实" in templates.answer_opening("睡不着，好担心结果", night)
    # 常规情况不加开场，直接说事最像真人
    assert templates.answer_opening("仲裁要多久？", day) == ""


# ---------------------------------------------------------------- 话术变体
def test_handoff_variants_stable_and_diverse():
    g = GroupProfile(group_id="g", lawyer_name="王")
    a = templates.build_handoff(Category.CASE_STATUS, g, seed="msg-a")
    a2 = templates.build_handoff(Category.CASE_STATUS, g, seed="msg-a")
    assert a == a2  # 同一消息稳定
    texts = {templates.build_handoff(Category.CASE_STATUS, g, seed=f"m{i}") for i in range(20)}
    assert len(texts) > 1  # 不同消息有变化
