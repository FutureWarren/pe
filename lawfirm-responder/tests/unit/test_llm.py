"""LLM 调用层测试：全部用假客户端，离线可回归。"""

import json
from types import SimpleNamespace

import pytest

from responder.engine import llm
from responder.models import Action, Category


class FakeClient:
    def __init__(self, text=None, stop_reason="end_turn", raise_exc=None):
        self._text = text
        self._stop = stop_reason
        self._exc = raise_exc
        self.last_kwargs = None
        self.messages = self

    def create(self, **kwargs):
        self.last_kwargs = kwargs
        if self._exc:
            raise self._exc
        content = [SimpleNamespace(type="text", text=self._text)] if self._text else []
        return SimpleNamespace(stop_reason=self._stop, content=content)


@pytest.fixture
def with_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")


def _patch_client(monkeypatch, fake):
    monkeypatch.setattr(llm, "_client", lambda timeout=15.0: fake)


def test_unavailable_without_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert llm.refine("消息", "claude-opus-4-8") is None
    assert llm.generate_answer_body("问题", "claude-opus-4-8") is None


def test_refine_parses_structured_output(with_key, monkeypatch):
    fake = FakeClient(text=json.dumps({
        "action": "handoff", "category": "case_status",
        "confidence": 0.85, "reason": "客户陈述了案件新情况",
    }))
    _patch_client(monkeypatch, fake)
    r = llm.refine("公司又找我谈话了", "claude-opus-4-8", case_type="劳动仲裁")
    assert r.action == Action.HANDOFF and r.category == Category.CASE_STATUS
    assert r.confidence == 0.85
    # 上下文进入了 user 消息而非 system（system 保持静态）
    assert "劳动仲裁" in fake.last_kwargs["messages"][0]["content"]
    assert "案件类型" not in fake.last_kwargs["system"]


def test_refine_refusal_returns_none(with_key, monkeypatch):
    _patch_client(monkeypatch, FakeClient(text=None, stop_reason="refusal"))
    assert llm.refine("消息", "claude-opus-4-8") is None


def test_refine_bad_json_returns_none(with_key, monkeypatch):
    _patch_client(monkeypatch, FakeClient(text="不是json"))
    assert llm.refine("消息", "claude-opus-4-8") is None


def test_refine_api_error_returns_none(with_key, monkeypatch):
    _patch_client(monkeypatch, FakeClient(raise_exc=RuntimeError("boom")))
    assert llm.refine("消息", "claude-opus-4-8") is None


def test_answer_body_ok(with_key, monkeypatch):
    fake = FakeClient(text="按劳动合同法的规定，一般可以主张经济补偿，具体看工作年限等因素。")
    _patch_client(monkeypatch, fake)
    body = llm.generate_answer_body(
        "被辞退了怎么赔？", "claude-opus-4-8",
        case_type="劳动仲裁", is_night=True, history_text="客户：之前问过一次",
    )
    assert "经济补偿" in body
    user_msg = fake.last_kwargs["messages"][0]["content"]
    assert "深夜" in user_msg and "之前问过一次" in user_msg


def test_answer_need_lawyer_sentinel_returns_none(with_key, monkeypatch):
    _patch_client(monkeypatch, FakeClient(text="[[NEED_LAWYER]]"))
    assert llm.generate_answer_body("我的案子能赢吗", "claude-opus-4-8") is None


def test_answer_refusal_returns_none(with_key, monkeypatch):
    _patch_client(monkeypatch, FakeClient(stop_reason="refusal"))
    assert llm.generate_answer_body("问题", "claude-opus-4-8") is None
