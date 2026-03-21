from __future__ import annotations

"""
ChatGPT 批量注册工具 — Web 管理界面
Flask 后端: 配置管理 / 任务控制 / SSE 实时日志 / 账号管理 / OAuth 导出
"""

import io
import json
import os
import queue
import sys
import threading
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

from flask import Flask, Response, jsonify, render_template, request, send_file

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "changeme-in-production")

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("DATA_DIR", BASE_DIR / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

CONFIG_PATH = DATA_DIR / "config.json"
ACCOUNTS_FILE = DATA_DIR / "registered_accounts.txt"
TOKEN_DIR = DATA_DIR / "codex_tokens"
LEGACY_CONFIG_PATH = BASE_DIR / "config.json"

if not CONFIG_PATH.exists() and LEGACY_CONFIG_PATH.exists():
    import shutil

    shutil.copy(LEGACY_CONFIG_PATH, CONFIG_PATH)

DEFAULT_CONFIG: dict[str, Any] = {
    "duckmail_api_base": "",
    "duckmail_domain": "",
    "duckmail_bearer": "",
    "default_proxy": "",
    "default_total_accounts": 10,
    "default_output_file": "registered_accounts.txt",
    "teams": [],
    "oauth_required": True,
    "oauth_issuer": "https://auth.openai.com",
    "oauth_client_id": "app_EMoamEEZ73f0CkXaXp7hrann",
    "oauth_redirect_uri": "com.openai.chat://auth.openai.com/android/com.openai.chat/callback",
    "sub2api_url": "",
    "sub2api_token": "",
    "upload_api_url": "",
    "upload_api_token": "",
}

TASK_PROGRESS_TEMPLATE = {
    "total": 0,
    "done": 0,
    "success": 0,
    "fail": 0,
    "stopped": 0,
    "started_at": None,
}

CONFIG_ALIASES = {
    "proxy": "default_proxy",
    "SUB2API_URL": "sub2api_url",
    "SUB2API_TOKEN": "sub2api_token",
}

_task_lock = threading.Lock()
_task_running = False
_task_thread: threading.Thread | None = None
_task_stop_event = threading.Event()
_task_progress = dict(TASK_PROGRESS_TEMPLATE)

_log_subscribers: list[queue.Queue[str]] = []
_log_lock = threading.Lock()


def _utc_now_iso() -> str:
    return datetime.utcnow().isoformat() + "Z"


def _json_body() -> dict[str, Any]:
    return request.get_json(silent=True) or {}


def _normalize_config(raw: dict[str, Any] | None) -> dict[str, Any]:
    merged = dict(DEFAULT_CONFIG)
    if raw:
        merged.update(raw)
    for old_key, new_key in CONFIG_ALIASES.items():
        if merged.get(old_key) and not merged.get(new_key):
            merged[new_key] = merged[old_key]
        merged.pop(old_key, None)
    merged["teams"] = [team for team in merged.get("teams", []) if isinstance(team, dict)]
    return merged


def _read_config() -> dict[str, Any]:
    if CONFIG_PATH.exists():
        try:
            with CONFIG_PATH.open("r", encoding="utf-8") as file:
                return _normalize_config(json.load(file))
        except (OSError, json.JSONDecodeError):
            pass
    return dict(DEFAULT_CONFIG)


def _write_config(cfg: dict[str, Any]) -> None:
    normalized = _normalize_config(cfg)
    with CONFIG_PATH.open("w", encoding="utf-8") as file:
        json.dump(normalized, file, indent=4, ensure_ascii=False)


def _safe_positive_int(value: Any, default: int, *, minimum: int = 1) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, parsed)


def _task_snapshot() -> dict[str, Any]:
    with _task_lock:
        return dict(_task_progress)


def _reset_task_progress(total: int) -> None:
    global _task_progress
    with _task_lock:
        _task_progress = {
            **TASK_PROGRESS_TEMPLATE,
            "total": total,
            "started_at": _utc_now_iso(),
        }


