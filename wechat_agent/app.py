import atexit
import queue
import signal
import threading
import sys

from .codex import CodexRunner
from .mcp import McpBridge
from .opencode import OpenCodeRunner
from .constants import BACKOFF_DELAY_MS, MAX_CONSECUTIVE_FAILURES, RETRY_DELAY_MS
from .lock import SingleInstanceLock
from .state import (
    CODEX_THREAD_STORE_FILE,
    INSTANCE_LOCK_FILE,
    OPENCODE_SESSION_STORE_FILE,
    SYNC_BUF_FILE,
    get_app_config_file,
    get_credentials_file,
    load_account,
    load_app_config,
    route_task,
)
from .util import log, sleep_ms
from .wechat import WechatClient, extract_text


def _register_exit_handlers(lock):
    atexit.register(lock.release)

    def handle_exit(_signum, _frame):
        lock.release()
        raise SystemExit(0)

    for sig_name in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, sig_name, None)
        if sig is not None:
            signal.signal(sig, handle_exit)


def _log_startup_state():
    account = load_account()
    if not account:
        log("⚠️  未找到微信登录凭据，请先运行 npm run setup 或设置 BOT_TOKEN")
        log(f"凭据文件位置: {get_credentials_file()}")
    elif account.get("source") == "env":
        log("使用环境变量 BOT_TOKEN 登录微信")
    else:
        suffix = f": {account.get('accountId')}" if account.get("accountId") else ""
        log(f"使用本地微信登录凭据{suffix}")

    app_config = load_app_config()
    if not app_config or not app_config.get("defaultProvider"):
        log("⚠️  未找到 provider 配置，默认回退到 codex")
        log(f"配置文件位置: {get_app_config_file()}")
        log("请运行 npm run setup 完成首次 provider 选择")
    elif app_config.get("defaultProvider") == "claude" and sys.stdin.isatty():
        log("⚠️  当前默认 provider 是 claude。请使用 `claude --dangerously-load-development-channels server:wechat` 启动。")


def _create_worker(task_queue):
    def worker():
        while True:
            task = task_queue.get()
            try:
                task()
            except Exception as err:
                log(f"任务执行失败: {err}")
            finally:
                task_queue.task_done()

    threading.Thread(target=worker, name="wechat-worker", daemon=True).start()


def main():
    lock = SingleInstanceLock(INSTANCE_LOCK_FILE)
    if not lock.acquire():
        return

    _register_exit_handlers(lock)
    _log_startup_state()

    app_config = load_app_config()
    default_provider = route_task((app_config or {}).get("defaultProvider"))

    wechat_client = WechatClient()
    codex_runner = CodexRunner(CODEX_THREAD_STORE_FILE)
    opencode_runner = OpenCodeRunner(OPENCODE_SESSION_STORE_FILE)
    context_token_cache = {}
    mcp_bridge = McpBridge(wechat_client, context_token_cache)
    mcp_bridge.start()

    task_queue = queue.Queue()
    _create_worker(task_queue)

    def send_provider_result(provider, sender_id, result, context_token=None):
        sender = sender_id.split("@")[0]
        ctx = context_token or context_token_cache.get(sender_id)
        if not ctx:
            log(f"[{provider}] 已拿到结果，但无法回复 {sender}：缺少 context_token")
            return

        response = wechat_client.send_message(sender_id, ctx, result[:1000])
        message_id = None
        if isinstance(response, dict):
            message_id = response.get("message_id") or response.get("msg_id")

        if message_id:
            log(f"[{provider}] 已回复 {sender}，message_id={message_id}")
        else:
            log(f"[{provider}] 已回复 {sender}，sendMessage 返回: {response}")

    get_updates_buf = ""
    consecutive_failures = 0
    if SYNC_BUF_FILE.exists():
        try:
            get_updates_buf = SYNC_BUF_FILE.read_text(encoding="utf-8")
            log("恢复上次同步状态")
        except Exception:
            pass

    log("开始监听微信消息...")
    log(f"当前默认 provider: {default_provider}")

    while True:
        try:
            response = wechat_client.get_updates(get_updates_buf)

            is_error = (
                ("ret" in response and response.get("ret") not in (None, 0))
                or ("errcode" in response and response.get("errcode") not in (None, 0))
            )
            if is_error:
                consecutive_failures += 1
                errmsg = response.get("errmsg") or ""
                log(
                    f"getUpdates 失败: ret={response.get('ret')} errcode={response.get('errcode')} errmsg={errmsg}"
                )
                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    log(f"连续失败 {MAX_CONSECUTIVE_FAILURES} 次，等待 {BACKOFF_DELAY_MS // 1000}s...")
                    consecutive_failures = 0
                    sleep_ms(BACKOFF_DELAY_MS)
                else:
                    sleep_ms(RETRY_DELAY_MS)
                continue

            consecutive_failures = 0

            if response.get("get_updates_buf"):
                get_updates_buf = response["get_updates_buf"]
                try:
                    SYNC_BUF_FILE.write_text(get_updates_buf, encoding="utf-8")
                except Exception:
                    pass

            for msg in response.get("msgs") or []:
                if msg.get("message_type") != 1:
                    continue

                text = extract_text(msg)
                if not text:
                    continue

                sender_id = msg.get("from_user_id") or "unknown"
                context_token = msg.get("context_token")
                if context_token:
                    context_token_cache[sender_id] = context_token
                else:
                    log(f"收到消息但缺少 context_token: from={sender_id.split('@')[0]}，后续可能无法自动回复")

                log(f"收到消息: from={sender_id.split('@')[0]} text={text[:60]}")

                if default_provider == "codex":

                    def codex_task(sender_id=sender_id, text=text, context_token=context_token):
                        log(f"[codex] 处理来自 {sender_id.split('@')[0]} 的消息...")
                        log("[codex] 已转交 Codex，会在拿到结果后自动回复微信")
                        result = codex_runner.run(sender_id, text)
                        log(f"[codex] 已收到结果，准备回复 {sender_id.split('@')[0]}")
                        send_provider_result("codex", sender_id, result, context_token=context_token)

                    task_queue.put(codex_task)
                elif default_provider == "opencode":

                    def opencode_task(sender_id=sender_id, text=text, context_token=context_token):
                        log(f"[opencode] 处理来自 {sender_id.split('@')[0]} 的消息...")
                        log("[opencode] 已转交 OpenCode，会在拿到结果后自动回复微信")
                        result = opencode_runner.run(sender_id, text)
                        log(f"[opencode] 已收到结果，准备回复 {sender_id.split('@')[0]}")
                        send_provider_result("opencode", sender_id, result, context_token=context_token)

                    task_queue.put(opencode_task)
                else:

                    def claude_task(sender_id=sender_id, text=text):
                        log(f"[claude] 推送来自 {sender_id.split('@')[0]} 的消息到 Claude Code channel...")
                        mcp_bridge.notify_claude_channel(text, sender_id)

                    task_queue.put(claude_task)

        except KeyboardInterrupt:
            raise
        except Exception as err:
            consecutive_failures += 1
            log(f"轮询异常: {err}")
            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                consecutive_failures = 0
                sleep_ms(BACKOFF_DELAY_MS)
            else:
                sleep_ms(RETRY_DELAY_MS)
