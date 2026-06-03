"""熔断器 + 健康评分 + 主动健康探测 —— 防止上游 API 抖动导致对话崩溃

ccx 风格的三态熔断器，升级为滑动窗口错误率:
- CLOSED: 正常通行，滑动窗口内错误率超阈值 → OPEN
- OPEN: 快速失败，冷却期后 → HALF_OPEN
- HALF_OPEN: 放行一个探测请求，成功 → CLOSED，失败 → OPEN

v2.1 升级:
- 滑动窗口错误率替代简单连续失败计数
- 主动健康探测 (每 30s ping /v1/models)
- 最小请求阈值防止低流量误熔断
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from enum import Enum

import httpx

logger = logging.getLogger("code-cn-bridge")


class CircuitState(Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    """单个上游 provider 的熔断器 —— 滑动窗口错误率版本"""

    def __init__(
        self,
        name: str,
        failure_threshold: int = 3,       # 兼容旧接口，不再作为主要判定
        cooldown_seconds: float = 30.0,
        half_open_max_requests: int = 1,
        error_rate_threshold: float = 0.5,  # 滑动窗口错误率阈值 (默认 50%)
        window_seconds: float = 300.0,       # 滑动窗口大小 (默认 5 分钟)
        min_requests: int = 5,               # 最小请求数，低于此数不触发熔断
    ):
        self.name = name
        self.failure_threshold = failure_threshold
        self.cooldown_seconds = cooldown_seconds
        self.half_open_max_requests = half_open_max_requests
        self.error_rate_threshold = error_rate_threshold
        self.window_seconds = window_seconds
        self.min_requests = min_requests

        self.state = CircuitState.CLOSED
        self.failure_count = 0
        self.success_count = 0
        self.last_failure_time = 0.0
        self.last_success_time = 0.0
        self._half_open_requests = 0
        self._lock = threading.RLock()

        # 滑动窗口: [(timestamp, is_success), ...]
        self._window: list[tuple[float, bool]] = []

        # 主动健康探测状态
        self._probe_healthy: bool | None = None  # None=未探测, True=健康, False=不健康
        self._last_probe_time: float = 0.0

    # ── 滑动窗口 ─────────────────────────────────────────────────

    def _prune_window(self) -> None:
        """清理窗口外的旧记录"""
        cutoff = time.time() - self.window_seconds
        self._window = [(ts, ok) for ts, ok in self._window if ts > cutoff]

    def _window_error_rate(self) -> tuple[float, int, int]:
        """返回 (错误率, 窗口内总请求数, 窗口内失败数)"""
        self._prune_window()
        total = len(self._window)
        if total == 0:
            return 0.0, 0, 0
        failures = sum(1 for _, ok in self._window if not ok)
        return failures / total, total, failures

    # ── 健康评分 ─────────────────────────────────────────────────

    @property
    def health_score(self) -> int:
        """0-100 健康评分，综合考虑滑动窗口和主动探测"""
        with self._lock:
            error_rate, total, failures = self._window_error_rate()

            # 基础分: 100 - 错误率*100
            score = int((1.0 - error_rate) * 100)

            # 主动探测修正
            if self._probe_healthy is False:
                score = max(0, score - 30)  # 主动探测失败严重扣分
            elif self._probe_healthy is True:
                score = min(100, score + 10)  # 主动探测成功小幅加分

            # 低流量时不扣分
            if total < self.min_requests:
                score = max(score, 90)

            return max(0, min(100, score))

    # ── 熔断判定 ─────────────────────────────────────────────────

    def before_request(self) -> bool:
        """请求前检查，返回 True 表示放行"""
        with self._lock:
            now = time.time()

            if self.state == CircuitState.CLOSED:
                # 检查滑动窗口错误率是否触发熔断
                error_rate, total, failures = self._window_error_rate()
                if (
                    total >= self.min_requests
                    and error_rate >= self.error_rate_threshold
                ):
                    self.state = CircuitState.OPEN
                    self.last_failure_time = now
                    logger.warning(
                        "熔断器 %s: CLOSED → OPEN (滑动窗口错误率 %.1f%%, %d/%d 失败)",
                        self.name, error_rate * 100, failures, total,
                    )
                    return False
                # 主动探测不健康也触发熔断
                if self._probe_healthy is False and now - self._last_probe_time < self.cooldown_seconds:
                    self.state = CircuitState.OPEN
                    self.last_failure_time = now
                    logger.warning(
                        "熔断器 %s: CLOSED → OPEN (主动探测失败)",
                        self.name,
                    )
                    return False
                return True

            if self.state == CircuitState.OPEN:
                if now - self.last_failure_time >= self.cooldown_seconds:
                    self.state = CircuitState.HALF_OPEN
                    self._half_open_requests = 0
                    logger.info(
                        "熔断器 %s: OPEN → HALF_OPEN (冷却 %.1fs, 健康评分 %d)",
                        self.name, now - self.last_failure_time, self.health_score,
                    )
                    return True
                logger.warning(
                    "熔断器 %s 已断开 (剩余冷却 %.1fs)，请求快速失败",
                    self.name,
                    self.cooldown_seconds - (now - self.last_failure_time),
                )
                return False

            # HALF_OPEN
            if self._half_open_requests < self.half_open_max_requests:
                self._half_open_requests += 1
                return True
            logger.warning(
                "熔断器 %s HALF_OPEN 探测请求已达上限，拒绝额外请求", self.name
            )
            return False

    def on_success(self) -> None:
        with self._lock:
            self.success_count += 1
            self.last_success_time = time.time()
            self._window.append((time.time(), True))
            self._prune_window()

            if self.state == CircuitState.HALF_OPEN:
                self.state = CircuitState.CLOSED
                self.failure_count = 0
                logger.info(
                    "熔断器 %s: HALF_OPEN → CLOSED (探测成功，健康评分 %d)",
                    self.name, self.health_score,
                )
            elif self.failure_count > 0:
                self.failure_count = max(0, self.failure_count - 1)

    def on_failure(self) -> None:
        with self._lock:
            self.failure_count += 1
            self.last_failure_time = time.time()
            self._window.append((time.time(), False))
            self._prune_window()

            if self.state == CircuitState.CLOSED:
                error_rate, total, failures = self._window_error_rate()
                if total >= self.min_requests and error_rate >= self.error_rate_threshold:
                    self.state = CircuitState.OPEN
                    logger.warning(
                        "熔断器 %s: CLOSED → OPEN (滑动窗口错误率 %.1f%%, %d/%d)",
                        self.name, error_rate * 100, failures, total,
                    )
            elif self.state == CircuitState.HALF_OPEN:
                self.state = CircuitState.OPEN
                logger.warning(
                    "熔断器 %s: HALF_OPEN → OPEN (探测失败)",
                    self.name,
                )

    # ── 主动探测 API ─────────────────────────────────────────────

    def record_probe_result(self, healthy: bool) -> None:
        """记录主动健康探测结果"""
        with self._lock:
            self._probe_healthy = healthy
            self._last_probe_time = time.time()
            if healthy:
                logger.debug("健康探测 %s: OK", self.name)
            else:
                logger.warning("健康探测 %s: FAIL", self.name)

    @property
    def probe_healthy(self) -> bool | None:
        return self._probe_healthy

    @property
    def window_stats(self) -> dict:
        """返回滑动窗口统计信息，供管理 API 查询"""
        with self._lock:
            error_rate, total, failures = self._window_error_rate()
            return {
                "error_rate": round(error_rate, 4),
                "total_requests": total,
                "failures": failures,
                "window_seconds": self.window_seconds,
                "min_requests": self.min_requests,
                "probe_healthy": self._probe_healthy,
                "last_probe_time": self._last_probe_time,
            }


class CircuitBreakerRegistry:
    """全局熔断器注册表，按 provider 名索引"""

    def __init__(self):
        self._breakers: dict[str, CircuitBreaker] = {}
        self._lock = threading.RLock()

    def get(self, name: str) -> CircuitBreaker:
        with self._lock:
            if name not in self._breakers:
                self._breakers[name] = CircuitBreaker(name=name)
            return self._breakers[name]

    def get_all(self) -> dict[str, CircuitBreaker]:
        with self._lock:
            return dict(self._breakers)

    def reset(self, name: str) -> None:
        with self._lock:
            if name in self._breakers:
                self._breakers[name] = CircuitBreaker(name=name)
                logger.info("熔断器 %s 已手动重置", name)


_registry: CircuitBreakerRegistry | None = None


def get_circuit_breaker_registry() -> CircuitBreakerRegistry:
    global _registry
    if _registry is None:
        _registry = CircuitBreakerRegistry()
    return _registry


# ═══════════════════════════════════════════════════════════════════
# 主动健康探测 — 后台异步任务，定期检查上游 API 可用性
# ═══════════════════════════════════════════════════════════════════

class HealthProber:
    """定期探测上游 provider 的 /v1/models 端点，提前发现故障"""

    def __init__(self, probe_interval: float = 30.0):
        self.probe_interval = probe_interval
        self._tasks: dict[str, asyncio.Task] = {}
        self._running = False

    async def start(self) -> None:
        """启动所有已配置 provider 的健康探测"""
        if self._running:
            return
        self._running = True

        from .config import get_config
        cfg = get_config()
        registry = get_circuit_breaker_registry()

        for provider_name, provider_cfg in cfg.providers.items():
            if not provider_cfg.get("enabled", True):
                continue

            adapter_name = provider_cfg.get("adapter", provider_name)
            base_url = provider_cfg.get("base_url", "")
            api_keys = cfg.get_api_keys(provider_name)

            if not base_url or not api_keys or not api_keys[0]:
                logger.debug("跳过 %s 健康探测: 缺少 base_url 或 api_key", provider_name)
                continue

            breaker = registry.get(provider_name)
            task = asyncio.create_task(
                self._probe_loop(provider_name, base_url, api_keys[0], breaker)
            )
            self._tasks[provider_name] = task
            logger.info("健康探测已启动: %s (每 %.0fs)", provider_name, self.probe_interval)

    async def stop(self) -> None:
        """停止所有健康探测任务"""
        self._running = False
        for name, task in self._tasks.items():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._tasks.clear()
        logger.info("所有健康探测已停止")

    async def _probe_loop(
        self, provider_name: str, base_url: str, api_key: str, breaker: CircuitBreaker
    ) -> None:
        """单个 provider 的探测循环"""
        while self._running:
            try:
                await asyncio.sleep(self.probe_interval)
                healthy = await self._ping_provider(provider_name, base_url, api_key)
                breaker.record_probe_result(healthy)
            except asyncio.CancelledError:
                break
            except Exception:
                breaker.record_probe_result(False)
                logger.debug("健康探测 %s 异常", provider_name, exc_info=True)

    async def _ping_provider(self, provider_name: str, base_url: str, api_key: str) -> bool:
        """Ping 上游 /v1/models，返回是否健康"""
        url = base_url.rstrip("/") + "/models"
        headers = {"Authorization": f"Bearer {api_key}"}
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(connect=10.0, read=10.0, write=10.0, pool=10.0),
                trust_env=False,
            ) as client:
                resp = await client.get(url, headers=headers)
                if resp.status_code < 500:
                    return True
                logger.warning("健康探测 %s: HTTP %d", provider_name, resp.status_code)
                return False
        except httpx.TimeoutException:
            logger.warning("健康探测 %s: 超时", provider_name)
            return False
        except Exception:
            logger.debug("健康探测 %s: 连接失败", provider_name, exc_info=True)
            return False


_prober: HealthProber | None = None


def get_health_prober() -> HealthProber:
    global _prober
    if _prober is None:
        _prober = HealthProber()
    return _prober
