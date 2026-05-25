"""请求统计 —— 请求计数、延迟追踪、日志缓冲"""

from __future__ import annotations

import time
import threading
from collections import deque
from dataclasses import dataclass, field


@dataclass
class RequestLog:
    timestamp: float
    model: str
    endpoint: str
    status_code: int
    elapsed_ms: float
    tokens: int = 0
    error: str = ""
    stream: bool = False
    provider: str = ""
    target_model: str = ""

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "time": time.strftime("%H:%M:%S", time.localtime(self.timestamp)),
            "model": self.model,
            "endpoint": self.endpoint,
            "status_code": self.status_code,
            "elapsed_ms": round(self.elapsed_ms, 1),
            "tokens": self.tokens,
            "error": self.error,
            "stream": self.stream,
            "provider": self.provider,
            "target_model": self.target_model,
        }


class StatsCollector:
    """请求统计收集器（线程安全）"""

    def __init__(self, max_logs: int = 500):
        self._lock = threading.RLock()
        self._start_time = time.time()
        self._request_count = 0
        self._success_count = 0
        self._error_count = 0
        self._total_latency_ms = 0.0
        self._logs: deque[RequestLog] = deque(maxlen=max_logs)
        self._log_listeners: list[callable] = []  # WebSocket 推送回调

    @property
    def uptime_seconds(self) -> float:
        return time.time() - self._start_time

    def add_listener(self, callback):
        """注册日志监听器（用于 WebSocket 推送）"""
        self._log_listeners.append(callback)

    def remove_listener(self, callback):
        if callback in self._log_listeners:
            self._log_listeners.remove(callback)

    def record(self, log: RequestLog):
        with self._lock:
            self._request_count += 1
            if log.status_code < 400:
                self._success_count += 1
            else:
                self._error_count += 1
            self._total_latency_ms += log.elapsed_ms
            self._logs.appendleft(log)

        # 通知监听器
        d = log.to_dict()
        for cb in self._log_listeners:
            try:
                cb(d)
            except Exception:
                pass

    def get_summary(self) -> dict:
        with self._lock:
            avg_latency = (self._total_latency_ms / self._request_count) if self._request_count > 0 else 0
            return {
                "uptime_seconds": round(self.uptime_seconds, 0),
                "request_count": self._request_count,
                "success_count": self._success_count,
                "error_count": self._error_count,
                "avg_latency_ms": round(avg_latency, 1),
            }

    def get_recent_logs(self, limit: int = 100) -> list[dict]:
        with self._lock:
            return [log.to_dict() for log in list(self._logs)[:limit]]

    def clear_logs(self):
        with self._lock:
            self._logs.clear()
            self._request_count = 0
            self._success_count = 0
            self._error_count = 0
            self._total_latency_ms = 0.0

    # ── 详细日志（完整请求/响应内容）─────────────────────────────

    def __init_detail(self):
        """lazy init for detailed log fields"""
        if not hasattr(self, '_detailed_logs'):
            self._detailed_logs: deque[dict] = deque(maxlen=100)
            self._detailed_enabled: bool = False

    @property
    def detailed_enabled(self) -> bool:
        self.__init_detail()
        return self._detailed_enabled

    def set_detailed_enabled(self, enabled: bool):
        self.__init_detail()
        self._detailed_enabled = enabled

    def record_detailed_log(self, entry: dict):
        self.__init_detail()
        with self._lock:
            self._detailed_logs.appendleft(entry)
        for cb in self._log_listeners:
            try:
                cb({"type": "detailed", "entry": entry})
            except Exception:
                pass

    def get_detailed_logs(self) -> list[dict]:
        self.__init_detail()
        with self._lock:
            return list(self._detailed_logs)

    def clear_detailed_logs(self):
        self.__init_detail()
        with self._lock:
            self._detailed_logs.clear()


# 全局单例
_stats: StatsCollector | None = None


def get_stats() -> StatsCollector:
    global _stats
    if _stats is None:
        _stats = StatsCollector()
    return _stats
