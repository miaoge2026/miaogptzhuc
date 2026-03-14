"""
ChatGPT 批量注册工具 — Web 管理界面
Flask 后端: 配置管理 / 任务控制 / SSE 实时日志 / 账号管理 / OAuth 导出

优化点:
- 任务进度实时跟踪（通过猴补丁注入 progress 回调）
- 新增 /api/progress 独立接口
- 新增健康检查 /api/health
- 优化 SSE 流: 自动发送进度事件
- 数据目录与代码目录分离（/data）
- 支持环境变量 PORT / SECRET_KEY
- 所有文件路径统一到 DATA_DIR
"""

import os
import io
import csv
import json
import time
import queue
import zipfile
import threading
import sys
from datetime import datetime
from flask import Flask, request, jsonify, Response, render_template, send_file

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "changeme-in-production")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# 数据目录优先使用环境变量，默认 /data（Docker volume）
DATA_DIR = os.environ.get("DATA_DIR", os.path.join(BASE_DIR, "data"))
os.makedirs(DATA_DIR, exist_ok=True)

CONFIG_PATH      = os.path.join(DATA_DIR, "config.json")
ACCOUNTS_FILE    = os.path.join(DATA_DIR, "registered_accounts.txt")
ACCOUNTS_CSV     = os.path.join(DATA_DIR, "registered_accounts.csv")
AK_FILE          = os.path.join(DATA_DIR, "ak.txt")
RK_FILE          = os.path.join(DATA_DIR, "rk.txt")
TOKEN_DIR        = os.path.join(DATA_DIR, "codex_tokens")
INVITE_TRACKER   = os.path.join(DATA_DIR, "invite_tracker.json")

# 兼容旧配置文件位置（首次迁移）
_LEGACY_CONFIG = os.path.join(BASE_DIR, "config.json")
if not os.path.exists(CONFIG_PATH) and os.path.exists(_LEGACY_CONFIG):
    import shutil
    shutil.copy(_LEGACY_CONFIG, CONFIG_PATH)

# ── Task state ──────────────────────────────────────────────
_task_lock    = threading.Lock()
_task_running = False
_task_thread  = None
_task_stop_event = threading.Event()
_task_progress   = {"total": 0, "done": 0, "success": 0, "fail": 0, "stopped": 0, "started_at": None}

# ── SSE log broadcast ──────────────────────────────────────
_log_subscribers: list[queue.Queue] = []
_log_lock = threading.Lock()

def _broadcast(payload: dict):
    """广播任意 JSON 事件到所有 SSE 订阅者"""
    raw = json.dumps(payload, ensure_ascii=False)
    with _log_lock:
        dead = []
        for q in _log_subscribers:
            try:
                q.put_nowait(raw)
            except queue.Full:
                dead.append(q)
        for q in dead:
            _log_subscribers.remove(q)

def _broadcast_log(line: str):
    _broadcast({"type": "log", "msg": line})

def _broadcast_progress(progress: dict):
    _broadcast({"type": "progress", "data": progress})


class _LogCapture(io.TextIOBase):
    """捕获 print() 输出并广播到 SSE，同时写入真实 stdout"""
    def __init__(self, real_stdout):
        self._real = real_stdout

    def write(self, s):
        if s and s.strip():
            _broadcast_log(s.rstrip("\n\r"))
        return self._real.write(s)

    def flush(self):
        return self._real.flush()


# ── Config helpers ──────────────────────────────────────────
_DEFAULT_CONFIG = {
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

def _read_config() -> dict:
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return dict(_DEFAULT_CONFIG)

def _write_config(cfg: dict):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=4, ensure_ascii=False)


def _safe_positive_int(value, default: int, *, minimum: int = 1) -> int:
    """将输入安全转换为正整数，异常时回退默认值。"""
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, parsed)


# ── Account helpers ─────────────────────────────────────────
def _parse_accounts() -> list[dict]:
    accounts = []
    if not os.path.exists(ACCOUNTS_FILE):
        return accounts
    with open(ACCOUNTS_FILE, "r", encoding="utf-8") as f:
        for i, line in enumerate(f):
            line = line.strip()
            if not line:
                continue
            parts = line.split("----")
            accounts.append({
                "index": i,
                "email":          parts[0] if len(parts) > 0 else "",
                "password":       parts[1] if len(parts) > 1 else "",
                "email_password": parts[2] if len(parts) > 2 else "",
                "oauth_status":   parts[3] if len(parts) > 3 else "",
                "raw": line,
            })
    return accounts

def _write_accounts(accounts: list[dict]):
    with open(ACCOUNTS_FILE, "w", encoding="utf-8") as f:
        for acc in accounts:
            f.write(acc["raw"] + "\n")


# ═══════════════════════════  ROUTES  ═══════════════════════

@app.route("/")
def index():
    return render_template("index.html")


# ── Health ──────────────────────────────────────────────────
@app.route("/api/health")
def health():
    return jsonify({
        "status": "ok",
        "time": datetime.utcnow().isoformat() + "Z",
        "running": _task_running,
    })


# ── Config ──────────────────────────────────────────────────
@app.route("/api/config", methods=["GET"])
def get_config():
    return jsonify(_read_config())

@app.route("/api/config", methods=["POST"])
def save_config():
    cfg = request.get_json(force=True)
    _write_config(cfg)
    return jsonify({"ok": True})


