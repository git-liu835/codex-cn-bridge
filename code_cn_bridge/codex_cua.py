"""Computer Use Agent (CUA) 代理模块

通过 stdin/stdout 启动 codex-computer-use.exe 子进程，
实现屏幕截图、鼠标/键盘控制、无障碍树读取等 Computer Use 功能。

协议: Helper Transport — newline-delimited JSON over stdin/stdout.

审批协议 (Helper Transport):
  当二进制返回 approvalRequest 时，客户端自动批准并重新发送请求，
  在 meta 中加入 x-oai-cua-approved-app 字段。

架构:
  Model (computer_use function tool)
      ↓
  Proxy (_process_computer_use_response in server.py)
      ↓
  CuaClient (this module)
      ↓  newline-delimited JSON over stdin/stdout
  codex-computer-use.exe (Rust binary)
      ↓  Win32 API
  Windows Desktop
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_logger = logging.getLogger(__name__)

# ── 常量 ────────────────────────────────────────────────────────────────
FRAME_HEADER_BYTES = 4
PIPE_PREFIX = "codex-computer-use-"
REQUEST_TIMEOUT = 30.0  # 默认 30 秒超时
SCREENSHOT_TIMEOUT = 60.0  # 截图可能较慢


# ── 数据结构 ─────────────────────────────────────────────────────────────


@dataclass
class CuaStatus:
    connected: bool = False
    pipe_name: str = ""
    request_count: int = 0
    error: str | None = None
    last_request_time: float = 0.0
    last_method: str = ""


# ── 管道发现 ─────────────────────────────────────────────────────────────


def discover_pipe() -> str | None:
    """发现活跃的 codex-computer-use 命名管道"""
    pipe_dir = r"\\.\pipe"
    try:
        pipes = os.listdir(pipe_dir)
        for p in pipes:
            if p.startswith(PIPE_PREFIX):
                full_path = rf"\\.\pipe\{p}"
                _logger.info("发现 CUA 管道: %s", full_path)
                return full_path
    except Exception as e:
        _logger.warning("列出管道失败: %s", e)
    return None


# ── 客户端 ───────────────────────────────────────────────────────────────


class CuaClient:
    """Computer Use Agent 客户端 — 通过 stdin/stdout 与 codex-computer-use.exe 通信

    审批协议 (Helper Transport):
      1. 客户端发送请求 → {"id":1, "method":"click", "params":{...}}
      2. 二进制返回审批请求 → {"id":1, "approvalRequest":{"app":"chrome.exe", ...}}
      3. 客户端重新发送请求，并在 meta 中加入已批准的应用:
         → {"id":2, "method":"click", "params":{...}, "meta":{"x-oai-cua-approved-app":"chrome.exe"}}
      4. 二进制返回实际结果 → {"id":2, "ok":true, "result":{...}}
    """

    # Helper Transport 审批 metadata key
    APPROVED_APP_META_KEY = "x-oai-cua-approved-app"

    def __init__(self):
        self._process = None  # subprocess.Popen
        self._pipe_name: str = ""
        self._request_id: int = 0
        self._pending: dict[int, threading.Event] = {}
        self._results: dict[int, Any] = {}
        self._errors: dict[int, str] = {}
        # 保存原始请求信息，用于审批后重发
        self._request_info: dict[int, dict] = {}  # id -> {method, params, meta, approved_apps, original_id}
        self._read_thread: threading.Thread | None = None
        self._running = False
        self._lock = threading.Lock()
        self.status = CuaStatus()

    # ── 连接管理 ─────────────────────────────────────────────────────

    def connect(self, pipe_name: str | None = None) -> bool:
        """启动 codex-computer-use.exe 子进程并通过 stdin/stdout 通信

        Args:
            pipe_name: 未使用（保留接口兼容），始终通过子进程 stdin/stdout 通信
        """
        if self._process is not None:
            self.close()

        # 查找 codex-computer-use.exe
        binary_path = self._find_binary()
        if binary_path is None:
            self.status.error = "未找到 codex-computer-use.exe"
            return False

        try:
            import subprocess
            import os

            parent_pid = os.getpid()
            self._process = subprocess.Popen(
                [str(binary_path), "--parent-pid", str(parent_pid)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )

            self._running = True
            self._request_id = 0
            self._pending.clear()
            self._results.clear()
            self._errors.clear()
            self._request_info.clear()

            # 启动读取线程
            self._read_thread = threading.Thread(
                target=self._read_loop, daemon=True, name="cua-reader"
            )
            self._read_thread.start()

            self.status = CuaStatus(
                connected=True,
                pipe_name=str(binary_path),
            )

            # 健康检查: list_windows
            try:
                result = self.request("list_windows", {}, timeout=10.0)
                _logger.info("CUA 连接成功 (PID=%d)，健康检查返回 %d 个窗口",
                    self._process.pid,
                    len(result) if isinstance(result, list) else 0)
            except Exception as e:
                _logger.warning("CUA 健康检查失败 (非致命): %s", e)

            _logger.info("CUA 已连接: PID=%d binary=%s", self._process.pid, binary_path)
            return True

        except Exception as e:
            self.status.error = f"启动失败: {e}"
            _logger.error("CUA 启动异常: %s", e)
            return False

    @staticmethod
    def _find_binary() -> Path | None:
        """查找 codex-computer-use.exe"""
        # 已知路径
        candidates = [
            Path.home() / "AppData" / "Local" / "OpenAI" / "Codex" / "runtimes" / "cua_node",
        ]
        for base in candidates:
            if base.exists():
                for p in base.rglob("codex-computer-use.exe"):
                    return p
        return None

    def close(self):
        """关闭连接"""
        self._running = False
        if self._process is not None:
            try:
                self._send_raw({"id": self._next_id(), "method": "close", "params": {}})
            except Exception:
                pass
            try:
                self._process.stdin.close()
            except Exception:
                pass
            try:
                self._process.terminate()
                self._process.wait(timeout=3)
            except Exception:
                try:
                    self._process.kill()
                except Exception:
                    pass
            self._process = None

        for event in self._pending.values():
            event.set()
        self._pending.clear()
        self._request_info.clear()

        self.status.connected = False
        _logger.info("CUA 已断开")

    def is_connected(self) -> bool:
        return self._process is not None and self._running and self._process.poll() is None

    def ensure_connected(self) -> bool:
        """确保连接可用，如果断开则尝试重连"""
        if self.is_connected():
            return True
        return self.connect()

    # ── 请求/响应 ────────────────────────────────────────────────────

    def request(self, method: str, params: dict, timeout: float = REQUEST_TIMEOUT,
                _meta: dict | None = None, _approved_apps: set | None = None) -> Any:
        """发送 JSON-RPC 请求并等待响应

        Args:
            method: CUA API 方法名 (list_windows, get_window_state, click, etc.)
            params: 方法参数
            timeout: 超时秒数
            _meta: 内部使用 - 请求的 meta 字段（审批重发时携带）
            _approved_apps: 内部使用 - 已批准的应用集合

        Returns:
            响应结果 (JSON 反序列化后的对象)

        Raises:
            TimeoutError: 请求超时
            RuntimeError: 连接断开或返回错误
        """
        if not self.is_connected():
            if not self.ensure_connected():
                raise RuntimeError(f"CUA 未连接: {self.status.error}")

        req_id = self._next_id()
        event = threading.Event()

        with self._lock:
            self._pending[req_id] = event
            # 保存原始请求信息，审批时需要重发
            self._request_info[req_id] = {
                "method": method,
                "params": params,
                "meta": _meta,
                "approved_apps": set(_approved_apps) if _approved_apps else set(),
            }

        # 构建请求 (stdin/stdout 协议：newline-delimited JSON)
        meta = {"x-oai-cua-request-budget-ms": int(timeout * 1000)}
        if _meta:
            meta.update(_meta)

        rpc_request = {
            "id": req_id,
            "method": method,
            "params": params,
            "meta": meta,
        }

        self.status.last_request_time = time.time()
        self.status.last_method = method
        self.status.request_count += 1

        try:
            self._send_raw(rpc_request)
        except Exception as e:
            with self._lock:
                self._pending.pop(req_id, None)
                self._request_info.pop(req_id, None)
            raise RuntimeError(f"发送请求失败: {e}")

        # 等待响应
        if not event.wait(timeout=timeout):
            with self._lock:
                self._pending.pop(req_id, None)
                self._request_info.pop(req_id, None)
            raise TimeoutError(f"CUA 请求超时 ({timeout}s): {method}")

        # 检查结果
        with self._lock:
            self._request_info.pop(req_id, None)
            if req_id in self._errors:
                error = self._errors.pop(req_id)
                raise RuntimeError(f"CUA 错误: {error}")
            if req_id in self._results:
                return self._results.pop(req_id)

        raise RuntimeError("CUA 响应异常: 无结果也无错误")

    # ── 高级 API 方法 ────────────────────────────────────────────────

    def list_windows(self) -> list[dict]:
        """列出所有可操作的窗口"""
        result = self.request("list_windows", {})
        return result if isinstance(result, list) else []

    def list_apps(self) -> list[dict]:
        """列出已安装的应用"""
        result = self.request("list_apps", {}, timeout=15.0)
        return result if isinstance(result, list) else []

    def get_window_state(self, window: dict, include_screenshot: bool = True,
                         include_text: bool = False) -> dict:
        """获取窗口状态（截图 + 无障碍树）"""
        params = {
            "window": window,
            "include_screenshot": include_screenshot,
            "include_text": include_text,
        }
        return self.request("get_window_state", params, timeout=SCREENSHOT_TIMEOUT)

    def click(self, window: dict, x: int | None = None, y: int | None = None,
              element_index: int | None = None, mouse_button: str = "left",
              click_count: int = 1, screenshot_id: str | None = None) -> None:
        """点击"""
        params: dict[str, Any] = {"window": window}
        if element_index is not None:
            params["element_index"] = element_index
        if x is not None:
            params["x"] = x
        if y is not None:
            params["y"] = y
        if mouse_button != "left":
            params["mouse_button"] = mouse_button
        if click_count > 1:
            params["click_count"] = click_count
        if screenshot_id:
            params["screenshotId"] = screenshot_id
        self.request("click", params)

    def type_text(self, window: dict, text: str) -> None:
        """输入文本"""
        self.request("type_text", {"window": window, "text": text})

    def press_key(self, window: dict, key: str) -> None:
        """按键"""
        self.request("press_key", {"window": window, "key": key})

    def scroll(self, window: dict, x: int, y: int,
               scroll_x: int = 0, scroll_y: int = 0,
               screenshot_id: str | None = None) -> None:
        """滚动"""
        params: dict[str, Any] = {
            "window": window, "x": x, "y": y,
            "scrollX": scroll_x, "scrollY": scroll_y,
        }
        if screenshot_id:
            params["screenshotId"] = screenshot_id
        self.request("scroll", params)

    def launch_app(self, app: str) -> None:
        """启动应用"""
        self.request("launch_app", {"app": app}, timeout=15.0)

    def activate_window(self, window: dict) -> None:
        """激活窗口到前台"""
        self.request("activate_window", {"window": window})

    # ── 内部方法 ─────────────────────────────────────────────────────

    def _next_id(self) -> int:
        self._request_id += 1
        return self._request_id

    def _send_raw(self, message: dict):
        """编码并发送一条 JSON 消息（换行分隔）"""
        line = json.dumps(message, ensure_ascii=False) + "\n"
        self._process.stdin.write(line.encode("utf-8"))
        self._process.stdin.flush()

    def _read_loop(self):
        """后台读取线程：逐行读取 stdout JSON 响应"""
        while self._running and self._process is not None:
            try:
                line = self._process.stdout.readline()
                if not line:
                    _logger.warning("CUA stdout EOF")
                    break

                line = line.decode("utf-8").strip()
                if not line:
                    continue

                try:
                    message = json.loads(line)
                    self._handle_message(message)
                except json.JSONDecodeError as e:
                    _logger.warning("CUA JSON 解析失败: %s (line=%s)", e, line[:100])

            except Exception as e:
                if self._running:
                    _logger.warning("CUA 读取异常: %s", e)
                break

        # 连接断开，拒绝所有等待中的请求
        self._running = False
        self.status.connected = False
        for req_id, event in list(self._pending.items()):
            with self._lock:
                self._errors[req_id] = "连接已断开"
                self._request_info.pop(req_id, None)
            event.set()
        self._request_info.clear()

    def _handle_message(self, message: dict):
        """处理收到的 JSON 消息 (Helper Transport 协议)

        响应格式:
          成功: {"id": N, "ok": true, "result": {...}}
          错误: {"id": N, "error": "message"} 或 {"id": N, "ok": false, "error": "..."}
          审批: {"id": N, "ok": false, "approvalRequest": {"app": "...", "displayName": "...", "riskLevel": "..."}}

        审批协议流程:
          1. 收到 approvalRequest 后，提取 app 名称
          2. 从 _request_info 获取原始请求的 method/params
          3. 自动批准：重新发送原始请求，在 meta 中加入 x-oai-cua-approved-app
          4. 将等待事件转移到新请求 ID
          5. 二进制返回实际结果后，释放事件
        """
        msg_id = message.get("id")
        if msg_id is None:
            _logger.debug("CUA 消息无 ID，忽略: %s", str(message)[:100])
            return

        # ── 审批请求处理 ──
        # 格式: {"id": N, "ok": false, "approvalRequest": {"app": "...", "displayName": "...", "riskLevel": "..."}}
        approval_req = message.get("approvalRequest")
        if approval_req and isinstance(approval_req, dict):
            app = approval_req.get("app", "")
            display_name = approval_req.get("displayName", app)
            risk_level = approval_req.get("riskLevel", "unknown")
            _logger.info("CUA 审批请求: app=%s displayName=%s risk=%s",
                         app, display_name, risk_level)

            if not app:
                _logger.warning("CUA 审批请求缺少 app 字段，无法自动批准")
                with self._lock:
                    event = self._pending.pop(msg_id, None)
                    self._request_info.pop(msg_id, None)
                if event:
                    self._errors[msg_id] = "审批请求缺少 app 字段"
                    event.set()
                return

            with self._lock:
                orig_info = self._request_info.get(msg_id)
                if orig_info is None:
                    _logger.warning("CUA 审批请求对应的原始请求信息不存在: id=%d", msg_id)
                    event = self._pending.pop(msg_id, None)
                    if event:
                        self._errors[msg_id] = "找不到原始请求信息用于审批重发"
                        event.set()
                    return

                # 检查该 app 是否已经在已批准列表中（防止循环）
                approved_apps = orig_info.get("approved_apps", set())
                if app in approved_apps:
                    _logger.warning("CUA app 已批准过但仍收到审批请求 (可能循环): %s", app)
                    event = self._pending.pop(msg_id, None)
                    self._request_info.pop(msg_id, None)
                    if event:
                        self._errors[msg_id] = f"app '{app}' 已批准但仍需审批 (循环)"
                        event.set()
                    return

                # 构建新请求：保留原始 method/params，增加 approved-app meta
                new_id = self._next_id()
                new_meta = {"x-oai-cua-approved-app": app}
                # 如果原始请求已有 meta，合并
                if orig_info.get("meta"):
                    new_meta = {**orig_info["meta"], **new_meta}

                new_approved_apps = approved_apps | {app}
                # 追踪原始请求 ID（用于结果映射回原始调用者）
                original_id = orig_info.get("original_id", msg_id)
                new_info = {
                    "method": orig_info["method"],
                    "params": orig_info["params"],
                    "meta": new_meta,
                    "approved_apps": new_approved_apps,
                    "original_id": original_id,
                }

                # 将事件从旧 ID 转移到新 ID
                event = self._pending.pop(msg_id, None)
                self._request_info.pop(msg_id, None)

                if event is None:
                    _logger.warning("CUA 审批时无等待中的事件: id=%d", msg_id)
                    return

                self._pending[new_id] = event
                self._request_info[new_id] = new_info

            # 发送重发请求（带 approved-app meta）
            timeout_ms = int(REQUEST_TIMEOUT * 1000)
            rpc_request = {
                "id": new_id,
                "method": new_info["method"],
                "params": new_info["params"],
                "meta": {
                    **new_meta,
                    "x-oai-cua-request-budget-ms": timeout_ms,
                },
            }

            try:
                self._send_raw(rpc_request)
                _logger.info("CUA 审批自动批准 → 重发请求 id=%d method=%s app=%s",
                             new_id, new_info["method"], app)
            except Exception as e:
                _logger.warning("CUA 审批重发失败: %s", e)
                with self._lock:
                    ev = self._pending.pop(new_id, None)
                    self._request_info.pop(new_id, None)
                if ev:
                    self._errors[new_id] = f"审批重发失败: {e}"
                    ev.set()
            return

        # ── 正常响应处理 ──
        with self._lock:
            # 检查是否为审批重发的请求（需要将结果映射回原始 ID）
            orig_info = self._request_info.pop(msg_id, None)
            result_key = msg_id
            event_for_orig = None
            if orig_info and "original_id" in orig_info:
                result_key = orig_info["original_id"]
                # 原始 ID 的事件可能已被弹出，需要检查
                event_for_orig = self._pending.pop(result_key, None)

            event = self._pending.pop(msg_id, None) or event_for_orig

        if event is None:
            _logger.warning("CUA 收到未知 ID 的响应: %d", msg_id)
            return

        # 将结果存到 result_key（可能是原始 ID，也可能是当前 ID）
        if "error" in message and message.get("error"):
            self._errors[result_key] = str(message["error"])
        elif message.get("ok") and "result" in message:
            self._results[result_key] = message["result"]
        elif "result" in message:
            self._results[result_key] = message["result"]
        else:
            # 整个消息作为结果（兼容不同响应格式）
            self._results[result_key] = message

        event.set()


# ── 统一入口 ─────────────────────────────────────────────────────────────


# 全局单例
_client: CuaClient | None = None
_client_lock = threading.Lock()


def get_client() -> CuaClient:
    """获取全局 CUA 客户端单例"""
    global _client
    with _client_lock:
        if _client is None:
            _client = CuaClient()
        return _client


def _resolve_app_path(app: str) -> str:
    """查找应用的正确 .exe 路径

    Windows 11 中很多内置应用是 UWP 应用，不在 System32 中。
    例如 mspaint.exe 在 WindowsApps 目录下。
    """
    if not app:
        return app

    # 如果路径已存在，直接返回
    if os.path.exists(app):
        return app

    # 常见应用的别名映射（Windows 11 UWP 应用）
    app_lower = app.lower()
    app_name = os.path.basename(app_lower).replace('.exe', '')

    # Windows 11 UWP 应用查找表
    uwp_app_map = {
        'mspaint': 'Microsoft.Paint',
        'paint': 'Microsoft.Paint',
        'notepad': 'Microsoft.WindowsNotepad',
        'calc': 'Microsoft.WindowsCalculator',
        'calculator': 'Microsoft.WindowsCalculator',
        'snippingtool': 'Microsoft.ScreenSketch',
        'snip': 'Microsoft.ScreenSketch',
    }

    package_name = uwp_app_map.get(app_name)
    if not package_name:
        return app  # 未知应用，返回原路径

    # 查找 UWP 应用安装路径
    try:
        import glob
        windows_apps = os.environ.get('ProgramFiles', r'C:\Program Files') + r'\WindowsApps'
        patterns = [
            f'{windows_apps}\\{package_name}_*\\PaintApp\\mspaint.exe',  # Paint 特殊结构
            f'{windows_apps}\\{package_name}_*\\{app_name}.exe',
            f'{windows_apps}\\{package_name}_*\\*.exe',
        ]
        for pattern in patterns:
            matches = glob.glob(pattern)
            if matches:
                _logger.info("UWP 应用路径解析: %s → %s", app, matches[0])
                return matches[0]
    except Exception as e:
        _logger.debug("UWP 应用路径查找失败: %s", e)

    return app


def handle_computer_use_call(action: str, params: dict) -> dict:
    """处理模型对 computer_use 工具的调用

    Args:
        action: 操作名称 (list_windows, get_window_state, click, etc.)
        params: 操作参数

    Returns:
        {"ok": True, "result": ...} 或 {"ok": False, "error": "..."}
    """
    client = get_client()

    if not client.is_connected():
        if not client.connect():
            return {"ok": False, "error": f"无法连接 CUA: {client.status.error}"}

    try:
        # 根据 action 选择超时
        timeout = REQUEST_TIMEOUT
        if action in ("get_window_state",):
            timeout = SCREENSHOT_TIMEOUT
        elif action in ("list_apps", "launch_app"):
            timeout = 15.0

        # launch_app 特殊处理：如果 .exe 路径不存在，自动查找 UWP 应用路径
        if action == "launch_app":
            app_val = params.get("app", "")
            resolved = _resolve_app_path(app_val)
            if resolved != app_val:
                _logger.info("launch_app 路径修正: %s → %s", app_val, resolved)
                params = dict(params)
                params["app"] = resolved

        result = client.request(action, params, timeout=timeout)

        # 对于截图响应，处理 data URL（太大时截断）
        if action == "get_window_state" and isinstance(result, dict):
            screenshots = result.get("screenshots", [])
            for ss in screenshots:
                url = ss.get("url", "")
                if url and len(url) > 10000:
                    # 保留图片元数据但截断 base64 内容
                    ss["url"] = "[screenshot captured, " + str(len(url)) + " chars, " + \
                                str(ss.get("width", "?")) + "x" + str(ss.get("height", "?")) + "]"
                    # 移除原始 base64 避免 JSON 过大
                    ss.pop("data", None)

            # 添加文本形式的窗口信息摘要，帮助模型理解屏幕内容
            if result.get("text"):
                text = result["text"]
                if len(text) > 5000:
                    result["text"] = text[:5000] + "\n...[truncated]"

        return {"ok": True, "result": result}

    except TimeoutError as e:
        return {"ok": False, "error": str(e)}
    except RuntimeError as e:
        # 连接可能已断开，尝试重连
        _logger.warning("CUA 请求失败: %s", e)
        client.close()
        if client.connect():
            try:
                result = client.request(action, params, timeout=timeout)
                return {"ok": True, "result": result}
            except Exception as e2:
                return {"ok": False, "error": f"重连后仍失败: {e2}"}
        return {"ok": False, "error": str(e)}
    except Exception as e:
        return {"ok": False, "error": f"未知错误: {e}"}


def get_status() -> dict:
    """获取 CUA 状态"""
    client = get_client()
    return {
        "connected": client.status.connected,
        "pipe_name": client.status.pipe_name,
        "request_count": client.status.request_count,
        "error": client.status.error,
        "last_method": client.status.last_method,
    }
