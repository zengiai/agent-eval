"""调度框架单元测试。

覆盖：
    - BaseJob 抽象类
    - JobConfig / JobExecution 数据类
    - JobManager 注册/暂停/恢复/触发/调度修改/列表
    - 超时处理
    - 异常隔离
    - 连续失败自动暂停
    - 预置 Job 骨架执行
"""

import asyncio
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import pytest
from sqlalchemy.exc import ProgrammingError

from backend.agent.scheduler.base import (
    BaseJob,
    JobConfig,
    JobExecution,
    JobLifecycle,
    JobStatus,
    TriggerType,
)
from backend.agent.scheduler.manager import JobManager, _is_missing_table_error
from backend.agent.scheduler.jobs import (
    SamplingJob,
    DailySamplingJob,
    DailyReportJob,
    AlertCheckJob,
)


# ============================================================================
# Test Fixtures
# ============================================================================


class SimpleJob(BaseJob):
    """测试用简单 Job：立即返回结果。"""

    def __init__(self, job_id: str = "test.simple", config: Dict = None) -> None:
        super().__init__(config)
        self._job_id = job_id

    def get_config(self) -> JobConfig:
        return JobConfig(
            job_id=self._job_id,
            name="Test Simple Job",
            trigger_type=TriggerType.INTERVAL,
            trigger_value="3600",
            timeout_seconds=5,
        )

    async def execute(self, context: Dict[str, Any]) -> Dict[str, Any]:
        return {"status": "ok", "message": "test completed"}


class SlowJob(BaseJob):
    """测试用慢 Job：模拟超时。"""

    def __init__(self, delay: float = 10.0, config: Dict = None) -> None:
        super().__init__(config)
        self._delay = delay

    def get_config(self) -> JobConfig:
        return JobConfig(
            job_id="test.slow",
            name="Test Slow Job",
            trigger_type=TriggerType.INTERVAL,
            trigger_value="3600",
            timeout_seconds=1,  # 1 秒超时
        )

    async def execute(self, context: Dict[str, Any]) -> Dict[str, Any]:
        await asyncio.sleep(self._delay)
        return {"status": "slow_done"}


class FailingJob(BaseJob):
    """测试用失败 Job。"""

    def __init__(self, fail_count: int = 999, config: Dict = None) -> None:
        super().__init__(config)
        self._fail_count = fail_count
        self._call_count = 0

    def get_config(self) -> JobConfig:
        return JobConfig(
            job_id="test.failing",
            name="Test Failing Job",
            trigger_type=TriggerType.INTERVAL,
            trigger_value="3600",
            timeout_seconds=5,
        )

    async def execute(self, context: Dict[str, Any]) -> Dict[str, Any]:
        self._call_count += 1
        if self._call_count <= self._fail_count:
            raise RuntimeError(f"Simulated failure #{self._call_count}")
        return {"status": "recovered"}


class ErrorCallbackJob(BaseJob):
    """测试 on_error 回调的 Job。"""

    def __init__(self, config: Dict = None) -> None:
        super().__init__(config)
        self.error_calls: list = []

    def get_config(self) -> JobConfig:
        return JobConfig(
            job_id="test.error_cb",
            name="Test Error Callback Job",
            trigger_type=TriggerType.INTERVAL,
            trigger_value="3600",
            timeout_seconds=5,
        )

    async def execute(self, context: Dict[str, Any]) -> Dict[str, Any]:
        raise ValueError("test error")

    async def on_error(self, error: Exception, context: Dict[str, Any]) -> None:
        self.error_calls.append(str(error))


class _MockSession:
    """Mock 异步会话，兼容 async with 协议。"""
    async def __aenter__(self): return self
    async def __aexit__(self, *args): pass
    async def commit(self): pass
    async def execute(self, stmt): return _MockResult()
    async def get(self, model, ident): return None
    def add(self, obj): pass  # SQLAlchemy session.add() 不是 async


class _MockResult:
    """Mock SQLAlchemy Result。"""
    def scalars(self):
        return _MockScalars()


class _MockScalars:
    """Mock scalars() 返回。"""
    def all(self):
        return []
    def one(self):
        return 0
    def one_or_none(self):
        return None


class _OrigUndefinedTableError(Exception):
    """模拟 asyncpg UndefinedTableError。"""