def _broadcast(payload: dict[str, Any]) -> None:
    raw = json.dumps(payload, ensure_ascii=False)
    with _log_lock:
        dead_queues = []
        for subscriber in _log_subscribers:
            try:
                subscriber.put_nowait(raw)
            except queue.Full:
                dead_queues.append(subscriber)
        for subscriber in dead_queues:
            _log_subscribers.remove(subscriber)


def _broadcast_log(line: str) -> None:
    _broadcast({"type": "log", "msg": line})


def _broadcast_progress(progress: dict[str, Any] | None = None) -> None:
    _broadcast({"type": "progress", "data": progress or _task_snapshot()})


class _LogCapture(io.TextIOBase):
    def __init__(self, real_stdout: io.TextIOBase):
        self._real = real_stdout

    def write(self, content: str) -> int:
        if content and content.strip():
            _broadcast_log(content.rstrip("\n\r"))
        return self._real.write(content)

    def flush(self) -> None:
        self._real.flush()


def _parse_accounts() -> list[dict[str, Any]]:
    accounts: list[dict[str, Any]] = []
    if not ACCOUNTS_FILE.exists():
        return accounts

    with ACCOUNTS_FILE.open("r", encoding="utf-8") as file:
        for index, raw_line in enumerate(file):
            line = raw_line.strip()
            if not line:
                continue
            parts = line.split("----")
            accounts.append(
                {
                    "index": index,
                    "email": parts[0] if len(parts) > 0 else "",
                    "password": parts[1] if len(parts) > 1 else "",
                    "email_password": parts[2] if len(parts) > 2 else "",
                    "oauth_status": parts[3] if len(parts) > 3 else "",
                    "raw": line,
                }
            )
    return accounts


def _write_accounts(accounts: list[dict[str, Any]]) -> None:
    with ACCOUNTS_FILE.open("w", encoding="utf-8") as file:
        for account in accounts:
            file.write(account["raw"] + "\n")


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/health")
def health():
    return jsonify({"status": "ok", "time": _utc_now_iso(), "running": _task_running})


@app.route("/api/config", methods=["GET"])
def get_config():
    return jsonify(_read_config())


@app.route("/api/config", methods=["POST"])
def save_config():
    _write_config(_json_body())
    return jsonify({"ok": True})


@app.route("/api/start", methods=["POST"])
def start_task():
    global _task_running, _task_thread

    with _task_lock:
        if _task_running:
            return jsonify({"ok": False, "error": "任务正在运行中"}), 409
        _task_running = True

    body = _json_body()
    count = _safe_positive_int(body.get("count"), 1)
    workers = _safe_positive_int(body.get("workers"), 1)
    proxy = (body.get("proxy") or "").strip() or None

    _task_stop_event.clear()
    _reset_task_progress(count)

    def _run() -> None:
        global _task_running
        real_stdout = sys.stdout
        original_register = None

        try:
            sys.stdout = _LogCapture(real_stdout)
            os.environ["DATA_DIR"] = str(DATA_DIR)

            import importlib
            import config_loader

            importlib.reload(config_loader)
            original_register = config_loader._register_one

            def _patched_register_one(idx, total, task_proxy, output_file):
                if _task_stop_event.is_set():
                    with _task_lock:
                        _task_progress["done"] += 1
                        _task_progress["stopped"] += 1
                    _broadcast_progress()
                    _broadcast_log(f"⚠️ [{idx}/{total}] 已跳过（任务已停止）")
                    return False, None, "stopped"

                result = original_register(idx, total, task_proxy, output_file)
                ok = result[0] if result else False
                with _task_lock:
                    _task_progress["done"] += 1
                    if ok:
                        _task_progress["success"] += 1
                    else:
                        err = result[2] if result and len(result) > 2 else ""
                        if str(err).lower() == "stopped":
                            _task_progress["stopped"] += 1
                        else:
                            _task_progress["fail"] += 1
                _broadcast_progress()
                return result

            config_loader._register_one = _patched_register_one
            config_loader.run_batch(
                total_accounts=count,
                output_file=str(ACCOUNTS_FILE),
                max_workers=workers,
                proxy=proxy,
            )
        except Exception as exc:
            _broadcast_log(f"❌ 任务异常: {exc}")
        finally:
            if original_register is not None:
                config_loader._register_one = original_register
            sys.stdout = real_stdout
            with _task_lock:
                _task_running = False
            _broadcast({"type": "done", "progress": _task_snapshot()})

    _task_thread = threading.Thread(target=_run, daemon=True)
    _task_thread.start()
    return jsonify({"ok": True})


