"""应用配置，通过环境变量或 .env 文件加载，同时支持 YAML/TOML 配置文件。"""

import os
from pathlib import Path
from typing import Any, Dict, Optional

import yaml
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """全局配置（env / .env 模式，用于独立部署）。"""

    # 数据库
    DATABASE_URL: str = "postgresql+asyncpg://aura:aura@localhost:5433/agent_eval"
    DATABASE_URL_SYNC: str = "postgresql+psycopg2://aura:aura@localhost:5433/agent_eval"

    # Redis
    REDIS_URL: str = "redis://localhost:6379/0"
    REDIS_KEY_PREFIX: str = "eval:events:"

    # Celery
    CELERY_BROKER_URL: str = "redis://localhost:6379/1"
    CELERY_RESULT_BACKEND: str = "redis://localhost:6379/2"

    # Ingest 消费参数
    FLUSH_INTERVAL_MS: int = 500
    FLUSH_BATCH_SIZE: int = 100

    # Trace 上报模式
    TRACE_MODE: str = "sdk"  # "sdk" | "otel"

    # Agent 版本
    AGENT_VERSION: str = "0.0.0"

    # 评测 LLM
    LLM_MODEL: str = "qwen3.7-max"
    LLM_API_KEY: str = ""
    LLM_BASE_URL: str = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
    LLM_TEMPERATURE: float = 0.0
    LLM_MAX_RETRIES: int = 3

    # 生产采样
    TRACE_RETENTION_DAYS: int = 30
    SAMPLING_DAILY_LIMIT: int = 100
    SAMPLING_RATIO: float = 0.05

    # ---- 7×24 Agent ----

    # Telegram Gateway
    TELEGRAM_BOT_TOKEN: str = ""
    TELEGRAM_MODE: str = "polling"        # polling | webhook
    TELEGRAM_ALLOWED_USERS: str = ""       # 逗号分隔的用户 ID 或 username
    TELEGRAM_PROXY: str = ""               # HTTP 代理（可选）
    TELEGRAM_REPLY_REACTION: str = "🤔"     # 回复处理中给原消息设置的 reaction；空字符串表示关闭

    # Dispatcher（预留）
    DISPATCHER_MODEL: str = "qwen3.7-max"
    DISPATCHER_MAX_HISTORY: int = 10

    # Runtime Web Brain bridge（仅本机调试入口）
    AGENT_RUNTIME_WEB_ENABLED: bool = True
    AGENT_RUNTIME_WEB_HOST: str = "127.0.0.1"
    AGENT_RUNTIME_WEB_PORT: int = 19090
    AGENT_RUNTIME_WEB_DEBUG_USER: str = ""
    AGENT_RUNTIME_WEB_TIMEOUT: float = 35.0

    @property
    def agent_runtime_web_base_url(self) -> str:
        """eval-api 代理到 agent_runtime Web bridge 的基础地址。"""
        return f"http://{self.AGENT_RUNTIME_WEB_HOST}:{self.AGENT_RUNTIME_WEB_PORT}"

    model_config = {
        "env_file": str(Path(__file__).resolve().parent.parent / ".env"),
        "extra": "ignore",
    }

    @property
    def telegram_allowed_users_set(self) -> set[str]:
        """解析逗号分隔的白名单为 set。"""
        if not self.TELEGRAM_ALLOWED_USERS.strip():
            return set()
        return {u.strip() for u in self.TELEGRAM_ALLOWED_USERS.split(",") if u.strip()}


settings = Settings()


def load_config_from_file(file_path: str) -> Dict[str, Any]:
    """从 YAML 或 TOML 配置文件加载数据库连接与基础设施配置。

    支持格式：
        - .yaml / .yml → PyYAML
        - .toml → tomllib（Python 3.11+ 标准库）

    返回扁平化字典，字段名与 Settings 对齐：
        {
            "DATABASE_URL": "postgresql+asyncpg://...",
            "REDIS_URL": "redis://...",
            "REDIS_KEY_PREFIX": "eval:events:",
            "FLUSH_INTERVAL_MS": 500,
            "FLUSH_BATCH_SIZE": 100,
            "CELERY_BROKER_URL": "redis://...",
            "CELERY_RESULT_BACKEND": "redis://...",
        }
    """
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"配置文件不存在: {file_path}")

    suffix = path.suffix.lower()
    if suffix in (".yaml", ".yml"):
        with open(path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f)
    elif suffix == ".toml":
        import tomllib
        with open(path, "rb") as f:
            raw = tomllib.load(f)
    else:
        raise ValueError(f"不支持的配置文件格式: {suffix}，仅支持 .yaml / .yml / .toml")

    databases = raw.get("databases", {})
    postgres = databases.get("postgres", {})
    redis_cfg = databases.get("redis", {})
    celery_cfg = raw.get("celery", {})
    ingest_cfg = raw.get("ingest", {})

    return {
        "DATABASE_URL": postgres.get("url", ""),
        "DATABASE_POOL_SIZE": postgres.get("pool_size", 20),
        "DATABASE_MAX_OVERFLOW": postgres.get("max_overflow", 10),
        "REDIS_URL": redis_cfg.get("url", "redis://localhost:6379/0"),
        "REDIS_KEY_PREFIX": redis_cfg.get("key_prefix", "eval:events:"),
        "FLUSH_INTERVAL_MS": ingest_cfg.get("flush_interval_ms", 500),
        "FLUSH_BATCH_SIZE": ingest_cfg.get("flush_batch_size", 100),
        "CELERY_BROKER_URL": celery_cfg.get("broker_url", ""),
        "CELERY_RESULT_BACKEND": celery_cfg.get("result_backend", ""),
    }
