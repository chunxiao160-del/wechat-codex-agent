import base64
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


def log(message):
    sys.stderr.write(f"[wechat-agent] {message}\n")
    sys.stderr.flush()


def ensure_parent(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)


def load_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return None


def now_utc_iso():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def sleep_ms(ms):
    time.sleep(ms / 1000)


def random_wechat_uin():
    value = str(int.from_bytes(os.urandom(4), "big")).encode("utf-8")
    return base64.b64encode(value).decode("ascii")


def configure_stdio():
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