@app.route("/api/stop", methods=["POST"])
def stop_task():
    _task_stop_event.set()
    _broadcast_log("⚠️ 收到停止指令，将在当前账号完成后停止")
    return jsonify({"ok": True})


@app.route("/api/status", methods=["GET"])
def task_status():
    return jsonify({"running": _task_running, "progress": _task_snapshot()})


@app.route("/api/logs")
def sse_logs():
    subscriber: queue.Queue[str] = queue.Queue(maxsize=500)
    with _log_lock:
        _log_subscribers.append(subscriber)

    def stream():
        yield f"data: {json.dumps({'type': 'progress', 'data': _task_snapshot()}, ensure_ascii=False)}\n\n"
        try:
            while True:
                try:
                    raw = subscriber.get(timeout=30)
                    yield f"data: {raw}\n\n"
                except queue.Empty:
                    yield ": keepalive\n\n"
        except GeneratorExit:
            pass
        finally:
            with _log_lock:
                if subscriber in _log_subscribers:
                    _log_subscribers.remove(subscriber)

    return Response(stream(), mimetype="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/api/accounts", methods=["GET"])
def list_accounts():
    return jsonify(_parse_accounts())


@app.route("/api/accounts", methods=["DELETE"])
def delete_accounts():
    body = _json_body()
    indices = set(body.get("indices", []))
    mode = body.get("mode", "selected")

    accounts = _parse_accounts()
    if mode == "all":
        _write_accounts([])
        return jsonify({"ok": True, "deleted": len(accounts)})

    remaining = [account for account in accounts if account["index"] not in indices]
    _write_accounts(remaining)
    return jsonify({"ok": True, "deleted": len(accounts) - len(remaining)})


@app.route("/api/export", methods=["POST"])
def export_oauth():
    body = _json_body()
    mode = body.get("mode", "all")
    indices = set(body.get("indices", []))

    if not TOKEN_DIR.is_dir():
        return jsonify({"error": "codex_tokens 目录不存在"}), 404

    if mode == "selected":
        accounts = _parse_accounts()
        target_emails = {account["email"] for account in accounts if account["index"] in indices}
    else:
        target_emails = None

    buffer = io.BytesIO()
    exported = 0

    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for token_path in sorted(TOKEN_DIR.glob("*.json")):
            try:
                content = token_path.read_text(encoding="utf-8")
            except OSError:
                continue
            if target_emails is not None:
                stem = token_path.stem
                if not any(email in stem or email in content for email in target_emails):
                    continue
            archive.writestr(token_path.name, content)
            exported += 1

    if exported == 0:
        return jsonify({"error": "没有找到匹配的 Token 文件"}), 404

    buffer.seek(0)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return send_file(buffer, mimetype="application/zip", as_attachment=True, download_name=f"codex_tokens_{timestamp}.zip")


@app.route("/api/datainfo")
def data_info():
    tracked_files = ["registered_accounts.txt", "registered_accounts.csv", "ak.txt", "rk.txt"]
    files = {}
    for name in tracked_files:
        path = DATA_DIR / name
        files[name] = {"exists": path.exists(), "size": path.stat().st_size if path.exists() else 0}

    token_count = len(list(TOKEN_DIR.glob("*.json"))) if TOKEN_DIR.is_dir() else 0
    return jsonify({"data_dir": str(DATA_DIR), "files": files, "token_count": token_count})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"🚀 ChatGPT 注册管理面板启动: http://0.0.0.0:{port}")
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
