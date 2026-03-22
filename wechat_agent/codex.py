from collections import deque
from dataclasses import dataclass, field
import json
import os
import shutil
import subprocess
import threading
from pathlib import Path

from .constants import DEFAULT_CODEX_TIMEOUT_MS
from .session_store import MultiSessionStore
from .util import ensure_parent, log


@dataclass
class CodexEventAccumulator:
    thread_id: str = ""
    item_order: list[str] = field(default_factory=list)
    item_text: dict[str, str] = field(default_factory=dict)
    messages: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    turn_failed: bool = False

    def handle_event(self, event):
        event_type = event.get("type")

        if event_type == "thread.started":
            thread_id = event.get("thread_id")
            if isinstance(thread_id, str) and thread_id.strip():
                self.thread_id = thread_id.strip()
            return

        if event_type in {"item.started", "item.delta", "item.completed"}:
            self._handle_item_event(event_type, event.get("item") or event)
            return

        if event_type in {"turn.failed", "error"}:
            self.turn_failed = True
            message = extract_error_message(event)
            if message:
                self.errors.append(message)

    def final_text(self):
        parts = []

        for item_id in self.item_order:
            text = self.item_text.get(item_id, "").strip()
            if text:
                parts.append(text)

        parts.extend(message.strip() for message in self.messages if isinstance(message, str) and message.strip())
        return "\n".join(parts).strip()

    def _handle_item_event(self, event_type, payload):
        item = payload if isinstance(payload, dict) else {}
        item_type = item.get("type") or item.get("item_type") or item.get("itemType")
        item_id = item.get("id") or item.get("item_id") or item.get("itemId")

        if item_type not in (None, "", "agent_message"):
            return

        if isinstance(item_id, str) and item_id and item_id not in self.item_order:
            self.item_order.append(item_id)

        if event_type == "item.delta":
            delta = item.get("delta") or payload.get("delta")
            if isinstance(item_id, str) and isinstance(delta, str) and delta:
                self.item_text[item_id] = self.item_text.get(item_id, "") + delta
            return

        text = item.get("text") or payload.get("text")
        if isinstance(text, str) and text:
            if isinstance(item_id, str) and item_id:
                self.item_text[item_id] = text
            else:
                self.messages.append(text)


@dataclass
class PendingResponse:
    event: threading.Event = field(default_factory=threading.Event)
    response: dict = None
    error: Exception = None


@dataclass
class CodexAppTurnAccumulator:
    thread_id: str
    turn_id: str
    item_order: list[str] = field(default_factory=list)
    item_text: dict[str, str] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    status: str = ""
    completed: threading.Event = field(default_factory=threading.Event)

    def handle_notification(self, message):
        method = str(message.get("method") or "")
        params = message.get("params") or {}

        if method == "item/agentMessage/delta":
            item_id = params.get("itemId")
            delta = params.get("delta")
            if isinstance(item_id, str) and item_id:
                if item_id not in self.item_order:
                    self.item_order.append(item_id)
                if isinstance(delta, str) and delta:
                    self.item_text[item_id] = self.item_text.get(item_id, "") + delta
            return

        if method == "item/completed":
            item = params.get("item") or {}
            if item.get("type") != "agentMessage":
                return
            item_id = item.get("id")
            text = item.get("text")
            if isinstance(item_id, str) and item_id:
                if item_id not in self.item_order:
                    self.item_order.append(item_id)
                if isinstance(text, str):
                    self.item_text[item_id] = text
            return

        if method == "error":
            error_message = extract_error_message(params.get("error") or params)
            if error_message:
                self.errors.append(error_message)
            return

        if method == "turn/completed":
            turn = params.get("turn") or {}
            self.status = str(turn.get("status") or "")
            error_message = extract_error_message(turn.get("error") or {})
            if error_message:
                self.errors.append(error_message)
            self.completed.set()

    def final_text(self):
        parts = []
        for item_id in self.item_order:
            text = self.item_text.get(item_id, "").strip()
            if text:
                parts.append(text)
        return "\n".join(parts).strip()


class CodexAppServerBootstrapError(RuntimeError):
    pass


class CodexAppServerTurnError(RuntimeError):
    pass