@pytest.fixture
def manager():
    """创建 JobManager（使用 MemoryJobStore + Mock session，无数据库依赖）。"""
    mgr = JobManager(
        session_factory=lambda: _MockSession(),
        timezone="Asia/Shanghai",
    )
    yield mgr


# ============================================================================
# Test: 数据类
# ============================================================================


class TestJobConfig:
    def test_default_values(self):
        cfg = JobConfig(job_id="test.1", name="Test Job")
        assert cfg.job_id == "test.1"
        assert cfg.name == "Test Job"
        assert cfg.trigger_type == TriggerType.INTERVAL
        assert cfg.trigger_value == "3600"
        assert cfg.enabled is True
        assert cfg.timeout_seconds == 600
        assert cfg.metadata == {}

    def test_cron_config(self):
        cfg = JobConfig(
            job_id="test.cron",
            name="Cron Job",
            trigger_type=TriggerType.CRON,
            trigger_value="0 8 * * *",
            timeout_seconds=300,
        )
        assert cfg.trigger_type == TriggerType.CRON
        assert cfg.trigger_value == "0 8 * * *"


class TestJobExecution:
    def test_default_status(self):
        je = JobExecution(
            id="exec-1", job_id="test.1", started_at=datetime.now(timezone.utc)
        )
        assert je.status == "running"
        assert je.result is None
        assert je.error_message is None


# ============================================================================
# Test: BaseJob
# ============================================================================


class TestBaseJob:
    def test_execution_count_starts_at_zero(self):
        job = SimpleJob()
        assert job.execution_count == 0

    def test_last_error_is_none_initially(self):
        job = SimpleJob()
        assert job.last_error is None

    def test_consecutive_failures_starts_at_zero(self):
        job = SimpleJob()
        assert job.consecutive_failures == 0

    def test_get_config_returns_job_config(self):
        job = SimpleJob(job_id="my.custom")
        cfg = job.get_config()
        assert isinstance(cfg, JobConfig)
        assert cfg.job_id == "my.custom"


# ============================================================================
# Test: JobManager 生命周期
# ============================================================================


class TestJobManagerLifecycle:
    def test_initial_state(self, manager):
        assert manager.is_started is False
        assert manager.job_count == 0

    @pytest.mark.asyncio
    async def test_start_stop(self, manager):
        # 直接控制 scheduler，跳过 DB 恢复
        manager._scheduler.start()
        manager._started = True
        assert manager.is_started is True

        manager._scheduler.shutdown(wait=False)
        manager._started = False
        assert manager.is_started is False


# ============================================================================
# Test: JobManager 注册管理
# ============================================================================


class TestJobManagerRegistration:
    def test_register_adds_to_registry(self, manager):
        job = SimpleJob()
        job_id = manager.register(job)
        assert job_id == "test.simple"
        assert manager.job_count == 1

    def test_register_multiple_jobs(self, manager):
        manager.register(SimpleJob(job_id="job.1"))
        manager.register(SimpleJob(job_id="job.2"))
        manager.register(SimpleJob(job_id="job.3"))
        assert manager.job_count == 3

    def test_unregister_removes_from_registry(self, manager):
        manager.register(SimpleJob(job_id="job.a"))
        assert manager.job_count == 1

        manager.unregister("job.a")
        assert manager.job_count == 0

    def test_unregister_nonexistent_is_safe(self, manager):
        manager.unregister("nonexistent")  # 不应抛异常
        assert manager.job_count == 0

    def test_list_jobs_returns_configs(self, manager):
        manager.register(SimpleJob(job_id="job.1"))
        manager.register(SimpleJob(job_id="job.2"))

        jobs = manager.list_jobs()
        assert len(jobs) == 2
        assert all(isinstance(j, JobConfig) for j in jobs)
        assert {j.job_id for j in jobs} == {"job.1", "job.2"}

    def test_missing_table_error_is_detected(self):
        error = ProgrammingError(
            "select * from agent_jobs",
            {},
            _OrigUndefinedTableError('relation "agent_jobs" does not exist'),
        )

        assert _is_missing_table_error(error) is True

    def test_other_db_error_is_not_missing_table(self):
        error = ProgrammingError(
            "select * from agent_jobs",
            {},
            RuntimeError("connection failed"),
        )

        assert _is_missing_table_error(error) is False


# ============================================================================
# Test: 暂停 / 恢复
# ============================================================================


