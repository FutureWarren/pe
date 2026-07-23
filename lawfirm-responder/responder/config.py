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
