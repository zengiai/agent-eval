"""EvalService —— 挂载式评测服务入口。

将评测系统以 Python 对象形式内嵌在 Agent 进程中运行，
通过 YAML/TOML 配置文件加载数据库连接，通过构造函数传入功能开关。

用法:
    eval_service = EvalService(
        config_file="eval_config.yaml",
        enabled_layers=["intent", "generation", "outcome"],
        sampling_rate=0.05,
        llm={"model": "qwen3.7-max", "api_key": "sk-xxx"},
    )
    await eval_service.mount()

    # ... Agent 正常运行 ...

    await eval_service.unmount()
"""

import asyncio
import logging
import threading
from typing import Dict, List, Optional, Any

import uvicorn

from backend.core.config import load_config_from_file, settings
from backend.core.database import create_engine_from_config
from backend.runner.engine import EvaluationOrchestrator
from backend.workers.ingest_worker import IngestWorker
from backend.api import app as fastapi_app

logger = logging.getLogger(__name__)

# ── 默认五层评测权重 ─────────────────────────────────────────────────
DEFAULT_ENABLED_LAYERS = ["intent", "retrieval", "tool", "generation", "outcome"]


class EvalService:
    """挂载式评测服务。

    聚合管理：
        - 数据库连接（从配置文件加载）
        - Ingest 消费者（asyncio Task）
        - 评测引擎（EvaluationOrchestrator）
        - FastAPI 看板（独立线程）

    生命周期：
        __init__ → mount() → [Agent 运行] → unmount()
    """

    def __init__(
        self,
        config_file: str = "eval_config.yaml",
        enabled_layers: Optional[List[str]] = None,
        sampling_rate: float = 0.05,
        sampling_daily_limit: int = 100,
        llm: Optional[Dict[str, Any]] = None,
    ):
        # ── 加载数据库配置 ───────────────────────────────────────────
        self._cfg = load_config_from_file(config_file)
        self._session_factory = create_engine_from_config(self._cfg)

        # ── 功能参数 ─────────────────────────────────────────────────
        self.enabled_layers = enabled_layers or DEFAULT_ENABLED_LAYERS
        self.sampling_rate = sampling_rate
        self.sampling_daily_limit = sampling_daily_limit
        self._llm = llm or {
            "model": "qwen3.7-max",
            "api_key": settings.LLM_API_KEY,
            "base_url": settings.LLM_BASE_URL,
            "temperature": settings.LLM_TEMPERATURE,
            "max_retries": settings.LLM_MAX_RETRIES,
        }

        # ── 子组件（init 中创建，mount 中启动）───────────────────────
        self._ingest_worker = IngestWorker(
            session_factory=self._session_factory,
            redis_url=self._cfg.get("REDIS_URL"),
            redis_key_prefix=self._cfg.get("REDIS_KEY_PREFIX"),
            flush_interval_ms=self._cfg.get("FLUSH_INTERVAL_MS", 500),
            flush_batch_size=self._cfg.get("FLUSH_BATCH_SIZE", 100),
        )
        self._orchestrator = EvaluationOrchestrator(
            config={
                "enabled_layers": self.enabled_layers,
                "llm": self._llm,
            }
        )

        # ── 运行时状态 ───────────────────────────────────────────────
        self._ingest_task: Optional[asyncio.Task] = None
        self._api_thread: Optional[threading.Thread] = None
        self._mounted = False

    # ── 挂载 / 卸载 ───────────────────────────────────────────────────

    async def mount(self, api_port: int = 18000) -> None:
        """挂载评测系统到当前 Agent 进程。

        启动：
        1. Ingest 消费者（asyncio Task，同事件循环）
        2. FastAPI 看板（独立线程，不阻塞主循环）

        Args:
            api_port: FastAPI 监听的端口，默认 8000
        """
        if self._mounted:
            logger.warning("EvalService 已挂载，跳过重复 mount")
            return

        # 1) 启动 Ingest 消费者
        self._ingest_task = asyncio.create_task(self._ingest_worker.start())
        logger.info("Ingest 消费者已启动（asyncio Task）")

        # 2) 启动 FastAPI 看板（独立线程）
        self._api_thread = threading.Thread(
            target=_run_fastapi,
            args=(api_port,),
            daemon=True,
            name="eval-fastapi",
        )
        self._api_thread.start()
        logger.info("FastAPI 看板已启动（独立线程，端口 %d）", api_port)

        self._mounted = True
        logger.info("EvalService 挂载完成，启用层: %s", self.enabled_layers)

    async def unmount(self, timeout: float = 5.0) -> None:
        """卸载评测系统，优雅释放所有资源。

        Args:
            timeout: 等待各组件退出的超时时间（秒）
        """
        if not self._mounted:
            return

        logger.info("正在卸载 EvalService...")

        # 1) 停止 Ingest 消费者
        if self._ingest_task and not self._ingest_task.done():
            await self._ingest_worker.stop()
            self._ingest_task.cancel()
            try:
                await asyncio.wait_for(
                    asyncio.shield(self._ingest_task), timeout=timeout
                )
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
            logger.info("Ingest 消费者已停止")

        # 2) 停止 FastAPI 线程
        if self._api_thread and self._api_thread.is_alive():
            self._api_thread.join(timeout=timeout)
            logger.info("FastAPI 看板线程已退出")

        # 3) 关闭数据库连接
        engine = self._session_factory.kw.get("bind")
        if engine:
            await engine.dispose()
            logger.info("数据库连接已关闭")

        self._mounted = False
        logger.info("EvalService 已安全卸载")

    @property
    def is_mounted(self) -> bool:
        """是否已挂载。"""
        return self._mounted


# ── FastAPI 独立线程运行器 ──────────────────────────────────────────

def _run_fastapi(port: int) -> None:
    """在独立线程中启动 FastAPI 服务。"""
    uvicorn.run(
        fastapi_app,
        host="0.0.0.0",
        port=port,
        log_level="info",
        access_log=False,
    )