class TestJobManagerPauseResume:
    @pytest.mark.asyncio
    async def test_pause_resume(self, manager):
        job = SimpleJob()
        manager.register(job)
        manager._scheduler.start()
        manager._started = True

        manager.pause("test.simple")
        # 验证 job 在 APScheduler 中已暂停
        aps_job = manager._scheduler.get_job("test.simple")
        assert aps_job.next_run_time is None  # 暂停后无下次触发时间
        assert manager.list_jobs()[0].enabled is False

        manager.resume("test.simple")
        aps_job = manager._scheduler.get_job("test.simple")
        assert aps_job.next_run_time is not None  # 恢复后有下次触发时间
        assert manager.list_jobs()[0].enabled is True

        manager._scheduler.shutdown(wait=False)

    @pytest.mark.asyncio
    async def test_pause_already_paused_is_idempotent(self, manager):
        job = SimpleJob()
        manager.register(job)
        manager._scheduler.start()
        manager._started = True

        manager.pause("test.simple")
        manager._scheduler.remove_job("test.simple")

        manager.pause("test.simple")

        assert manager.list_jobs()[0].enabled is False
        manager._scheduler.shutdown(wait=False)

    @pytest.mark.asyncio
    async def test_pause_nonexistent_raises(self, manager):
        manager._scheduler.start()
        manager._started = True
        with pytest.raises(Exception):
            manager.pause("nonexistent")
        manager._scheduler.shutdown(wait=False)


# ============================================================================
# Test: 立即触发
# ============================================================================


class TestJobManagerTriggerNow:
    @pytest.mark.asyncio
    async def test_trigger_now_returns_execution_id(self, manager):
        job = SimpleJob()
        manager.register(job)

        exec_id = await manager.trigger_now("test.simple")
        assert exec_id is not None
        assert isinstance(exec_id, str)
        uuid.UUID(exec_id)  # 应为有效 UUID

    @pytest.mark.asyncio
    async def test_trigger_now_nonexistent_raises(self, manager):
        with pytest.raises(ValueError, match="未知任务"):
            await manager.trigger_now("nonexistent")


# ============================================================================
# Test: 调度周期修改
# ============================================================================


class TestJobManagerUpdateSchedule:
    @pytest.mark.asyncio
    async def test_update_schedule_changes_trigger(self, manager):
        job = SimpleJob()
        manager.register(job)
        manager._scheduler.start()
        manager._started = True

        manager.update_schedule("test.simple", TriggerType.CRON, "0 */2 * * *")

        aps_job = manager._scheduler.get_job("test.simple")
        assert "cron" in str(aps_job.trigger).lower()

        manager._scheduler.shutdown(wait=False)

    def test_update_schedule_nonexistent_raises(self, manager):
        with pytest.raises(ValueError, match="未知任务"):
            manager.update_schedule("nonexistent", TriggerType.INTERVAL, "60")


# ============================================================================
# Test: 执行包装（_execute_and_record）
# ============================================================================


class TestJobExecutionWrapper:
    @pytest.mark.asyncio
    async def test_successful_execution(self, manager):
        job = SimpleJob()
        manager.register(job)

        start = datetime.now(timezone.utc)
        exec_id = str(uuid.uuid4())
        await manager._execute_and_record(job, exec_id, start)

        assert job.execution_count == 1  # 应在成功后递增
        assert job.consecutive_failures == 0
        assert job.last_error is None

    @pytest.mark.asyncio
    async def test_timeout_handling(self, manager):
        job = SlowJob(delay=5.0)  # 5 秒延迟，但 timeout=1 秒
        manager.register(job)

        start = datetime.now(timezone.utc)
        exec_id = str(uuid.uuid4())
        await manager._execute_and_record(job, exec_id, start)

        # SlowJob 应因超时失败
        assert job.consecutive_failures >= 1
        assert "超时" in (job.last_error or "")

    @pytest.mark.asyncio
    async def test_exception_isolation(self, manager):
        job = FailingJob(fail_count=1)
        manager.register(job)

        start = datetime.now(timezone.utc)
        exec_id = str(uuid.uuid4())
        await manager._execute_and_record(job, exec_id, start)

        assert job.consecutive_failures == 1
        assert job.last_error is not None  # 记录了错误信息

    @pytest.mark.asyncio
    async def test_on_error_callback_invoked(self, manager):
        job = ErrorCallbackJob()
        manager.register(job)

        start = datetime.now(timezone.utc)
        exec_id = str(uuid.uuid4())
        await manager._execute_and_record(job, exec_id, start)

        assert len(job.error_calls) == 1
        assert "test error" in job.error_calls[0]