# ── Task control ────────────────────────────────────────────
@app.route("/api/start", methods=["POST"])
def start_task():
    global _task_running, _task_thread, _task_progress

    with _task_lock:
        if _task_running:
            return jsonify({"ok": False, "error": "任务正在运行中"}), 409

    body    = request.get_json(force=True) or {}
    count   = _safe_positive_int(body.get("count"), 1)
    workers = _safe_positive_int(body.get("workers"), 1)
    proxy   = body.get("proxy", "").strip() or None

    _task_stop_event.clear()
    _task_progress = {
        "total": count, "done": 0,
        "success": 0, "fail": 0, "stopped": 0,
        "started_at": datetime.utcnow().isoformat() + "Z",
    }

    def _run():
        global _task_running
        real_stdout = sys.__stdout__
        sys.stdout = _LogCapture(real_stdout)

        try:
            import importlib
            # 注入数据目录路径到环境变量，让 config_loader 使用
            os.environ["DATA_DIR"] = DATA_DIR

            import config_loader
            importlib.reload(config_loader)

            # 猴补丁：注入进度回调
            _orig_register_one = config_loader._register_one

            def _patched_register_one(idx, total, proxy, output_file):
                if _task_stop_event.is_set():
                    with _task_lock:
                        _task_progress["done"] += 1
                        _task_progress["stopped"] += 1
                    _broadcast_progress(dict(_task_progress))
                    _broadcast_log(f"⚠️ [{idx}/{total}] 已跳过（任务已停止）")
                    return False, None, "stopped"

                result = _orig_register_one(idx, total, proxy, output_file)
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
                _broadcast_progress(dict(_task_progress))
                return result

            config_loader._register_one = _patched_register_one

            config_loader.run_batch(
                total_accounts=count,
                output_file=ACCOUNTS_FILE,
                max_workers=workers,
                proxy=proxy,
            )
        except Exception as e:
            _broadcast_log(f"❌ 任务异常: {e}")
        finally:
            sys.stdout = real_stdout
            with _task_lock:
                _task_running = False
            _broadcast({"type": "done", "progress": dict(_task_progress)})

    _task_running = True
    _task_thread  = threading.Thread(target=_run, daemon=True)
    _task_thread.start()
    return jsonify({"ok": True})


@app.route("/api/stop", methods=["POST"])
def stop_task():
    _task_stop_event.set()
    _broadcast_log("⚠️ 收到停止指令，将在当前账号完成后停止")
    return jsonify({"ok": True})


@app.route("/api/status", methods=["GET"])
def task_status():
    return jsonify({
        "running":  _task_running,
        "progress": _task_progress,
    })


# ── SSE Logs ────────────────────────────────────────────────
@app.route("/api/logs")
def sse_logs():
    q = queue.Queue(maxsize=500)
    with _log_lock:
        _log_subscribers.append(q)

    def stream():
        # 推送当前进度快照
        yield f"data: {json.dumps({'type': 'progress', 'data': _task_progress})}\n\n"
        try:
            while True:
                try:
                    raw = q.get(timeout=30)
                    yield f"data: {raw}\n\n"
                except queue.Empty:
                    yield ": keepalive\n\n"
        except GeneratorExit:
            pass
        finally:
            with _log_lock:
                if q in _log_subscribers:
                    _log_subscribers.remove(q)

    return Response(
        stream(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── Accounts ────────────────────────────────────────────────
@app.route("/api/accounts", methods=["GET"])
def list_accounts():
    return jsonify(_parse_accounts())


@app.route("/api/accounts", methods=["DELETE"])
def delete_accounts():
    body    = request.get_json(force=True) or {}
    indices = set(body.get("indices", []))
    mode    = body.get("mode", "selected")

    accounts = _parse_accounts()
    if mode == "all":
        _write_accounts([])
        return jsonify({"ok": True, "deleted": len(accounts)})

    remaining = [a for a in accounts if a["index"] not in indices]
    _write_accounts(remaining)
    return jsonify({"ok": True, "deleted": len(accounts) - len(remaining)})


# ── OAuth Export ────────────────────────────────────────────
@app.route("/api/export", methods=["POST"])
def export_oauth():
    body    = request.get_json(force=True) or {}
    mode    = body.get("mode", "all")
    indices = set(body.get("indices", []))

    token_dir = TOKEN_DIR
    if not os.path.isdir(token_dir):
        return jsonify({"error": "codex_tokens 目录不存在"}), 404

    if mode == "selected":
        accounts      = _parse_accounts()
        target_emails = {a["email"] for a in accounts if a["index"] in indices}
    else:
        target_emails = None

    buf      = io.BytesIO()
    exported = 0

    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for fname in sorted(os.listdir(token_dir)):
            if not fname.endswith(".json"):
                continue
            fpath = os.path.join(token_dir, fname)
            try:
                content = open(fpath, "r", encoding="utf-8").read()
            except Exception:
                continue
            if target_emails is not None:
                stem    = fname[:-5]
                matched = any(em in stem or em in content for em in target_emails)
                if not matched:
                    continue
            zf.writestr(fname, content)
            exported += 1

    if exported == 0:
        return jsonify({"error": "没有找到匹配的 Token 文件"}), 404

    buf.seek(0)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return send_file(
        buf,
        mimetype="application/zip",
        as_attachment=True,
        download_name=f"codex_tokens_{ts}.zip",
    )


# ── Data directory info ─────────────────────────────────────
@app.route("/api/datainfo")
def data_info():
    files = {}
    for name in ["registered_accounts.txt", "registered_accounts.csv", "ak.txt", "rk.txt"]:
        p = os.path.join(DATA_DIR, name)
        files[name] = {
            "exists": os.path.exists(p),
            "size": os.path.getsize(p) if os.path.exists(p) else 0,
        }
    token_count = 0
    if os.path.isdir(TOKEN_DIR):
        token_count = len([f for f in os.listdir(TOKEN_DIR) if f.endswith(".json")])
    return jsonify({"data_dir": DATA_DIR, "files": files, "token_count": token_count})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"🚀 ChatGPT 注册管理面板启动: http://0.0.0.0:{port}")
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
