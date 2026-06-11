"""SQLAlchemy 异步引擎与会话管理。

支持两种模式：
- 独立部署：通过全局 settings 创建引擎
- 挂载模式：通过配置字典动态创建引擎
"""

from typing import Dict, Any, Optional

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from backend.core.config import settings

# ── 独立部署模式的全局引擎 ──────────────────────────────────────────
engine = create_async_engine(settings.DATABASE_URL, echo=False, pool_size=20, max_overflow=10)
async_session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    """ORM 基类。"""
    pass


async def get_db() -> AsyncSession:
    """FastAPI 依赖注入：获取数据库会话（独立部署模式）。"""
    async with async_session_factory() as session:
        try:
            yield session
        finally:
            await session.close()


# ── 挂载模式的动态引擎 ──────────────────────────────────────────────

def create_engine_from_config(cfg: Dict[str, Any]) -> async_sessionmaker:
    """从配置字典创建独立的异步引擎和会话工厂。

    供 EvalService 挂载时使用，不与全局 engine 冲突。

    Args:
        cfg: 配置字典，由 load_config_from_file() 返回，包含：
            DATABASE_URL, DATABASE_POOL_SIZE, DATABASE_MAX_OVERFLOW

    Returns:
        async_sessionmaker 实例，用于创建数据库会话。
    """
    db_url = cfg.get("DATABASE_URL", "")
    if not db_url:
        raise ValueError("配置中缺少 DATABASE_URL")

    pool_size = cfg.get("DATABASE_POOL_SIZE", 20)
    max_overflow = cfg.get("DATABASE_MAX_OVERFLOW", 10)

    _engine = create_async_engine(
        db_url,
        echo=False,
        pool_size=pool_size,
        max_overflow=max_overflow,
        pool_pre_ping=True,
    )
    return async_sessionmaker(_engine, class_=AsyncSession, expire_on_commit=False)