def extract_error_message(event):
    for key in ("message", "error", "stderr"):
        value = event.get(key) if isinstance(event, dict) else None
        if isinstance(value, str) and value.strip():
            return value.strip()

    error = event.get("error") if isinstance(event, dict) else None
    if isinstance(error, dict):
        for key in ("message", "detail", "stderr", "additionalDetails"):
            value = error.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()

    if isinstance(event, dict):
        try:
            return json.dumps(event, ensure_ascii=False)
        except Exception:
            return str(event)

    return str(event)


class CodexAppServerClient:
    def __init__(self, command, cwd, env, model=None, timeout_ms=DEFAULT_CODEX_TIMEOUT_MS, request_timeout_ms=15_000):
        self.command = command
        self.cwd = str(cwd)
        self.env = env
        self.model = model
        self.timeout_ms = timeout_ms
        self.request_timeout_ms = request_timeout_ms

        self._process = None
        self._initialized = False
        self._next_id = 1
        self._state_lock = threading.Lock()
        self._startup_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._pending_responses = {}
        self._turn_states = {}
        self._turn_backlog = {}
        self._loaded_threads = set()
        self._stderr_tail = deque(maxlen=80)

    def ensure_thread(self, thread_id=None):
        self._ensure_started()
        if thread_id:
            with self._state_lock:
                if thread_id in self._loaded_threads and self._process and self._process.poll() is None:
                    return thread_id, False

            result = self._send_request(
                "thread/resume",
                self._thread_resume_params(thread_id),
                timeout_ms=self.request_timeout_ms,
            )
            thread = (result or {}).get("thread") or {}
            resumed_id = str(thread.get("id") or thread_id).strip()
            if not resumed_id:
                raise CodexAppServerBootstrapError("app-server thread/resume 未返回 thread id")
            with self._state_lock:
                self._loaded_threads.add(resumed_id)
            return resumed_id, False

        result = self._send_request(
            "thread/start",
            self._thread_start_params(),
            timeout_ms=self.request_timeout_ms,
        )
        thread = (result or {}).get("thread") or {}
        started_id = str(thread.get("id") or "").strip()
        if not started_id:
            raise CodexAppServerBootstrapError("app-server thread/start 未返回 thread id")
        with self._state_lock:
            self._loaded_threads.add(started_id)
        return started_id, True

    def run_turn(self, thread_id, user_message):
        self._ensure_started()
        result = self._send_request(
            "turn/start",
            {
                "threadId": thread_id,
                "input": [{"type": "text", "text": user_message, "text_elements": []}],
            },
            timeout_ms=self.request_timeout_ms,
        )

        turn = (result or {}).get("turn") or {}
        turn_id = str(turn.get("id") or "").strip()
        if not turn_id:
            raise CodexAppServerTurnError("app-server turn/start 未返回 turn id")

        accumulator = CodexAppTurnAccumulator(thread_id=thread_id, turn_id=turn_id)
        backlog = []
        with self._state_lock:
            self._turn_states[turn_id] = accumulator
            backlog = self._turn_backlog.pop(turn_id, [])

        for message in backlog:
            accumulator.handle_notification(message)

        if turn.get("status") in {"completed", "failed", "interrupted"}:
            accumulator.handle_notification(
                {
                    "method": "turn/completed",
                    "params": {"threadId": thread_id, "turn": turn},
                }
            )

        if not accumulator.completed.wait(self.timeout_ms / 1000):
            self._best_effort_interrupt(thread_id, turn_id)
            with self._state_lock:
                self._turn_states.pop(turn_id, None)
                self._turn_backlog.pop(turn_id, None)
            raise subprocess.TimeoutExpired(cmd="codex app-server turn/start", timeout=self.timeout_ms / 1000)

        with self._state_lock:
            self._turn_states.pop(turn_id, None)

        result_text = accumulator.final_text()
        if accumulator.status == "completed" and result_text:
            return result_text

        error_message = accumulator.errors[-1] if accumulator.errors else ""
        if accumulator.status == "completed":
            raise CodexAppServerTurnError("Codex 未返回文本结果。")
        if accumulator.status == "interrupted":
            raise CodexAppServerTurnError(error_message or "Codex 会话已中断。")
        raise CodexAppServerTurnError(error_message or f"Codex turn 状态异常: {accumulator.status or 'unknown'}")

    def _thread_start_params(self):
        params = {
            "cwd": self.cwd,
            "approvalPolicy": "never",
            "sandbox": "danger-full-access",
            "experimentalRawEvents": False,
            "persistExtendedHistory": False,
            "serviceName": "wechat-agent",
        }
        if self.model:
            params["model"] = self.model
        return params

    def _thread_resume_params(self, thread_id):
        params = {
            "threadId": thread_id,
            "cwd": self.cwd,
            "approvalPolicy": "never",
            "sandbox": "danger-full-access",
            "persistExtendedHistory": False,
        }
        if self.model:
            params["model"] = self.model
        return params

    def _ensure_started(self):
        with self._startup_lock:
            if self._process and self._process.poll() is None and self._initialized:
                return

            self._start_process()
            try:
                self._send_request(
                    "initialize",
                    {
                        "clientInfo": {
                            "name": "wechat-agent",
                            "title": "wechat-agent",
                            "version": "1.0.0",
                        },
                        "capabilities": {"experimentalApi": False},
                    },
                    timeout_ms=self.request_timeout_ms,
                    ensure_started=False,
                )
                self._send_notification("initialized", ensure_started=False)
                self._initialized = True
            except Exception as err:
                self._handle_process_exit(self._process, CodexAppServerBootstrapError(str(err)))
                raise CodexAppServerBootstrapError(str(err))

    def _start_process(self):
        command = [
            self.command,
            "-C",
            self.cwd,
            "app-server",
            "--listen",
            "stdio://",
            "--session-source",
            "wechat-agent",
        ]
        log(
            "[codex] 启动 app-server: "
            + json.dumps({"cmd": command[0], "cwd": self.cwd}, ensure_ascii=False)
        )
        process = subprocess.Popen(
            command,
            cwd=self.cwd,
            env=self.env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=False,
            bufsize=1,
        )

        with self._state_lock:
            self._process = process
            self._initialized = False
            self._pending_responses = {}
            self._turn_states = {}
            self._turn_backlog = {}
            self._loaded_threads = set()
            self._stderr_tail.clear()

        stdout_reader = threading.Thread(
            target=self._read_stdout,
            args=(process,),
            name="codex-app-server-stdout",
            daemon=True,
        )
        stderr_reader = threading.Thread(
            target=self._read_stderr,
            args=(process,),
            name="codex-app-server-stderr",
            daemon=True,
        )
        stdout_reader.start()
        stderr_reader.start()

    def _read_stdout(self, process):
        try:
            assert process.stdout is not None
            for line in process.stdout:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    message = json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                self._handle_stdout_message(message)
        except Exception as err:
            self._handle_process_exit(process, CodexAppServerBootstrapError(f"读取 app-server stdout 失败: {err}"))
            return

        self._handle_process_exit(process)

    def _read_stderr(self, process):
        try:
            assert process.stderr is not None
            for line in process.stderr:
                stripped = line.rstrip()
                if not stripped:
                    continue
                with self._state_lock:
                    self._stderr_tail.append(stripped)
                if "ERROR" in stripped or "panicked" in stripped:
                    log(f"[codex-app-server] {stripped}")
        except Exception:
            pass

    def _handle_stdout_message(self, message):
        response_id = message.get("id")
        if response_id is not None:
            pending = None
            with self._state_lock:
                pending = self._pending_responses.pop(response_id, None)
            if pending is not None:
                pending.response = message
                pending.event.set()
            return

        method = str(message.get("method") or "")
        params = message.get("params") or {}

        if method == "thread/started":
            thread = params.get("thread") or {}
            thread_id = str(thread.get("id") or "").strip()
            if thread_id:
                with self._state_lock:
                    self._loaded_threads.add(thread_id)
            return

        turn_id = notification_turn_id(message)
        if not turn_id:
            return

        state = None
        with self._state_lock:
            state = self._turn_states.get(turn_id)
            if state is None:
                self._turn_backlog.setdefault(turn_id, []).append(message)
                return

        state.handle_notification(message)

    def _send_request(self, method, params, timeout_ms=None, ensure_started=True):
        if ensure_started:
            self._ensure_started()

        pending = PendingResponse()
        with self._state_lock:
            process = self._process
            if process is None or process.poll() is not None:
                raise CodexAppServerBootstrapError(self._exit_message(process))
            request_id = self._next_id
            self._next_id += 1
            self._pending_responses[request_id] = pending

        try:
            self._write_message({"id": request_id, "method": method, "params": params})
        except Exception as err:
            with self._state_lock:
                self._pending_responses.pop(request_id, None)
            self._handle_process_exit(process, CodexAppServerBootstrapError(str(err)))
            raise CodexAppServerBootstrapError(str(err))

        timeout_s = max(1, int((timeout_ms or self.request_timeout_ms) / 1000))
        if not pending.event.wait(timeout_s):
            with self._state_lock:
                self._pending_responses.pop(request_id, None)
            raise CodexAppServerBootstrapError(f"app-server 请求超时: {method}")

        if pending.error:
            raise CodexAppServerBootstrapError(str(pending.error))

        response = pending.response or {}
        if "error" in response:
            raise CodexAppServerBootstrapError(self._format_rpc_error(response["error"]))

        return response.get("result") or {}

    def _send_notification(self, method, ensure_started=True):
        if ensure_started:
            self._ensure_started()
        self._write_message({"method": method})

    def _write_message(self, payload):
        with self._write_lock:
            process = self._process
            if process is None or process.stdin is None or process.poll() is not None:
                raise BrokenPipeError(self._exit_message(process))
            process.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
            process.stdin.flush()

    def _best_effort_interrupt(self, thread_id, turn_id):
        try:
            self._send_request(
                "turn/interrupt",
                {"threadId": thread_id, "turnId": turn_id},
                timeout_ms=5_000,
            )
        except Exception:
            pass

    def _handle_process_exit(self, process, error=None):
        pending = []
        turns = []
        with self._state_lock:
            if process is None or process is not self._process:
                return
            pending = list(self._pending_responses.values())
            turns = list(self._turn_states.values())
            self._pending_responses = {}
            self._turn_states = {}
            self._turn_backlog = {}
            self._loaded_threads = set()
            self._initialized = False
            self._process = None

        exit_error = error or CodexAppServerBootstrapError(self._exit_message(process))
        for pending_response in pending:
            pending_response.error = exit_error
            pending_response.event.set()

        for turn_state in turns:
            turn_state.errors.append(str(exit_error))
            turn_state.status = "failed"
            turn_state.completed.set()

    def _exit_message(self, process):
        code = None
        if process is not None:
            code = process.poll()
        stderr_text = self._stderr_text()
        suffix = f"，exit={code}" if code is not None else ""
        if stderr_text:
            return f"codex app-server 已退出{suffix}: {stderr_text}"
        return f"codex app-server 已退出{suffix}".rstrip()

    def _stderr_text(self):
        with self._state_lock:
            if not self._stderr_tail:
                return ""
            return " | ".join(list(self._stderr_tail)[-5:])

    @staticmethod
    def _format_rpc_error(error):
        if isinstance(error, dict):
            message = error.get("message")
            if isinstance(message, str) and message.strip():
                return message.strip()
            return extract_error_message(error)
        return str(error)


