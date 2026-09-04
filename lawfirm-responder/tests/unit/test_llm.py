"""LLM 调用层测试：全部用假客户端/假 HTTP，离线可回归。覆盖 DeepSeek 与 Anthropic 双后端。"""

import json
from types import SimpleNamespace

import pytest

from responder.config import Settings
from responder.engine import llm
from responder.models import Action, Category

DS_SETTINGS = Settings(llm_provider="auto")


# ---------------------------------------------------------------- 供应商解析
def test_resolve_prefers_deepseek_on_auto(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "dk")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ak")
    p = llm.resolve(Settings(llm_provider="auto"))
    assert p.name == "deepseek" and p.model == "deepseek-chat"


def test_resolve_explicit_anthropic(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "dk")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ak")
    p = llm.resolve(Settings(llm_provider="anthropic"))
    assert p.name == "anthropic" and p.model == "claude-opus-4-8"


def test_auto_does_not_silently_fall_back_to_the_overseas_provider(monkeypatch):
    """DeepSeek 的 key 掉了、环境里恰好还有个 ANTHROPIC_API_KEY，
    旧写法会**静默地**把客户的咨询原文改发给境外服务商——
    《个人信息保护法》上那是从「向第三方提供」变成「个人信息出境」，
    要求完全不同，而律所对此毫不知情。

    退回确定性话术（规则 + 模板照常工作，客户仍然有人应）比换个处理者安全得多。
    """
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ak")
    assert llm.resolve(Settings(llm_provider="auto")) is None
    # 显式配了就照办——钉死不等于锁死
    assert llm.resolve(Settings(llm_provider="anthropic")).name == "anthropic"


def test_resolve_none_without_keys(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert llm.resolve(Settings(llm_provider="auto")) is None
    assert llm.refine("消息", settings=Settings()) is None
    assert llm.generate_answer_body("问题", settings=Settings()) is None


# ---------------------------------------------------------------- DeepSeek 后端
class FakeHttpResponse:
    def __init__(self, status_code=200, content="", payload=None):
        self.status_code = status_code
        self.text = "err"
        self._payload = payload or {
            "choices": [{"message": {"content": content}}]
        }

    def json(self):
        return self._payload


@pytest.fixture
def ds_env(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "dk")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


def test_deepseek_refine_parses_json(ds_env, monkeypatch):
    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured.update(url=url, headers=headers, payload=json)
        return FakeHttpResponse(content=json_mod.dumps({
            "action": "handoff", "category": "case_status",
            "confidence": 0.9, "reason": "案件新情况",
        }))

    import json as json_mod
    monkeypatch.setattr(llm.httpx, "post", fake_post)
    r = llm.refine("公司又找我谈话了", case_type="劳动仲裁", settings=DS_SETTINGS)
    assert r.action == Action.HANDOFF and r.confidence == 0.9
    assert captured["url"] == llm.DEEPSEEK_URL
    assert captured["headers"]["Authorization"] == "Bearer dk"
    assert captured["payload"]["response_format"] == {"type": "json_object"}
    assert captured["payload"]["model"] == "deepseek-chat"
    # JSON 字段说明进入 system（DeepSeek 无原生 schema 强制）
    assert "confidence" in captured["payload"]["messages"][0]["content"]


def test_deepseek_answer_body(ds_env, monkeypatch):
    monkeypatch.setattr(
        llm.httpx, "post",
        lambda *a, **kw: FakeHttpResponse(content="按劳动合同法的规定，一般可以主张经济补偿。"),
    )
    body = llm.generate_answer_body("被辞退了怎么赔？", settings=DS_SETTINGS)
    assert "经济补偿" in body


def test_deepseek_http_error_returns_none(ds_env, monkeypatch):
    monkeypatch.setattr(llm.httpx, "post", lambda *a, **kw: FakeHttpResponse(status_code=429))
    assert llm.generate_answer_body("问题", settings=DS_SETTINGS) is None


def test_deepseek_need_lawyer_returns_none(ds_env, monkeypatch):
    monkeypatch.setattr(
        llm.httpx, "post", lambda *a, **kw: FakeHttpResponse(content="[[NEED_LAWYER]]")
    )
    assert llm.generate_answer_body("你们收多少钱", settings=DS_SETTINGS) is None


def test_deepseek_exception_returns_none(ds_env, monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("network down")

    monkeypatch.setattr(llm.httpx, "post", boom)
    assert llm.refine("消息", settings=DS_SETTINGS) is None


# ---------------------------------------------------------------- Anthropic 后端
class FakeAnthropicClient:
    def __init__(self, text=None, stop_reason="end_turn"):
        self._text = text
        self._stop = stop_reason
        self.messages = self

    def create(self, **kwargs):
        content = [SimpleNamespace(type="text", text=self._text)] if self._text else []
        return SimpleNamespace(stop_reason=self._stop, content=content)


@pytest.fixture
def an_env(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ak")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)


def test_anthropic_refine(an_env, monkeypatch):
    fake = FakeAnthropicClient(text=json.dumps({
        "action": "silence", "category": "chitchat", "confidence": 0.8, "reason": "闲聊",
    }))
    monkeypatch.setattr(llm, "_anthropic_client", lambda timeout=15.0: fake)
    r = llm.refine("随便聊聊", settings=Settings(llm_provider="anthropic"))
    assert r.action == Action.SILENCE and r.category == Category.CHITCHAT


def test_anthropic_refusal_returns_none(an_env, monkeypatch):
    monkeypatch.setattr(
        llm, "_anthropic_client",
        lambda timeout=15.0: FakeAnthropicClient(stop_reason="refusal"),
    )
    assert llm.generate_answer_body("问题", settings=Settings(llm_provider="anthropic")) is None
