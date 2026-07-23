"""测试全局隔离：任何测试不得使用环境/.env 里的真实 LLM key（防误打真实 API）。

需要 key 的测试用 monkeypatch.setenv 显式设置假值。
"""

import pytest


@pytest.fixture(autouse=True)
def _no_ambient_llm_keys(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