def notification_turn_id(message):
    params = message.get("params") or {}
    direct = params.get("turnId")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()
    turn = params.get("turn") or {}
    turn_id = turn.get("id")
    if isinstance(turn_id, str) and turn_id.strip():
        return turn_id.strip()
    return ""


class CodexRunner:
    def __init__(self, store_file):
        self.store_file = Path(store_file)
        ensure_parent(self.store_file)
        self._lock = threading.Lock()
        self.timeout_ms = self._get_timeout_ms()
        self.request_timeout_ms = self._get_request_timeout_ms()
        self.model = os.environ.get("CODEX_MODEL", "").strip()
        self.session_store = MultiSessionStore(self.store_file)
        self.command = self._resolve_command()
        self.codex_home_root = self._resolve_codex_home_root()
        self.codex_env = self._build_codex_env()
        self.use_app_server = str(os.environ.get("CODEX_USE_APP_SERVER", "1")).strip().lower() not in {
            "0",
            "false",
            "no",
        }
        self.app_client = CodexAppServerClient(
            self.command,
            Path.cwd(),
            self.codex_env,
            model=self.model or None,
            timeout_ms=self.timeout_ms,
            request_timeout_ms=self.request_timeout_ms,
        )

    def _get_timeout_ms(self):
        raw = os.environ.get("CODEX_TURN_TIMEOUT_MS", "").strip()
        if not raw:
            return DEFAULT_CODEX_TIMEOUT_MS
        try:
            value = int(raw)
            return value if value > 0 else DEFAULT_CODEX_TIMEOUT_MS
        except ValueError:
            return DEFAULT_CODEX_TIMEOUT_MS

    def _get_request_timeout_ms(self):
        raw = os.environ.get("CODEX_APP_SERVER_REQUEST_TIMEOUT_MS", "").strip()
        if not raw:
            return 15_000
        try:
            value = int(raw)
            return value if value > 0 else 15_000
        except ValueError:
            return 15_000

    def _resolve_command(self):
        override = os.environ.get("CODEX_BIN", "").strip()
        if override:
            return override

        candidates = ["codex"]
        if os.name == "nt":
            candidates = ["codex.cmd", "codex.exe", "codex"]

        for candidate in candidates:
            resolved = shutil.which(candidate)
            if resolved:
                return resolved

        return "codex"

    def _resolve_codex_home_root(self):
        override = os.environ.get("WECHAT_AGENT_CODEX_HOME", "").strip()
        if override:
            return Path(override)
        return Path.cwd() / "sessions" / "codex-home"

    def _build_codex_env(self):
        self._sync_codex_home()
        env = os.environ.copy()
        env["HOME"] = str(self.codex_home_root)
        return env

    def _sync_codex_home(self):
        source_codex_dir = Path.home() / ".codex"
        target_codex_dir = self.codex_home_root / ".codex"
        target_codex_dir.mkdir(parents=True, exist_ok=True)

        for filename in ("auth.json", "config.toml", "version.json"):
            source = source_codex_dir / filename
            target = target_codex_dir / filename
            if not source.exists():
                continue
            try:
                if (
                    target.exists()
                    and target.stat().st_mtime >= source.stat().st_mtime
                    and target.stat().st_size == source.stat().st_size
                ):
                    continue
                shutil.copy2(source, target)
            except Exception:
                continue

    def _base_args(self):
        args = [
            self.command,
            "-C",
            str(Path.cwd()),
            "-a",
            "never",
            "-s",
            "danger-full-access",
        ]
        if self.model:
            args.extend(["-m", self.model])
        return args

    @staticmethod
    def _build_prompt(user_message, fresh_session):
        if not fresh_session:
            return user_message
        return "\n".join(
            [
                "你通过微信与用户交流。",
                "默认用中文回复，除非用户明确使用其他语言。",
                "回复尽量直接、简洁、可执行。",
                "微信不渲染 Markdown，尽量输出纯文本。",
                "",
                f"用户消息：{user_message}",
            ]
        )

    def _run_once_exec(self, user_id, user_message, existing_thread_id=None):
        self._sync_codex_home()
        fresh_session = not existing_thread_id
        args = self._base_args() + ["exec"]
        if existing_thread_id:
            args.extend(["resume", existing_thread_id])
        args.extend(["--skip-git-repo-check", "--json", self._build_prompt(user_message, fresh_session)])
        log(
            "[codex] 启动命令: "
            + json.dumps(
                {
                    "cmd": args[0],
                    "resume": bool(existing_thread_id),
                    "cwd": str(Path.cwd()),
                },
                ensure_ascii=False,
            )
        )

        process = subprocess.Popen(
            args,
            cwd=Path.cwd(),
            env=self.codex_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=False,
            bufsize=1,
        )
        accumulator = CodexEventAccumulator(thread_id=existing_thread_id or "")

        stdout_error = []
        stderr_chunks = []

        def read_stdout():
            try:
                assert process.stdout is not None
                for line in process.stdout:
                    stripped = line.strip()
                    if not stripped:
                        continue
                    try:
                        event = json.loads(stripped)
                    except json.JSONDecodeError:
                        continue
                    accumulator.handle_event(event)
            except Exception as err:
                stdout_error.append(err)

        def read_stderr():
            try:
                assert process.stderr is not None
                for line in process.stderr:
                    stderr_chunks.append(line)
            except Exception:
                pass

        reader = threading.Thread(target=read_stdout, name="codex-stdout-reader", daemon=True)
        stderr_reader = threading.Thread(target=read_stderr, name="codex-stderr-reader", daemon=True)
        reader.start()
        stderr_reader.start()

        try:
            return_code = process.wait(timeout=self.timeout_ms / 1000)
        except subprocess.TimeoutExpired:
            process.kill()
            reader.join(timeout=2)
            stderr_reader.join(timeout=2)
            raise

        reader.join(timeout=2)
        stderr_reader.join(timeout=2)
        stderr_text = "".join(stderr_chunks).strip()

        if stdout_error:
            raise RuntimeError(str(stdout_error[-1]))

        result_text = accumulator.final_text()

        if accumulator.thread_id:
            with self._lock:
                self.session_store.set_current_engine_id(user_id, accumulator.thread_id)
                self.session_store.save()

        if return_code == 0 and result_text:
            return result_text

        error_message = result_text or (accumulator.errors[-1] if accumulator.errors else "")
        if not error_message and stderr_text:
            lines = [line.strip() for line in stderr_text.splitlines() if line.strip()]
            if lines:
                error_message = lines[-1]

        raise RuntimeError(error_message or f"codex 返回非零退出码: {return_code}")

    def _run_once_app_server(self, user_id, user_message, existing_thread_id=None):
        thread_id, fresh_session = self.app_client.ensure_thread(existing_thread_id)
        with self._lock:
            self.session_store.set_current_engine_id(user_id, thread_id)
            self.session_store.save()
        return self.app_client.run_turn(thread_id, self._build_prompt(user_message, fresh_session))

    def _run_exec_with_retry(self, user_id, user_message, existing_thread_id):
        try:
            return self._run_once_exec(user_id, user_message, existing_thread_id=existing_thread_id)
        except subprocess.TimeoutExpired:
            raise
        except Exception as first_error:
            if existing_thread_id:
                log(f"[codex] 续用会话失败，改为新会话重试: {first_error}")
                with self._lock:
                    self.session_store.clear_current_engine_id(user_id)
                    self.session_store.save()
                return self._run_once_exec(user_id, user_message, existing_thread_id=None)
            raise

    def _run_app_server_with_retry(self, user_id, user_message, existing_thread_id):
        try:
            return self._run_once_app_server(user_id, user_message, existing_thread_id=existing_thread_id)
        except subprocess.TimeoutExpired:
            raise
        except CodexAppServerTurnError:
            raise
        except Exception as first_error:
            if existing_thread_id:
                log(f"[codex] app-server 续用会话失败，改为新会话重试: {first_error}")
                with self._lock:
                    self.session_store.clear_current_engine_id(user_id)
                    self.session_store.save()
                return self._run_once_app_server(user_id, user_message, existing_thread_id=None)
            raise

    def run(self, user_id, user_message):
        with self._lock:
            existing_thread_id = self.session_store.get_current_engine_id(user_id, create_if_missing=True)
            self.session_store.save()

        if not self.use_app_server:
            try:
                return self._run_exec_with_retry(user_id, user_message, existing_thread_id)
            except subprocess.TimeoutExpired:
                seconds = max(1, self.timeout_ms // 1000)
                return f"❌ Codex 在 {seconds} 秒内没有返回结果，请稍后重试。"
            except Exception as err:
                return f"❌ Codex 执行失败：{err}"

        try:
            return self._run_app_server_with_retry(user_id, user_message, existing_thread_id)
        except CodexAppServerBootstrapError as app_server_error:
            log(f"[codex] app-server 不可用，回退到 exec: {app_server_error}")
            try:
                return self._run_exec_with_retry(user_id, user_message, existing_thread_id)
            except subprocess.TimeoutExpired:
                seconds = max(1, self.timeout_ms // 1000)
                return f"❌ Codex 在 {seconds} 秒内没有返回结果，请稍后重试。"
            except Exception as exec_error:
                return f"❌ Codex 执行失败：{exec_error}"
        except subprocess.TimeoutExpired:
            seconds = max(1, self.timeout_ms // 1000)
            return f"❌ Codex 在 {seconds} 秒内没有返回结果，请稍后重试。"
        except Exception as first_error:
            return f"❌ Codex 执行失败：{first_error}"

    def create_session(self, user_id, name=None):
        with self._lock:
            session = self.session_store.create_session(user_id, name=name)
            self.session_store.save()
            return session

    def list_sessions(self, user_id):
        with self._lock:
            return self.session_store.list_sessions(user_id)

    def get_current_session(self, user_id):
        with self._lock:
            return self.session_store.get_current_session(user_id, create_if_missing=False)

    def switch_session(self, user_id, target):
        with self._lock:
            session = self.session_store.switch_session(user_id, target)
            if session:
                self.session_store.save()
            return session
