import json
import os
import subprocess
import threading
from pathlib import Path

from .constants import DEFAULT_OPENCODE_TIMEOUT_MS
from .util import ensure_parent


class OpenCodeRunner:
    def __init__(self, store_file):
        self.store_file = Path(store_file)
        ensure_parent(self.store_file)
        self._lock = threading.Lock()
        self.timeout_ms = self._get_timeout_ms()
        self.model = os.environ.get("OPENCODE_MODEL", "").strip()
        self.session_store = self._load_session_store()

    def _get_timeout_ms(self):
        raw = os.environ.get("OPENCODE_TURN_TIMEOUT_MS", "").strip()
        if not raw:
            return DEFAULT_OPENCODE_TIMEOUT_MS
        try:
            value = int(raw)
            return value if value > 0 else DEFAULT_OPENCODE_TIMEOUT_MS
        except ValueError:
            return DEFAULT_OPENCODE_TIMEOUT_MS

    def _load_session_store(self):
        try:
            if not self.store_file.exists():
                return {}
            return json.loads(self.store_file.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _save_session_store(self):
        ensure_parent(self.store_file)
        self.store_file.write_text(
            json.dumps(self.session_store, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _resolve_command(self):
        override = os.environ.get("OPENCODE_BIN", "").strip()
        return override if override else "opencode"

    def _build_args(self, session_id, prompt):
        args = [self._resolve_command(), "run", "--format", "json", "--thinking"]
        if session_id:
            args.extend(["--session", session_id])
        if self.model:
            args.extend(["--model", self.model])
        args.extend(["--dir", str(Path.cwd()), prompt])
        return args

    def _run_once(self, user_id, user_message, session_id=None):
        completed = subprocess.run(
            self._build_args(session_id, user_message),
            cwd=Path.cwd(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=self.timeout_ms / 1000,
            shell=False,
        )

        next_session_id = session_id
        text_parts = []
        errors = []

        for line in completed.stdout.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            try:
                event = json.loads(stripped)
            except json.JSONDecodeError:
                continue

            event_type = event.get("type")
            part = event.get("part") or {}

            if event_type == "step_start":
                session_candidate = part.get("sessionID")
                if isinstance(session_candidate, str) and session_candidate.strip():
                    next_session_id = session_candidate.strip()
            elif event_type == "text":
                text = part.get("text")
                if isinstance(text, str) and text:
                    text_parts.append(text)
            elif event_type == "error":
                errors.append(self._extract_error_message(event))

        if next_session_id:
            with self._lock:
                self.session_store[user_id] = next_session_id
                self._save_session_store()

        result_text = "".join(text_parts).strip()
        if completed.returncode == 0 and result_text:
            return result_text

        stderr_text = completed.stderr.strip()
        error_message = result_text or (errors[-1] if errors else "")
        if not error_message and stderr_text:
            lines = [line.strip() for line in stderr_text.splitlines() if line.strip()]
            if lines:
                error_message = lines[-1]

        raise RuntimeError(error_message or f"opencode 返回非零退出码: {completed.returncode}")

    def run(self, user_id, user_message):
        session_id = self.session_store.get(user_id)
        try:
            return self._run_once(user_id, user_message, session_id=session_id)
        except subprocess.TimeoutExpired:
            seconds = max(1, self.timeout_ms // 1000)
            return f"❌ OpenCode 在 {seconds} 秒内没有返回结果，请稍后重试。"
        except FileNotFoundError:
            return "❌ 未找到 opencode CLI，请先安装并确保它在 PATH 中。"
        except Exception as first_error:
            if session_id:
                with self._lock:
                    self.session_store.pop(user_id, None)
                    self._save_session_store()
                try:
                    return self._run_once(user_id, user_message, session_id=None)
                except subprocess.TimeoutExpired:
                    seconds = max(1, self.timeout_ms // 1000)
                    return f"❌ OpenCode 在 {seconds} 秒内没有返回结果，请稍后重试。"
                except FileNotFoundError:
                    return "❌ 未找到 opencode CLI，请先安装并确保它在 PATH 中。"
                except Exception as second_error:
                    return f"❌ OpenCode 执行失败：{second_error}"
            return f"❌ OpenCode 执行失败：{first_error}"

    @staticmethod
    def _extract_error_message(raw):
        err_obj = raw.get("error")
        if isinstance(err_obj, dict):
            data = err_obj.get("data")
            if isinstance(data, dict):
                msg = data.get("message")
                name = err_obj.get("name")
                if isinstance(msg, str) and msg:
                    if isinstance(name, str) and name:
                        return f"{name}: {msg}"
                    return msg
            msg = err_obj.get("message")
            if isinstance(msg, str) and msg:
                return msg
            name = err_obj.get("name")
            if isinstance(name, str) and name:
                return name
        if isinstance(err_obj, str) and err_obj:
            return err_obj
        part = raw.get("part")
        if isinstance(part, dict):
            for key in ("error", "message"):
                value = part.get(key)
                if isinstance(value, str) and value:
                    return value
        message = raw.get("message")
        if isinstance(message, str) and message:
            return message
        try:
            return json.dumps(raw, ensure_ascii=False)
        except Exception:
            return str(raw)
