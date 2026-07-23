"""运行配置。所有阈值可通过环境变量按部署调整，敏感信息不入库。"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="RESPONDER_", env_file=".env", extra="ignore"
    )

    # shadow: 只判断不发言，AI 草稿仅进入控制台; live: 自动发言
    mode: str = "shadow"

    wecom_corp_id: str = ""
    wecom_token: str = ""
    wecom_encoding_aes_key: str = ""
    wecom_corp_secret: str = ""
    wecom_agent_id: str = ""

    claude_model: str = "claude-opus-4-8"

    # AI 在群内的身份定位：普通销售顾问角色，不明示 AI 身份（业务决策 2026-07）。
    # 全量消息留痕入库，可溯源。
    ai_persona_name: str = "顾问助理"

    # 免责句式开关：业务决策暂不落地，机制保留，合伙人审定句式后置 True
    disclaimer_required: bool = False

    # ---- LLM 层（未配置 ANTHROPIC_API_KEY 时以下全部自动降级为纯规则/模板路径）
    # 边界样本复核：规则判「沉默」但可能是漏掉的问题时，交给模型二次确认
    llm_refine_enabled: bool = True
    # 直接回答路径用模型生成一般性法律框架
    llm_answer_enabled: bool = True
    llm_timeout_seconds: float = 15.0
    llm_max_tokens_answer: int = 500
    # 注入模型的群聊上下文条数
    history_window: int = 10
    # 群聊单条回复长度上限（字符），超出按句号截断
    answer_max_chars: int = 240

    # 分条发送：多句内容拆成 1~3 条微信消息，条间隔模拟打字节奏（真人感）
    split_messages: bool = True
    split_max_parts: int = 3
    split_delay_seconds: float = 1.5

    # AI 补位等待时长（秒）。[待定] 默认白天 2.5 分钟、夜间 1 分钟，可按群配置覆盖。
    wait_seconds_day: int = 150
    wait_seconds_night: int = 60
    night_start_hour: int = 21
    night_end_hour: int = 8

    # 律师群内发言后 AI 静默时长（秒）
    takeover_seconds: int = 1800

    # 紧急提醒升级时长（秒）
    escalation_seconds: int = 600

    db_path: str = "responder.db"
    api_host: str = "127.0.0.1"
    api_port: int = 8020


@lru_cache
def get_settings() -> Settings:
    return Settings()
