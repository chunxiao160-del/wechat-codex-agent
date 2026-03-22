import json
import os
import time
from pathlib import Path

from .util import ensure_parent, load_json, log


class SingleInstanceLock:
    def __init__(self, lock_file):
        self.lock_file = Path(lock_file)
        self.fd = None

    def acquire(self):
        ensure_parent(self.lock_file)

        while True:
            try:
                self.fd = os.open(self.lock_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                payload = json.dumps(
                    {"pid": os.getpid(), "startedAt": time.strftime("%Y-%m-%dT%H:%M:%S")},
                    ensure_ascii=False,
                    indent=2,
                )
                os.write(self.fd, payload.encode("utf-8"))
                return True
            except FileExistsError:
                running_pid = self._read_locked_pid()
                if running_pid and self._is_process_alive(running_pid):
                    log(f"检测到已有实例在运行 (pid={running_pid})，当前进程退出")
                    return False

                try:
                    self.lock_file.unlink()
                    log("发现陈旧锁文件，已自动清理")
                except FileNotFoundError:
                    pass

    def release(self):
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None

        try:
            self.lock_file.unlink()
        except FileNotFoundError:
            pass

    def _read_locked_pid(self):
        parsed = load_json(self.lock_file)
        if not isinstance(parsed, dict):
            return None

        pid = parsed.get("pid")
        if isinstance(pid, int) and pid > 0:
            return pid
        return None

    @staticmethod
    def _is_process_alive(pid):
        if os.name == "nt":
            handle = None
            try:
                import ctypes

                process_query_limited_information = 0x1000
                still_active = 259
                handle = ctypes.windll.kernel32.OpenProcess(
                    process_query_limited_information, False, pid
                )
                if not handle:
                    return False
                exit_code = ctypes.c_ulong()
                if not ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                    return False
                return exit_code.value == still_active
            except Exception:
                return False
            finally:
                if handle:
                    ctypes.windll.kernel32.CloseHandle(handle)

        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