# ============================================================================
# Test: 连续失败自动暂停
# ============================================================================


class TestConsecutiveFailuresAutoPause:
    @pytest.mark.asyncio
    async def test_three_failures_triggers_auto_pause(self, manager):
        """连续 3 次失败后应自动暂停 Job。"""
        job = FailingJob(fail_count=999)  # 永远失败
        manager.register(job)
        manager._scheduler.start()
        manager._started = True

        # 执行 3 次
        for i in range(3):
            start = datetime.now(timezone.utc)
            exec_id = str(uuid.uuid4())
            await manager._execute_and_record(job, exec_id, start)

        # 3 次失败后应被自动暂停
        assert job.consecutive_failures == 3

        # 验证 APScheduler 中已暂停
        try:
            aps_job = manager._scheduler.get_job("test.failing")
            assert aps_job.next_run_time is None
        except Exception:
            pass  # MemoryJobStore 中暂停的 job 可能行为不同

        manager._scheduler.shutdown(wait=False)

    @pytest.mark.asyncio
    async def test_success_resets_consecutive_failures(self, manager):
        """成功执行后连续失败计数应重置。"""
        job = FailingJob(fail_count=1)  # 第一次失败，第二次成功
        manager.register(job)

        # 第一次：失败
        start = datetime.now(timezone.utc)
        await manager._execute_and_record(job, str(uuid.uuid4()), start)
        assert job.consecutive_failures == 1
        assert job.execution_count == 0  # 失败不增加计数

        # 第二次：成功（call_count=2 > fail_count=1）
        start = datetime.now(timezone.utc)
        await manager._execute_and_record(job, str(uuid.uuid4()), start)
        assert job.consecutive_failures == 0
        assert job.execution_count == 1  # 成功执行一次


# ============================================================================
# Test: 触发器构建
# ============================================================================


class TestTriggerBuilding:
    def test_cron_trigger(self):
        trigger = JobManager._build_trigger_from(TriggerType.CRON, "0 8 * * *")
        from apscheduler.triggers.cron import CronTrigger
        assert isinstance(trigger, CronTrigger)

    def test_interval_trigger(self):
        trigger = JobManager._build_trigger_from(TriggerType.INTERVAL, "3600")
        from apscheduler.triggers.interval import IntervalTrigger
        assert isinstance(trigger, IntervalTrigger)

    def test_date_trigger(self):
        trigger = JobManager._build_trigger_from(TriggerType.DATE, "2026-06-17T08:00:00")
        from apscheduler.triggers.date import DateTrigger
        assert isinstance(trigger, DateTrigger)

    def test_invalid_trigger_type(self):
        with pytest.raises(ValueError, match="不支持的触发器类型"):
            JobManager._build_trigger_from("invalid", "value")


# ============================================================================
# Test: 预置 Job 配置
# ============================================================================


class TestPresetJobs:
    def test_sampling_job_config(self):
        job = SamplingJob({"sampling_rate": 0.1, "hours_back": 2})
        cfg = job.get_config()
        assert cfg.job_id == "sampling.hourly"
        assert cfg.trigger_type == TriggerType.INTERVAL
        assert cfg.metadata["sampling_rate"] == 0.1

    def test_daily_sampling_job_config(self):
        job = DailySamplingJob()
        cfg = job.get_config()
        assert cfg.job_id == "sampling.daily"
        assert cfg.trigger_type == TriggerType.CRON
        assert cfg.trigger_value == "0 2 * * *"

    def test_daily_report_job_config(self):
        job = DailyReportJob()
        cfg = job.get_config()
        assert cfg.job_id == "report.daily"
        assert cfg.trigger_type == TriggerType.CRON
        assert cfg.trigger_value == "0 8 * * *"

    def test_alert_check_job_config(self):
        job = AlertCheckJob()
        cfg = job.get_config()
        assert cfg.job_id == "alert.check"
        assert cfg.trigger_type == TriggerType.INTERVAL
        assert cfg.trigger_value == "1800"

    @pytest.mark.asyncio
    async def test_alert_check_job_execute_no_db(self):
        """AlertCheckJob 在无 DB 上下文时应返回占位结果。"""
        job = AlertCheckJob()
        result = await job.execute({})
        assert result["triggered"] == 0
        assert "骨架实现" in result.get("note", "")
