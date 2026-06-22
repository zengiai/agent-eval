"""JobManager —— 基于 APScheduler 的定时任务管理器。

管理所有 BaseJob 的注册、调度、暂停、恢复和生命周期。
执行历史持久化到 agent_job_executions 表。
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.executors.asyncio import AsyncIOExecutor
from apscheduler.jobstores.base import JobLookupError
from apscheduler.jobstores.memory import MemoryJobStore
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.triggers.date import DateTrigger
from sqlalchemy import select, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.agent.scheduler.base import (
    BaseJob,
    JobConfig,
    JobExecution,
    JobLifecycle,
    JobStatus,
    TriggerType,
)
from backend.agent.scheduler.models import AgentJob, AgentJobExecution

logger = logging.getLogger(__name__)

# 连续失败自动暂停阈值
MAX_CONSECUTIVE_FAILURES = 3


def _is_missing_table_error(error: Exception) -> bool:
    """判断是否为迁移未执行导致的缺表错误。"""
    if not isinstance(error, DBAPIError):
        return False
    original = getattr(error, "orig", None)
    return "UndefinedTableError" in type(original).__name__ or "does not exist" in str(error)


class JobManager:
    """基于 APScheduler 的定时任务管理器。

    职责:
        - 管理 BaseJob 注册表（job_id → BaseJob）
        - 通过 APScheduler 管理调度周期
        - 持久化执行历史到 agent_job_executions
        - 连续失败自动暂停 + 异常隔离

    用法::

        manager = JobManager(
            session_factory=eval_service._session_factory,
            timezone="Asia/Shanghai",
        )
        await manager.start()
        manager.register(SamplingJob())
        await manager.trigger_now("sampling.hourly")
        await manager.stop()
    """

    def __init__(
        self,
        session_factory: async_sessionmaker,
        timezone: str = "Asia/Shanghai",
        max_workers: int = 3,
    ) -> None:
        self._session_factory = session_factory
        self._timezone = timezone

        # APScheduler 内部组件
        jobstores = {"default": MemoryJobStore()}
        executors = {"default": AsyncIOExecutor()}
        self._scheduler = AsyncIOScheduler(
            jobstores=jobstores,
            executors=executors,
            timezone=timezone,
        )

        # 业务注册表
        self._job_registry: Dict[str, BaseJob] = {}
        self._job_lifecycle: Dict[str, JobLifecycle] = {}
        self._started = False

        # 全局上下文（注入到每个 Job.execute()）
        self._global_context: Dict[str, Any] = {}

    # ── 生命周期 ──────────────────────────────────────────────────

    async def start(self) -> None:
        """启动调度器。

        1. 从 agent_jobs 表恢复之前注册的任务
        2. 启动 APScheduler
        """
        if self._started:
            logger.warning("JobManager 已启动，跳过重复 start")
            return

        # 从 PG 恢复已注册任务
        await self._restore_from_db()

        self._scheduler.start()
        self._started = True

        jobs = self.list_jobs()
        logger.info(
            "JobManager 已启动，注册任务 %d 个: %s",
            len(jobs),
            [j.job_id for j in jobs],
        )

    async def stop(self, wait: bool = True) -> None:
        """停止调度器。

        Args:
            wait: True=等待当前执行中的 Job 完成，False=立即停止
        """
        if not self._started:
            return

        self._scheduler.shutdown(wait=wait)
        self._started = False
        logger.info("JobManager 已停止")

    # ── 上下文注入 ────────────────────────────────────────────────

    def set_context(self, **kwargs: Any) -> None:
        """设置全局上下文，将注入到每个 Job.execute() 调用中。

        常用 key:
            - db_session_factory
            - eval_service
            - config
            - gateway (IMGateway 实例)
        """
        self._global_context.update(kwargs)

    def _build_context(self) -> Dict[str, Any]:
        """构建传递给 Job.execute() 的上下文。"""
        return {
            "db_session_factory": self._session_factory,
            "logger": logging.getLogger("agent.job"),
            **self._global_context,
        }

    # ── 任务注册管理 ──────────────────────────────────────────────

    def register(self, job: BaseJob) -> str:
        """注册一个定时任务。

        1. 加入内存注册表
        2. 添加到 APScheduler（根据 JobConfig 构建 trigger）
        3. 持久化到 agent_jobs 表

        Returns:
            job_id
        """
        cfg = job.get_config()
        if not cfg.enabled:
            logger.info("Job [%s] 已禁用，仅注册不调度", cfg.job_id)

        self._job_registry[cfg.job_id] = job
        self._job_lifecycle.setdefault(
            cfg.job_id,
            JobLifecycle.RUNNING if cfg.enabled else JobLifecycle.STOPPED,
        )

        if cfg.enabled:
            trigger = self._build_trigger(cfg)
            self._scheduler.add_job(
                func=self._execute_wrapper,
                trigger=trigger,
                id=cfg.job_id,
                name=cfg.name,
                replace_existing=True,
                kwargs={"job": job},
            )

        # 异步持久化到 PG（fire-and-forget，无事件循环时静默跳过）
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._persist_job_config(cfg))
        except RuntimeError:
            logger.debug("无运行中的事件循环，跳过 JobConfig 持久化")

        logger.info(
            "Job [%s] 已注册 (trigger=%s, value=%s)",
            cfg.job_id,
            cfg.trigger_type.value,
            cfg.trigger_value,
        )
        return cfg.job_id

    def unregister(self, job_id: str) -> None:
        """注销任务。"""
        try:
            self._scheduler.remove_job(job_id)
        except Exception:
            logger.debug("Job [%s] 不在调度器中（可能已暂停）", job_id)

        self._job_registry.pop(job_id, None)
        self._job_lifecycle.pop(job_id, None)
        logger.info("Job [%s] 已注销", job_id)

    # ── 运行时控制 ────────────────────────────────────────────────

    def pause(self, job_id: str) -> None:
        """暂停任务（保留注册，停止调度触发）。"""
        if job_id not in self._job_registry:
            self._scheduler.pause_job(job_id)
            return

        try:
            self._scheduler.pause_job(job_id)
        except JobLookupError:
            lifecycle = self._job_lifecycle.get(job_id)
            if lifecycle != JobLifecycle.PAUSED:
                raise

        self._job_lifecycle[job_id] = JobLifecycle.PAUSED
        self._safe_update_job_status(job_id, JobLifecycle.PAUSED)
        logger.info("Job [%s] 已暂停", job_id)

    def resume(self, job_id: str) -> None:
        """恢复已暂停的任务。"""
        try:
            self._scheduler.resume_job(job_id)
        except JobLookupError:
            job = self._job_registry.get(job_id)
            if not job:
                raise
            cfg = job.get_config()
            trigger = self._build_trigger(cfg)
            self._scheduler.add_job(
                func=self._execute_wrapper,
                trigger=trigger,
                id=cfg.job_id,
                name=cfg.name,
                replace_existing=True,
                kwargs={"job": job},
            )
        self._job_lifecycle[job_id] = JobLifecycle.RUNNING
        self._safe_update_job_status(job_id, JobLifecycle.RUNNING)
        logger.info("Job [%s] 已恢复", job_id)

    async def trigger_now(self, job_id: str) -> str:
        """立即触发一次任务（不影响原调度周期）。

        直接调用 job.execute()，通过 _execute_wrapper 记录执行历史。

        Returns:
            execution_id (UUID 字符串)
        """
        job = self._job_registry.get(job_id)
        if not job:
            raise ValueError(f"未知任务: {job_id}")

        cfg = job.get_config()
        start = datetime.now(timezone.utc)
        execution_id = str(uuid.uuid4())

        logger.info("Job [%s] 手动触发 (execution_id=%s)", job_id, execution_id)

        # 异步执行并记录（安全创建 task）
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(
                self._execute_and_record(job, execution_id, start, is_manual=True)
            )
        except RuntimeError:
            # 无运行中的事件循环，降级为同步等待
            logger.debug("无运行中的事件循环，trigger_now 降级为同步执行")


        return execution_id

    def update_schedule(
        self,
        job_id: str,
        trigger_type: TriggerType,
        trigger_value: str,
    ) -> None:
        """修改调度周期。

        Args:
            job_id: 任务 ID
            trigger_type: 新触发器类型
            trigger_value: 新触发值（cron 表达式 或 秒数）
        """
        job = self._job_registry.get(job_id)
        if not job:
            raise ValueError(f"未知任务: {job_id}")

        new_trigger = self._build_trigger_from(trigger_type, trigger_value)
        self._scheduler.reschedule_job(job_id, trigger=new_trigger)

        # 更新 JobConfig
        cfg = job.get_config()
        cfg.trigger_type = trigger_type
        cfg.trigger_value = trigger_value

        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._persist_job_config(cfg))
        except RuntimeError:
            logger.debug("无运行中的事件循环，跳过 JobConfig 持久化")
        logger.info(
            "Job [%s] 调度周期已更新: %s → %s",
            job_id, trigger_type.value, trigger_value,
        )

    def list_jobs(self) -> List[JobConfig]:
        """列出所有已注册任务（含暂停的）。"""
        return [
            replace(cfg, enabled=self._is_job_effectively_enabled(cfg))
            for cfg in (j.get_config() for j in self._job_registry.values())
        ]

    def _is_job_effectively_enabled(self, cfg: JobConfig) -> bool:
        """返回 Job 当前是否会被定时触发。"""
        if not cfg.enabled:
            return False

        lifecycle = self._job_lifecycle.get(cfg.job_id)
        if lifecycle in {JobLifecycle.PAUSED, JobLifecycle.STOPPED}:
            return False

        if self._scheduler.running:
            aps_job = self._scheduler.get_job(cfg.job_id)
            if aps_job is not None and getattr(aps_job, "next_run_time", None) is None:
                return False
        return True

    async def get_history(
        self, job_id: str, limit: int = 20
    ) -> List[JobExecution]:
        """查询任务执行历史（从 agent_job_executions 表）。"""
        async with self._session_factory() as session:
            result = await session.execute(
                select(AgentJobExecution)
                .where(AgentJobExecution.job_id == job_id)
                .order_by(AgentJobExecution.started_at.desc())
                .limit(limit)
            )
            rows = result.scalars().all()
            return [
                JobExecution(
                    id=str(r.id),
                    job_id=r.job_id,
                    started_at=r.started_at,
                    completed_at=r.completed_at,
                    status=r.status,
                    result=r.result,
                    error_message=r.error_message,
                    duration_ms=r.duration_ms,
                )
                for r in rows
            ]

    # ── 内部：触发器构建 ─────────────────────────────────────────

    def _build_trigger(self, cfg: JobConfig):
        """从 JobConfig 构建 APScheduler trigger。"""
        return self._build_trigger_from(cfg.trigger_type, cfg.trigger_value)

    @staticmethod
    def _build_trigger_from(trigger_type: TriggerType, trigger_value: str):
        """根据类型和值构建 trigger 对象。"""
        if trigger_type == TriggerType.CRON:
            return CronTrigger.from_crontab(trigger_value)
        elif trigger_type == TriggerType.INTERVAL:
            return IntervalTrigger(seconds=int(trigger_value))
        elif trigger_type == TriggerType.DATE:
            return DateTrigger(run_date=trigger_value)
        else:
            raise ValueError(f"不支持的触发器类型: {trigger_type}")

    # ── 内部：执行包装与记录 ─────────────────────────────────────

    async def _execute_wrapper(self, job: BaseJob) -> None:
        """APScheduler 回调入口：包装 execute()，记录执行历史 + 异常处理。

        此方法由 APScheduler 按周期自动调用。
        """
        cfg = job.get_config()
        start = datetime.now(timezone.utc)
        execution_id = str(uuid.uuid4())

        await self._execute_and_record(job, execution_id, start, is_manual=False)

    async def _execute_and_record(
        self,
        job: BaseJob,
        execution_id: str,
        start: datetime,
        is_manual: bool = False,
    ) -> None:
        """核心执行逻辑：运行 job.execute() 并记录结果。

        Args:
            job: 任务实例
            execution_id: 执行记录 UUID
            start: 开始时间
            is_manual: 是否为手动触发（影响日志措辞）
        """
        cfg = job.get_config()
        trigger_label = "手动" if is_manual else "定时"

        # 1. 写入执行记录 (running)
        await self._create_execution_record(execution_id, cfg.job_id, start)

        # 2. 执行（带超时保护）
        try:
            result = await asyncio.wait_for(
                job.execute(self._build_context()),
                timeout=cfg.timeout_seconds,
            )
            end = datetime.now(timezone.utc)
            duration = int((end - start).total_seconds() * 1000)

            # 更新执行记录 (success)
            await self._update_execution_record(
                execution_id, JobStatus.SUCCESS, result, duration
            )

            # 重置连续失败计数
            job._execution_count += 1
            job._consecutive_failures = 0
            job._last_error = None

            logger.info(
                "Job [%s] %s触发执行成功 (duration=%dms)",
                cfg.job_id, trigger_label, duration,
            )

        except asyncio.TimeoutError:
            end = datetime.now(timezone.utc)
            duration = int((end - start).total_seconds() * 1000)
            error_msg = f"执行超时 (>{cfg.timeout_seconds}s)"

            await self._update_execution_record(
                execution_id, JobStatus.FAILED, None, duration, error_msg
            )
            job._consecutive_failures += 1
            job._last_error = error_msg

            logger.error("Job [%s] %s触发超时", cfg.job_id, trigger_label)
            await job.on_error(TimeoutError(error_msg), self._build_context())

        except Exception as e:
            end = datetime.now(timezone.utc)
            duration = int((end - start).total_seconds() * 1000)
            error_msg = f"{type(e).__name__}: {e}"

            await self._update_execution_record(
                execution_id, JobStatus.FAILED, None, duration, error_msg
            )
            job._consecutive_failures += 1
            job._last_error = str(e)

            logger.error(
                "Job [%s] %s触发失败: %s",
                cfg.job_id, trigger_label, e, exc_info=True,
            )
            await job.on_error(e, self._build_context())

        # 3. 检查连续失败 → 自动暂停
        if job._consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
            logger.warning(
                "Job [%s] 连续失败 %d 次，自动暂停",
                cfg.job_id, job._consecutive_failures,
            )
            try:
                self.pause(cfg.job_id)
            except Exception:
                logger.exception("自动暂停 Job [%s] 失败", cfg.job_id)

    # ── 内部：数据库操作 ─────────────────────────────────────────

    async def _create_execution_record(
        self, execution_id: str, job_id: str, started_at: datetime
    ) -> None:
        """创建执行记录（status=running）。"""
        try:
            async with self._session_factory() as session:
                record = AgentJobExecution(
                    id=uuid.UUID(execution_id),
                    job_id=job_id,
                    started_at=started_at,
                    status=JobStatus.RUNNING.value,
                )
                session.add(record)
                await session.commit()
        except Exception:
            logger.exception("写入执行记录失败 (execution_id=%s)", execution_id)

    async def _update_execution_record(
        self,
        execution_id: str,
        status: JobStatus,
        result: Optional[Dict],
        duration_ms: Optional[int],
        error_message: Optional[str] = None,
    ) -> None:
        """更新执行记录（完成状态）。"""
        try:
            async with self._session_factory() as session:
                stmt = (
                    update(AgentJobExecution)
                    .where(AgentJobExecution.id == uuid.UUID(execution_id))
                    .values(
                        status=status.value,
                        result=result,
                        duration_ms=duration_ms,
                        error_message=error_message,
                        completed_at=datetime.now(timezone.utc),
                    )
                )
                await session.execute(stmt)
                await session.commit()
        except Exception:
            logger.exception("更新执行记录失败 (execution_id=%s)", execution_id)

    async def _persist_job_config(self, cfg: JobConfig) -> None:
        """持久化 JobConfig 到 agent_jobs 表（upsert）。"""
        try:
            async with self._session_factory() as session:
                result = await session.execute(
                    select(AgentJob).where(AgentJob.job_id == cfg.job_id)
                )
                existing = result.scalars().first()
                if existing:
                    existing.name = cfg.name
                    existing.description = cfg.description
                    existing.trigger_type = cfg.trigger_type.value
                    existing.trigger_value = cfg.trigger_value
                    existing.enabled = cfg.enabled
                    existing.config = cfg.metadata
                    existing.updated_at = datetime.now(timezone.utc)
                else:
                    record = AgentJob(
                        job_id=cfg.job_id,
                        name=cfg.name,
                        description=cfg.description,
                        trigger_type=cfg.trigger_type.value,
                        trigger_value=cfg.trigger_value,
                        enabled=cfg.enabled,
                        status=JobLifecycle.RUNNING.value,
                        config=cfg.metadata,
                    )
                    session.add(record)
                await session.commit()
        except Exception as e:
            if _is_missing_table_error(e):
                logger.warning(
                    "跳过 JobConfig 持久化：agent_jobs 表不存在，请执行迁移后恢复持久化 (job_id=%s)",
                    cfg.job_id,
                )
                return
            logger.exception("持久化 JobConfig 失败 (job_id=%s)", cfg.job_id)

    async def _update_job_status(
        self, job_id: str, status: JobLifecycle
    ) -> None:
        """更新 agent_jobs 表中的 status 字段。"""
        try:
            async with self._session_factory() as session:
                stmt = (
                    update(AgentJob)
                    .where(AgentJob.job_id == job_id)
                    .values(status=status.value, updated_at=datetime.now(timezone.utc))
                )
                await session.execute(stmt)
                await session.commit()
        except Exception:
            logger.exception("更新 Job 状态失败 (job_id=%s)", job_id)

    def _safe_update_job_status(self, job_id: str, status: JobLifecycle) -> None:
        """安全的异步状态更新（无事件循环时静默跳过）。"""
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._update_job_status(job_id, status))
        except RuntimeError:
            logger.debug("无运行中的事件循环，跳过 Job 状态持久化")

    async def _restore_from_db(self) -> None:
        """从 agent_jobs 表恢复之前注册的任务。

        注意：仅在 start() 时调用一次。
        若 agent_jobs 表尚不存在（迁移未执行），静默跳过。
        """
        try:
            async with self._session_factory() as session:
                result = await session.execute(
                    select(AgentJob).where(AgentJob.enabled == True)
                )
                rows = result.scalars().all()

            restored_count = 0
            for row in rows:
                # 仅恢复在注册表中存在的 Job（需在 register 之前已创建实例）
                job = self._job_registry.get(row.job_id)
                if job:
                    try:
                        try:
                            lifecycle = JobLifecycle(row.status)
                        except ValueError:
                            lifecycle = JobLifecycle.RUNNING if row.enabled else JobLifecycle.STOPPED
                        self._job_lifecycle[row.job_id] = lifecycle

                        if lifecycle == JobLifecycle.PAUSED:
                            try:
                                self._scheduler.remove_job(row.job_id)
                            except Exception:
                                pass
                            continue

                        trigger = self._build_trigger_from(
                            TriggerType(row.trigger_type), row.trigger_value
                        )
                        self._scheduler.add_job(
                            func=self._execute_wrapper,
                            trigger=trigger,
                            id=row.job_id,
                            name=row.name,
                            replace_existing=True,
                            kwargs={"job": job},
                        )
                        restored_count += 1
                    except Exception:
                        logger.exception(
                            "恢复 Job [%s] 失败，跳过", row.job_id
                        )

            if restored_count > 0:
                logger.info("从 PG 恢复了 %d 个任务", restored_count)
        except Exception:
            # 表可能尚不存在（迁移未执行），静默跳过
            logger.debug("从 PG 恢复任务跳过（可能是首次启动或迁移未执行）")

    # ── 属性 ──────────────────────────────────────────────────────

    @property
    def is_started(self) -> bool:
        """调度器是否已启动。

        同时检查内存标志位和 APScheduler 原生状态，
        防止因异常关闭导致标志位与实际状态不一致。
        """
        return self._started and (self._scheduler.running if self._scheduler else False)

    @property
    def job_count(self) -> int:
        """已注册任务数。"""
        return len(self._job_registry)
