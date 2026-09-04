"""测试全局隔离：任何测试不得使用环境/.env 里的真实 LLM key（防误打真实 API）。

需要 key 的测试用 monkeypatch.setenv 显式设置假值。
"""

import pytest


@pytest.fixture(autouse=True)
def _no_ambient_llm_keys(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


@pytest.fixture(autouse=True)
def _isolate_dotenv(tmp_path, monkeypatch):
    """任何测试都不得读写仓库里那份开发用 .env。

    实际踩过：一个改令牌的用例把 RESPONDER_ADMIN_TOKEN 写进了真实 .env，
    后面所有靠「未配置令牌即本机放行」的用例统统 401——报错是 KeyError，
    跟真正的原因隔了十万八千里。persist_setting 用的是相对路径，
    把工作目录挪到临时目录即可一并解决读与写。
    """
    monkeypatch.chdir(tmp_path)


@pytest.fixture(autouse=True)
def _reset_login_lockout():
    """连续输错锁定是进程级状态，用例之间必须清空。

    不清的话，专门测「输错」的用例会把计数留给后面的用例，
    后者收到 429 而不是它期望的结果——排查起来极其误导。
    """
    from responder.console import api

    api._fails.clear()
    yield
    api._fails.clear()
