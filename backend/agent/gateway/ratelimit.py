"""基于滑动窗口的速率限制器。

每个用户独立计数，60 秒滑动窗口。
纯内存实现，进程重启后重置。
"""

from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Dict


class RateLimiter:
    """滑动窗口速率限制器。

    Usage::

        limiter = RateLimiter(max_per_minute=30)
        if await limiter.check(user_id):
            # 放行
        else:
            # 返回速率限制提示
    """

    def __init__(self, max_per_minute: int = 30) -> None:
        self._max = max_per_minute
        self._buckets: Dict[str, deque] = {}

    async def check(self, user_id: str) -> bool:
        """检查用户是否允许发送消息。

        Returns:
            ``True`` 表示允许，``False`` 表示已超限。
        """
        now = datetime.now(timezone.utc)
        if user_id not in self._buckets:
            self._buckets[user_id] = deque()

        bucket = self._buckets[user_id]
        # 清理 60 秒前的记录
        while bucket and bucket[0] < now - timedelta(seconds=60):
            bucket.popleft()

        if len(bucket) >= self._max:
            return False

        bucket.append(now)
        return True
