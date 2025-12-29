from starlette.applications import Starlette
from starlette.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates
from starlette.responses import HTMLResponse, JSONResponse
from starlette.responses import StreamingResponse, Response
from starlette.requests import Request
from app.config import CONFIG
from app.database import Base, engine, SessionLocal
from sqlalchemy import text
from app.telegram_client import multi_manager
from sqlalchemy.orm import Session
from app.models import SendLog, Task, TaskEvent
from app.services.send_service import send_to_groups
from app.services.group_service import get_groups, clear_group_cache
# from app.services.multi_account_sender import get_multi_sender  # 暂不使用
from app.routers.accounts import check_single_account, delete_account
from app.routers.system import reset_system
from app.services.account_service import account_service
import json
import time
import uuid
import asyncio
import os
import random
import logging
from datetime import datetime, timedelta

app = Starlette()

templates = Jinja2Templates(directory="templates")
app.mount("/static", StaticFiles(directory="static"), name="static")

def _discover_session_accounts() -> list[str]:
    session_dir = CONFIG.SESSION_DIR
    try:
        items = os.listdir(session_dir)
    except Exception:
        return []
    names: list[str] = []
    for filename in items:
        if not filename.endswith(".session"):
            continue
        if filename.endswith(".session-journal"):
            continue
        name = filename[:-8]
        if not name:
            continue
        names.append(name)
    return sorted(set(names))


@app.middleware("http")
async def admin_token_middleware(request, call_next):
    token = request.headers.get("X-Admin-Token")
    request.state.admin_token = token
    response = await call_next(request)
    return response


@app.route("/")
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

@app.route("/api/accounts/status")
async def list_accounts_status(request: Request):
    token = request.headers.get("X-Admin-Token")
    if token != CONFIG.ADMIN_TOKEN:
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)
    count = getattr(CONFIG, "ACCOUNT_COUNT", 100)
    prefix = getattr(CONFIG, "ACCOUNT_PREFIX", "account")
    prefix_names = [f"{prefix}_{i:02d}" for i in range(1, count + 1)]
    prefix_set = set(prefix_names)
    extra_names = [n for n in _discover_session_accounts() if n not in prefix_set]
    names = prefix_names + extra_names
    data = []
    
    session_dir = CONFIG.SESSION_DIR
    for name in names:
        # Optimization: Check file existence instead of connecting to Telegram
        # This is much faster for large number of accounts
        session_path = os.path.join(session_dir, f"{name}.session")
        authorized = os.path.exists(session_path)
        # Always append the account, regardless of authorization status
        data.append({"account": name, "authorized": authorized})
    return JSONResponse(data)


@app.route("/api/accounts/authorized-list")
async def list_authorized_accounts(request: Request):
    token = request.headers.get("X-Admin-Token")
    if token != CONFIG.ADMIN_TOKEN:
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)
    session_dir = CONFIG.SESSION_DIR
    db: Session = SessionLocal()
    running_counts: dict[str, int] = {}
    try:
        rows = db.query(Task).filter(Task.status == "running").all()
        for t in rows:
            name = (t.account_name or "").strip()
            if not name:
                continue
            running_counts[name] = running_counts.get(name, 0) + 1
    finally:
        db.close()
    names = _discover_session_accounts()
    data: list[dict] = []
    seen = set()
    for name in names:
        seen.add(name)
        session_path = os.path.join(session_dir, f"{name}.session")
        authorized = os.path.exists(session_path)
        cnt = int(running_counts.get(name, 0))
        data.append(
            {
                "account": name,
                "authorized": bool(authorized),
                "running_tasks": cnt,
                "has_running_tasks": cnt > 0,
            }
        )
    for name, cnt in running_counts.items():
        if name in seen:
            continue
        data.append(
            {
                "account": name,
                "authorized": False,
                "running_tasks": int(cnt),
                "has_running_tasks": True,
            }
        )
    return JSONResponse(data)

app.add_route("/api/accounts/check-single", check_single_account, methods=["POST"])
app.add_route("/api/accounts/delete", delete_account, methods=["POST"])
app.add_route("/api/system/reset", reset_system, methods=["POST"])


@app.route("/api/accounts/upload-sessions", methods=["POST"])
async def upload_sessions(request: Request):
    """上传 session 文件"""
    token = request.headers.get("X-Admin-Token")
    if token != CONFIG.ADMIN_TOKEN:
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)
    
    form = await request.form()
    files = form.getlist("files")
    
    if not files:
        return JSONResponse({"detail": "No files uploaded"}, status_code=400)
    
    uploaded = 0
    errors = []
    validated_accounts = []
    
    for file in files:
        try:
            filename = file.filename
            if not filename.endswith('.session'):
                errors.append(f"{filename}: 不是 .session 文件")
                continue
            
            # 提取账号名 (去掉 .session 后缀)
            account_name = filename[:-8]  # Remove .session
            target_path = os.path.join(CONFIG.SESSION_DIR, filename)
            
            # 保存文件
            content = await file.read()
            with open(target_path, 'wb') as f:
                f.write(content)
            
            # 断开旧连接（如果存在）
            if account_name in multi_manager.managers:
                try:
                    await multi_manager.managers[account_name].disconnect()
                    logging.info(f"[UPLOAD] Disconnected old session for {account_name}")
                except Exception:
                    pass
            
            # 简单验证：只检查文件大小，不实际连接（避免数据库操作）
            try:
                import os as os_module
                file_size = os_module.path.getsize(target_path)
                if file_size > 5000:  # Session 文件通常大于 5KB
                    validated_accounts.append(account_name)
                    logging.info(f"[UPLOAD] Session file uploaded for {account_name} ({file_size} bytes)")
                else:
                    errors.append(f"{account_name}: 文件太小，可能损坏 ({file_size} bytes)")
            except Exception as e:
                errors.append(f"{account_name}: 文件检查失败 - {str(e)[:50]}")
            
            uploaded += 1
        except Exception as e:
            errors.append(f"{file.filename}: {str(e)}")
    
    return JSONResponse({
        "uploaded": uploaded,
        "validated": len(validated_accounts),
        "validated_accounts": validated_accounts,
        "errors": errors if errors else None
    })


@app.route("/api/accounts/bulk-delete", methods=["POST"])
async def bulk_delete_accounts(request: Request):
    token = request.headers.get("X-Admin-Token")
    if token != CONFIG.ADMIN_TOKEN:
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"detail": "Invalid JSON"}, status_code=400)
    accounts = body.get("accounts") or []
    if not isinstance(accounts, list):
        return JSONResponse({"detail": "accounts must be a list"}, status_code=400)
    cleaned: list[str] = []
    for a in accounts:
        if not isinstance(a, str):
            a = str(a)
        name = a.strip()
        if name and name not in cleaned:
            cleaned.append(name)
    if not cleaned:
        return JSONResponse({"detail": "no_accounts"}, status_code=400)
    db: Session = SessionLocal()
    results = []
    try:
        for name in cleaned:
            stopped = 0
            try:
                rows = db.query(Task).filter(Task.account_name == name, Task.status == "running").all()
                for t in rows:
                    t.stop_requested = 1
                    t.status = "stopped"
                    t.finished_at = CONFIG.now()
                    db.add(
                        TaskEvent(
                            task_id=t.id,
                            event="account_deleted",
                            detail="account_session_deleted_by_admin",
                            meta_json=json.dumps({"account": name}, ensure_ascii=False),
                        )
                    )
                if rows:
                    db.commit()
                stopped = len(rows)
            except Exception:
                db.rollback()
            deleted = False
            error = None
            try:
                if name in multi_manager.managers:
                    try:
                        await multi_manager.managers[name].disconnect()
                    except Exception:
                        pass
                deleted = await account_service.delete_session(name)
            except Exception as e:
                error = str(e)[:200]
            results.append(
                {
                    "account": name,
                    "deleted": bool(deleted),
                    "tasks_stopped": stopped,
                    "error": error,
                }
            )
        return JSONResponse({"results": results})
    finally:
        db.close()

@app.route("/api/accounts/assign-sequence", methods=["POST"])
async def assign_sequence(request: Request):
    token = request.headers.get("X-Admin-Token")
    if token != CONFIG.ADMIN_TOKEN:
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"detail": "Invalid JSON"}, status_code=400)

    accounts = body.get("accounts") or []
    if not isinstance(accounts, list) or not all(isinstance(a, str) for a in accounts):
        return JSONResponse({"detail": "accounts must be a list of strings"}, status_code=400)

    action = (body.get("action") or "copy").strip().lower()
    overwrite = bool(body.get("overwrite", False))
    if action not in ("copy", "move"):
        return JSONResponse({"detail": "action must be copy|move"}, status_code=400)

    count = int(getattr(CONFIG, "ACCOUNT_COUNT", 100))
    prefix = getattr(CONFIG, "ACCOUNT_PREFIX", "account")
    session_dir = CONFIG.SESSION_DIR

    targets = [f"{prefix}_{i:02d}" for i in range(1, count + 1)]
    available_targets: list[str] = []
    for t in targets:
        dst_path = os.path.join(session_dir, f"{t}.session")
        if overwrite or not os.path.exists(dst_path):
            available_targets.append(t)

    assigned = []
    skipped = []
    errors = []

    idx = 0
    for src in accounts:
        src_name = src.strip()
        if not src_name:
            continue
        src_path = os.path.join(session_dir, f"{src_name}.session")
        if not os.path.exists(src_path):
            errors.append(f"{src_name}: session_not_found")
            continue
        if idx >= len(available_targets):
            skipped.append(src_name)
            continue

        dst_name = available_targets[idx]
        idx += 1
        dst_path = os.path.join(session_dir, f"{dst_name}.session")

        if not overwrite and os.path.exists(dst_path):
            skipped.append(src_name)
            continue

        try:
            with open(src_path, "rb") as f:
                data = f.read()
            with open(dst_path, "wb") as f:
                f.write(data)

            src_journal = f"{src_path}-journal"
            dst_journal = f"{dst_path}-journal"
            if os.path.exists(src_journal):
                with open(src_journal, "rb") as f:
                    jdata = f.read()
                with open(dst_journal, "wb") as f:
                    f.write(jdata)

            if action == "move":
                try:
                    os.remove(src_path)
                except Exception:
                    pass
                if os.path.exists(src_journal):
                    try:
                        os.remove(src_journal)
                    except Exception:
                        pass

            if dst_name in multi_manager.managers:
                try:
                    await multi_manager.managers[dst_name].disconnect()
                except Exception:
                    pass

            assigned.append({"from": src_name, "to": dst_name})
        except Exception as e:
            errors.append(f"{src_name} -> {dst_name}: {type(e).__name__}: {str(e)}")

    return JSONResponse({
        "ok": True,
        "assigned": assigned,
        "skipped": skipped,
        "errors": errors,
        "available_slots": len(available_targets),
    })


@app.route("/api/groups")
async def list_groups(request: Request):
    token = request.headers.get("X-Admin-Token")
    if token != CONFIG.ADMIN_TOKEN:
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)
    only_groups = request.query_params.get("only_groups", "true").lower() != "false"
    account = request.query_params.get("account") or CONFIG.DEFAULT_ACCOUNT
    refresh = request.query_params.get("refresh", "false").lower() in ("1", "true", "yes")
    if getattr(CONFIG, "GROUP_CACHE_ENABLED", 1) == 0:
        refresh = True
    try:
        authorized = await multi_manager.is_authorized(account)
    except Exception:
        authorized = False
    if not authorized:
        return JSONResponse({"detail": "session_not_authorized"}, status_code=403)
    db: Session = SessionLocal()
    try:
        data = await get_groups(multi_manager, account=account, only_groups=only_groups, refresh=refresh, db=db)
        return JSONResponse(data)
    except asyncio.CancelledError:
        return JSONResponse({"detail": "request_cancelled"}, status_code=499)
    except BaseException as e:
        msg = str(e).lower()
        if "not authorized" in msg or "session" in msg:
            return JSONResponse({"detail": "session_not_authorized"}, status_code=403)
        return JSONResponse({"detail": "internal_error"}, status_code=500)
    finally:
        db.close()

@app.route("/api/groups/debug")
async def debug_groups(request: Request):
    token = request.headers.get("X-Admin-Token")
    if token != CONFIG.ADMIN_TOKEN:
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)
    only_groups = request.query_params.get("only_groups", "true").lower() != "false"
    account = request.query_params.get("account") or CONFIG.DEFAULT_ACCOUNT
    info = {
        "account": account,
        "only_groups": only_groups,
        "authorized": False,
        "dialogs_count": 0,
        "groups_count": 0,
        "sample": [],
        "session_path": None,
    }
    try:
        acc_cfg = CONFIG.ACCOUNTS.get(account)
        if acc_cfg:
            import os as _os
            info["session_path"] = _os.path.join(CONFIG.SESSION_DIR, acc_cfg.get("session_name"))
    except Exception:
        pass
    try:
        authorized = await multi_manager.is_authorized(account)
        info["authorized"] = bool(authorized)
        if not authorized:
            return JSONResponse(info)
        await multi_manager.ensure_connected(account)
        client = multi_manager.get(account).client
        dialogs = await client.get_dialogs()
        info["dialogs_count"] = len(dialogs)
        sample = []
        from telethon.tl.types import Channel, Chat
        for d in dialogs[:10]:
            e = d.entity
            if isinstance(e, Chat):
                sample.append({"id": e.id, "title": d.name, "type": "chat"})
            elif isinstance(e, Channel):
                is_megagroup = bool(getattr(e, "megagroup", False))
                if only_groups and not is_megagroup:
                    continue
                sample.append({"id": e.id, "title": d.name, "type": "channel", "megagroup": is_megagroup})
        info["sample"] = sample
        info["groups_count"] = len(sample)
        return JSONResponse(info)
    except asyncio.CancelledError:
        return JSONResponse({**info, "detail": "request_cancelled"}, status_code=499)
    except Exception as e:
        return JSONResponse({**info, "detail": str(e)}, status_code=200)

@app.route("/api/groups/cache/clear")
async def clear_groups_cache(request: Request):
    token = request.headers.get("X-Admin-Token")
    if token != CONFIG.ADMIN_TOKEN:
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)
    account = request.query_params.get("account")
    only_groups_param = request.query_params.get("only_groups")
    only_groups = None
    if only_groups_param is not None:
        only_groups = only_groups_param.lower() != "false"
    db: Session = SessionLocal()
    try:
        resp = clear_group_cache(account=account, only_groups=only_groups, db=db)
        return JSONResponse({"ok": True, **resp})
    finally:
        db.close()


@app.route("/api/send", methods=["POST"])
async def send(request: Request):
    token = request.headers.get("X-Admin-Token")
    if token != CONFIG.ADMIN_TOKEN:
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)
    body = await request.json()
    group_ids = body.get("group_ids") or []
    message = (body.get("message") or "").strip()
    parse_mode = body.get("parse_mode") or "plain"
    disable_web_page_preview = bool(body.get("disable_web_page_preview", True))
    delay_ms = int(body.get("delay_ms", 11000))  # 默认 11 秒
    retry_max = int(body.get("retry_max", getattr(CONFIG, "SEND_RETRY_MAX", 0)))
    retry_delay_ms = int(body.get("retry_delay_ms", getattr(CONFIG, "SEND_RETRY_DELAY_MS", 1500)))
    account = body.get("account") or CONFIG.DEFAULT_ACCOUNT
    request_id = body.get("request_id")
    ok, reason = _check_request_guard(token, request_id)
    if not ok:
        return JSONResponse({"detail": "Too Many Requests"}, status_code=429, headers={"Retry-After": "1"})
    try:
        authorized = await multi_manager.is_authorized(account)
    except Exception:
        authorized = False
    if not authorized:
        return JSONResponse({"detail": "session_not_authorized"}, status_code=403)
    if not group_ids or not message:
        return JSONResponse({"detail": "group_ids and message required"}, status_code=400)
    db: Session = SessionLocal()
    try:
        resp = await send_to_groups(multi_manager, db, account, group_ids, message, parse_mode, disable_web_page_preview, delay_ms, retry_max, retry_delay_ms)
        return JSONResponse(resp)
    finally:
        db.close()


@app.route("/api/test-send", methods=["POST"])
async def test_send(request: Request):
    token = request.headers.get("X-Admin-Token")
    if token != CONFIG.ADMIN_TOKEN:
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)
    body = await request.json()
    group_ids = body.get("group_ids") or []
    message = (body.get("message") or "").strip()
    parse_mode = body.get("parse_mode") or "plain"
    disable_web_page_preview = bool(body.get("disable_web_page_preview", True))
    account = body.get("account") or CONFIG.DEFAULT_ACCOUNT
    retry_max = int(body.get("retry_max", getattr(CONFIG, "SEND_RETRY_MAX", 0)))
    retry_delay_ms = int(body.get("retry_delay_ms", getattr(CONFIG, "SEND_RETRY_DELAY_MS", 1500)))
    request_id = body.get("request_id")
    ok, reason = _check_request_guard(token, request_id)
    if not ok:
        return JSONResponse({"detail": "Too Many Requests"}, status_code=429, headers={"Retry-After": "1"})
    try:
        authorized = await multi_manager.is_authorized(account)
    except Exception:
        authorized = False
    if not authorized:
        return JSONResponse({"detail": "session_not_authorized"}, status_code=403)
    if not group_ids or not message:
        return JSONResponse({"detail": "group_ids and message required"}, status_code=400)
    db: Session = SessionLocal()
    try:
        resp = await send_to_groups(multi_manager, db, account, group_ids, message, parse_mode, disable_web_page_preview, 0, retry_max, retry_delay_ms)
        return JSONResponse(resp)
    finally:
        db.close()


@app.route("/api/logs")
async def recent_logs(request: Request):
    token = request.headers.get("X-Admin-Token")
    if token != CONFIG.ADMIN_TOKEN:
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)
    limit = int(request.query_params.get("limit", 50))
    db: Session = SessionLocal()
    try:
        rows = (
            db.query(SendLog)
            .order_by(SendLog.created_at.desc())
            .limit(limit)
            .all()
        )
        data = [
            {
                "id": r.id,
                "group_id": r.group_id,
                "group_title": r.group_title,
                "message_preview": r.message_preview,
                "status": r.status,
                "error": r.error,
                "message_id": getattr(r, "message_id", None),
                "parse_mode": getattr(r, "parse_mode", None),
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ]
        return JSONResponse(data)
    finally:
        db.close()


@app.route("/api/logs/export.csv")
async def export_logs_csv(request: Request):
    token = request.headers.get("X-Admin-Token")
    if token != CONFIG.ADMIN_TOKEN:
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)
    limit = int(request.query_params.get("limit", 1000))
    status_filter = request.query_params.get("status")
    db: Session = SessionLocal()
    try:
        q = db.query(SendLog).order_by(SendLog.created_at.desc())
        if status_filter:
            q = q.filter(SendLog.status == status_filter)
        rows = q.limit(limit).all()
        import csv
        from io import StringIO
        buf = StringIO()
        writer = csv.writer(buf)
        writer.writerow(["id","account_name","group_id","group_title","message_preview","status","error","message_id","parse_mode","created_at"])
        for r in rows:
            writer.writerow([
                r.id,
                r.account_name,
                r.group_id,
                r.group_title,
                (r.message_preview or "").replace("\n"," ").strip(),
                r.status,
                (r.error or "").replace("\n"," ").strip(),
                getattr(r, "message_id", None),
                getattr(r, "parse_mode", None),
                r.created_at.isoformat() if r.created_at else "",
            ])
        csv_data = buf.getvalue()
        headers = {"Content-Type": "text/csv; charset=utf-8", "Content-Disposition": "attachment; filename=send_logs.csv"}
        return Response(content=csv_data, media_type="text/csv", headers=headers)
    finally:
        db.close()


async def startup_event():
    Base.metadata.create_all(bind=engine)
    try:
        with engine.connect() as conn:
            conn.execute(text("PRAGMA journal_mode=WAL;"))
            cols = conn.execute(text("PRAGMA table_info('send_logs')")).fetchall()
            names = {c[1] for c in cols}
            if 'account_name' not in names:
                conn.execute(text("ALTER TABLE send_logs ADD COLUMN account_name VARCHAR(64)"))
            if 'message_id' not in names:
                conn.execute(text("ALTER TABLE send_logs ADD COLUMN message_id INTEGER"))
            if 'parse_mode' not in names:
                conn.execute(text("ALTER TABLE send_logs ADD COLUMN parse_mode VARCHAR(16)"))

            task_cols = conn.execute(text("PRAGMA table_info('tasks')")).fetchall()
            task_names = {c[1] for c in task_cols}
            if 'rounds' not in task_names:
                conn.execute(text("ALTER TABLE tasks ADD COLUMN rounds INTEGER DEFAULT 1"))
            if 'current_round' not in task_names:
                conn.execute(text("ALTER TABLE tasks ADD COLUMN current_round INTEGER DEFAULT 1"))
            if 'round_interval_s' not in task_names:
                conn.execute(text("ALTER TABLE tasks ADD COLUMN round_interval_s INTEGER DEFAULT 0"))
            if 'next_round_at' not in task_names:
                conn.execute(text("ALTER TABLE tasks ADD COLUMN next_round_at DATETIME"))
            conn.commit()
    except Exception:
        pass
    try:
        import os as _os
        import shutil as _shutil
        _os.makedirs(CONFIG.SESSION_DIR, exist_ok=True)
        for _name, _cfg in CONFIG.ACCOUNTS.items():
            _sn = _cfg.get("session_name")
            if not _sn:
                continue
            _target = _os.path.join(CONFIG.SESSION_DIR, _sn + ".session")
            if not _os.path.exists(_target):
                _src = _os.path.join(".", _sn + ".session")
                if _os.path.exists(_src):
                    try:
                        _shutil.copy2(_src, _target)
                    except Exception:
                        pass
    except Exception:
        pass

    # 设置 account_service 的 manager 引用（用于健康检查）
    account_service.set_manager(multi_manager)
    
    # 启动定期清理空闲连接的后台任务
    asyncio.create_task(_periodic_connection_cleanup())

    db: Session = SessionLocal()
    try:
        rows = db.query(Task).filter(Task.status == "running").limit(100).all()
        for t in rows:
            try:
                gids = json.loads(t.group_ids_json or "[]")
                start_idx = max(0, (t.current_index or 0))
                rem = gids[start_idx:]
                if rem:
                    asyncio.create_task(_run_send_task(t.id, t.account_name, rem, t.message, t.parse_mode, bool(t.disable_web_page_preview), t.delay_ms, t.rounds or 1, t.round_interval_s or 0))
            except Exception:
                pass
    finally:
        db.close()


_REQ_IDS: dict[str, float] = {}
_LAST_TS: dict[str, float] = {}


async def _periodic_connection_cleanup():
    """定期清理空闲的 Telegram 连接以节省内存"""
    while True:
        try:
            await asyncio.sleep(60)  # 每分钟检查一次
            await multi_manager._cleanup_idle_connections()
        except Exception as e:
            print(f"[CLEANUP] Error: {e}")


def _check_request_guard(token: str, request_id: str | None, window_ms: int = 500):
    now = time.monotonic()
    # prune old ids
    for k, ts in list(_REQ_IDS.items()):
        if now - ts > 60:
            del _REQ_IDS[k]
    # duplicate id guard
    if request_id:
        if request_id in _REQ_IDS:
            return False, "duplicate"
        _REQ_IDS[request_id] = now
    # throttle by token
    last = _LAST_TS.get(token, 0.0)
    if now - last < (window_ms / 1000.0):
        _LAST_TS[token] = now
        return False, "too_frequent"
    _LAST_TS[token] = now
    return True, None

app.add_event_handler("startup", startup_event)

TASKS: dict[str, dict] = {}


@app.route("/api/send-async", methods=["POST"])
async def send_async(request: Request):
    token = request.headers.get("X-Admin-Token")
    if token != CONFIG.ADMIN_TOKEN:
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)
    body = await request.json()
    group_ids = body.get("group_ids") or []
    message = (body.get("message") or "").strip()
    parse_mode = body.get("parse_mode") or "plain"
    disable_web_page_preview = bool(body.get("disable_web_page_preview", True))
    delay_ms = int(body.get("delay_ms", 11000))  # 默认 11 秒
    delay_ms = max(delay_ms, getattr(CONFIG, "SEND_MIN_DELAY_MS", 1500))
    rounds = int(body.get("rounds", 30))
    round_interval_s = int(body.get("round_interval_s", 600))
    account = body.get("account") or CONFIG.DEFAULT_ACCOUNT
    request_id = body.get("request_id")
    ok, reason = _check_request_guard(token, request_id)
    if not ok:
        return JSONResponse({"detail": "Too Many Requests"}, status_code=429, headers={"Retry-After": "1"})
    try:
        authorized = await multi_manager.is_authorized(account)
    except Exception:
        authorized = False
    if not authorized:
        return JSONResponse({"detail": "session_not_authorized"}, status_code=403)
    if not group_ids or not message:
        return JSONResponse({"detail": "group_ids and message required"}, status_code=400)
    task_id = uuid.uuid4().hex[:24]
    db: Session = SessionLocal()
    try:
        t = Task(
            id=task_id,
            status="running",
            total=len(group_ids),
            success=0,
            failed=0,
            account_name=account,
            message=message,
            parse_mode=parse_mode,
            disable_web_page_preview=1 if disable_web_page_preview else 0,
            delay_ms=delay_ms,
            rounds=rounds,
            round_interval_s=round_interval_s,
            current_index=0,
            group_ids_json=json.dumps(group_ids),
            request_id=request_id,
        )
        db.add(t)
        db.add(TaskEvent(task_id=task_id, event="created", detail="task_created", meta_json=json.dumps({"count": len(group_ids)}, ensure_ascii=False)))
        db.commit()
    finally:
        db.close()
    asyncio.create_task(_run_send_task(task_id, account, group_ids, message, parse_mode, disable_web_page_preview, delay_ms, rounds, round_interval_s))
    return JSONResponse({"task_id": task_id})


@app.route("/api/task-status")
async def task_status(request: Request):
    token = request.headers.get("X-Admin-Token")
    if token != CONFIG.ADMIN_TOKEN:
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)
    task_id = request.query_params.get("task_id")
    if not task_id:
        return JSONResponse({"detail": "Not Found"}, status_code=404)
    db: Session = SessionLocal()
    try:
        t = db.query(Task).filter(Task.id == task_id).first()
        if not t:
            return JSONResponse({"detail": "Not Found"}, status_code=404)
        data = {
            "task_id": t.id,
            "status": t.status,
            "total": t.total,
            "success": t.success,
            "failed": t.failed,
            "current_index": t.current_index,
            "started_at": t.started_at.isoformat() if t.started_at else None,
            "finished_at": t.finished_at.isoformat() if t.finished_at else None,
            "rounds": t.rounds,
            "current_round": t.current_round,
            "round_interval_s": t.round_interval_s,
            "next_round_at": t.next_round_at.isoformat() if t.next_round_at else None,
        }
        return JSONResponse(data)
    finally:
        db.close()

@app.route("/api/tasks/summary")
async def tasks_summary(request: Request):
    token = request.headers.get("X-Admin-Token")
    if token != CONFIG.ADMIN_TOKEN:
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)
    db: Session = SessionLocal()
    try:
        rows = db.query(Task).filter(Task.status == "running").all()
        acc_map: dict[str, dict] = {}
        for t in rows:
            k = t.account_name
            v = acc_map.get(k) or {"account": k, "tasks_count": 0, "total": 0, "success": 0, "failed": 0, "current_round": 0, "rounds": 0, "last_updated_at": None}
            v["tasks_count"] += 1
            v["total"] += int(t.total or 0)
            v["success"] += int(t.success or 0)
            v["failed"] += int(t.failed or 0)
            v["current_round"] = max(int(v["current_round"] or 0), int(t.current_round or 0))
            v["rounds"] = max(int(v["rounds"] or 0), int(t.rounds or 0))
            ts = t.heartbeat_at or t.started_at
            if ts:
                cur = v["last_updated_at"]
                if (not cur) or (ts > cur):
                    v["last_updated_at"] = ts
            acc_map[k] = v
        data = []
        for _, e in acc_map.items():
            if e.get("last_updated_at"):
                e["last_updated_at"] = e["last_updated_at"].isoformat()
            data.append(e)
        return JSONResponse(data)
    finally:
        db.close()


async def _run_send_task_with_delay(
    task_id: str,
    account: str,
    group_ids: list[int],
    message: str,
    parse_mode: str,
    disable_web_page_preview: bool,
    delay_ms: int,
    rounds: int,
    round_interval_s: int,
    start_delay: float = 0,
):
    """带延迟启动的发送任务包装器"""
    if start_delay > 0:
        print(f"[TASK] {account}: waiting {start_delay:.1f}s before starting...")
        await asyncio.sleep(start_delay)
    print(f"[TASK] {account}: starting send task (task_id={task_id[:8]}...)")
    await _run_send_task(task_id, account, group_ids, message, parse_mode, disable_web_page_preview, delay_ms, rounds, round_interval_s)


async def _run_send_task(task_id: str, account: str, group_ids: list[int], message: str, parse_mode: str, disable_web_page_preview: bool, delay_ms: int, rounds: int, round_interval_s: int):
    db: Session = SessionLocal()
    consecutive_failures = 0  # 连续失败计数器
    MAX_CONSECUTIVE_FAILURES = 10  # 最大连续失败次数
    
    try:
        for i in range(rounds):
            current_round = i + 1
            t = db.query(Task).filter(Task.id == task_id).first()
            if t:
                t.current_round = current_round
                t.current_index = 0 # Reset index for each round
                db.commit()

            delay = max(delay_ms, 0) / 1000.0
            ids = list(group_ids)
            random.shuffle(ids)
            for idx, gid in enumerate(ids):
                t = db.query(Task).filter(Task.id == task_id).first()
                if t and t.stop_requested:
                    t.status = "stopped"
                    t.finished_at = CONFIG.now()
                    db.add(TaskEvent(task_id=task_id, event="stopped", detail="task_stopped", meta_json=json.dumps({}, ensure_ascii=False)))
                    db.commit()
                    return  # Stop the entire task

                while t and t.paused:
                    await asyncio.sleep(1)
                    t = db.query(Task).filter(Task.id == task_id).first()

                ok, err, msg_id = await multi_manager.send_message_to_group(
                    account,
                    group_id=gid,
                    text=message,
                    parse_mode=parse_mode,
                    disable_web_page_preview=disable_web_page_preview,
                )
                status = "success" if ok else "failed"
                preview = message[:200]
                title = str(gid)
                try:
                    ent = await multi_manager.get(account).client.get_entity(gid)
                    title = getattr(ent, 'title', None) or getattr(ent, 'username', None) or getattr(ent, 'first_name', None) or str(gid)
                except Exception:
                    title = str(gid)

                log = SendLog(
                    account_name=account,
                    group_id=gid,
                    group_title=title,
                    message_preview=preview,
                    status=status,
                    error=None if ok else (err or "send_failed"),
                    message_id=msg_id,
                    parse_mode=parse_mode,
                )
                db.add(log)
                t = db.query(Task).filter(Task.id == task_id).first()
                if t:
                    if ok:
                        t.success = (t.success or 0) + 1
                        consecutive_failures = 0  # 成功则重置计数器
                    else:
                        t.failed = (t.failed or 0) + 1
                        consecutive_failures += 1  # 失败则增加计数器
                        
                        if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                            print(f"[AUTO-STOP] {account}: {consecutive_failures} consecutive failures, stopping task")
                            t.status = "error"
                            t.finished_at = CONFIG.now()
                            db.add(TaskEvent(
                                task_id=task_id, 
                                event="auto_stopped", 
                                detail=f"连续失败{consecutive_failures}次，自动停止任务",
                                meta_json=json.dumps({"consecutive_failures": consecutive_failures, "last_error": err}, ensure_ascii=False)
                            ))
                            db.commit()
                            return  # 停止任务
                            
                    t.current_index = (t.current_index or 0) + 1
                    t.heartbeat_at = CONFIG.now()
                    db.add(TaskEvent(task_id=task_id, event="progress", detail=f"{t.current_index}/{t.total}", meta_json=json.dumps({"gid": gid}, ensure_ascii=False)))
                db.commit()

                if delay > 0:
                    await asyncio.sleep(delay)

            if current_round < rounds:
                t = db.query(Task).filter(Task.id == task_id).first()
                if t:
                    t.next_round_at = CONFIG.now() + timedelta(seconds=round_interval_s)
                    db.commit()
                await asyncio.sleep(round_interval_s)

        t = db.query(Task).filter(Task.id == task_id).first()
        if t and t.status not in ("stopped", "error"):
            t.status = "done"
            t.finished_at = CONFIG.now()
            db.add(TaskEvent(task_id=task_id, event="finished", detail="task_done", meta_json=json.dumps({}, ensure_ascii=False)))
            db.commit()
    except Exception as e:
        t = db.query(Task).filter(Task.id == task_id).first()
        if t:
            t.status = "error"
            t.finished_at = CONFIG.now()
            db.add(TaskEvent(task_id=task_id, event="error", detail=f"task_error: {e}", meta_json=json.dumps({}, ensure_ascii=False)))
            db.commit()
    finally:
        db.close()

@app.route("/api/login/send-code", methods=["POST"])
async def login_send_code(request: Request):
    token = request.headers.get("X-Admin-Token")
    if token != CONFIG.ADMIN_TOKEN:
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)
    body = await request.json()
    account = body.get("account") or CONFIG.DEFAULT_ACCOUNT
    phone = body.get("phone")
    force_sms = body.get("force_sms", False)
    force_new_session = body.get("force_new_session", False)
    if not phone:
        return JSONResponse({"detail": "phone required"}, status_code=400)
    try:
        resp = await multi_manager.send_login_code(account, phone, force_sms=force_sms, force_new_session=force_new_session)
        return JSONResponse(resp)
    except Exception as e:
        return JSONResponse({"detail": str(e)}, status_code=500)


@app.route("/api/login/submit-code", methods=["POST"])
async def login_submit_code(request: Request):
    token = request.headers.get("X-Admin-Token")
    if token != CONFIG.ADMIN_TOKEN:
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)
    body = await request.json()
    code = body.get("code")
    phone = (body.get("phone") or "").strip()
    account = body.get("account") or CONFIG.DEFAULT_ACCOUNT
    if not phone or not code:
        return JSONResponse({"detail": "phone and code required"}, status_code=400)
    try:
        resp = await multi_manager.confirm_login(account, phone, code, body.get("password") or None)
        return JSONResponse(resp)
    except Exception as e:
        return JSONResponse({"detail": str(e)}, status_code=500)


@app.route("/api/account-status")
async def account_status(request: Request):
    token = request.headers.get("X-Admin-Token")
    if token != CONFIG.ADMIN_TOKEN:
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)
    account = request.query_params.get("account") or CONFIG.DEFAULT_ACCOUNT
    try:
        authorized = await multi_manager.is_authorized(account)
        return JSONResponse({"authorized": authorized})
    except Exception as e:
        return JSONResponse({"authorized": False, "detail": str(e)})


@app.route("/api/session/validate", methods=["POST"])
async def validate_session(request: Request):
    """验证上传的 session 文件是否有效（不会删除 session）"""
    token = request.headers.get("X-Admin-Token")
    if token != CONFIG.ADMIN_TOKEN:
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)
    body = await request.json()
    account = body.get("account")
    if not account:
        return JSONResponse({"detail": "account required"}, status_code=400)
    try:
        result = await multi_manager.validate_session(account)
        return JSONResponse({"account": account, **result})
    except Exception as e:
        return JSONResponse({"account": account, "valid": False, "error": str(e)})


@app.route("/api/session/validate-batch", methods=["POST"])
async def validate_sessions_batch(request: Request):
    """批量验证多个 session 文件"""
    token = request.headers.get("X-Admin-Token")
    if token != CONFIG.ADMIN_TOKEN:
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)
    body = await request.json()
    accounts = body.get("accounts") or []
    
    # 如果没有指定账号，则验证所有已存在的 session 文件
    if not accounts:
        session_dir = CONFIG.SESSION_DIR
        if os.path.isdir(session_dir):
            for f in os.listdir(session_dir):
                if f.endswith(".session"):
                    accounts.append(f.rsplit(".", 1)[0])
    
    results = []
    # 使用信号量限制并发
    sem = asyncio.Semaphore(5)
    
    async def validate_one(acc: str):
        async with sem:
            try:
                result = await multi_manager.validate_session(acc)
                return {"account": acc, **result}
            except Exception as e:
                return {"account": acc, "valid": False, "error": str(e)}
    
    tasks = [validate_one(acc) for acc in accounts]
    results = await asyncio.gather(*tasks)
    
    summary = {
        "total": len(results),
        "authorized": sum(1 for r in results if r.get("authorized")),
        "unauthorized": sum(1 for r in results if r.get("valid") and not r.get("authorized")),
        "invalid": sum(1 for r in results if not r.get("valid")),
    }
    return JSONResponse({"summary": summary, "results": results})


@app.route("/api/send-multi-account", methods=["POST"])
@app.route("/api/send-async-batch", methods=["POST"])
async def send_multi_account(request: Request):
    """
    使用多个账号并发发送消息 - 为每个账号创建单独的任务
    
    请求体:
    {
        "accounts": ["account_01", "account_02", ...],  // 可选，不填则使用所有已授权账号
        "group_ids": [123, 456, ...],
        "message": "消息内容",
        "parse_mode": "plain|markdown|html",
        "disable_web_page_preview": true,
        "delay_ms": 11000,  // 默认 11 秒
        "rounds": 1,
        "round_interval_s": 600,
        "stagger_min_s": 120,  // 账号启动间隔最小秒数
        "stagger_max_s": 300   // 账号启动间隔最大秒数
    }
    """
    token = request.headers.get("X-Admin-Token")
    if token != CONFIG.ADMIN_TOKEN:
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)
    
    body = await request.json()
    group_ids = body.get("group_ids") or []
    message = (body.get("message") or "").strip()
    parse_mode = body.get("parse_mode") or "plain"
    disable_web_page_preview = bool(body.get("disable_web_page_preview", True))
    delay_ms = int(body.get("delay_ms", 11000))  # 默认 11 秒
    delay_ms = max(delay_ms, getattr(CONFIG, "SEND_MIN_DELAY_MS", 1500))
    rounds = int(body.get("rounds", 1))
    round_interval_s = int(body.get("round_interval_s", 600))
    # 错开延迟：默认 10-30 秒，防风控但不会等太久
    stagger_min_s = float(body.get("stagger_min_s", 10))
    stagger_max_s = float(body.get("stagger_max_s", 30))
    request_id = body.get("request_id")
    
    # 防重复请求
    ok, reason = _check_request_guard(token, request_id)
    if not ok:
        return JSONResponse({"detail": "Too Many Requests"}, status_code=429, headers={"Retry-After": "1"})
    
    if not group_ids or not message:
        return JSONResponse({"detail": "group_ids and message required"}, status_code=400)
    
    # 获取账号列表
    accounts = body.get("accounts") or []
    if not accounts:
        count = getattr(CONFIG, "ACCOUNT_COUNT", 100)
        prefix = getattr(CONFIG, "ACCOUNT_PREFIX", "account")
        session_dir = CONFIG.SESSION_DIR
        for i in range(1, count + 1):
            name = f"{prefix}_{i:02d}"
            session_path = os.path.join(session_dir, f"{name}.session")
            if os.path.exists(session_path):
                accounts.append(name)
        for name in _discover_session_accounts():
            if name not in accounts and os.path.exists(os.path.join(session_dir, f"{name}.session")):
                accounts.append(name)
    
    if not accounts:
        return JSONResponse({"detail": "no_accounts_available"}, status_code=400)
    
    # 直接使用有 session 文件的账号，授权检查在发送时进行（更快）
    # 为每个账号创建单独的任务
    task_ids = []
    db: Session = SessionLocal()
    try:
        for acc in accounts:
            task_id = uuid.uuid4().hex[:24]
            t = Task(
                id=task_id,
                status="running",
                total=len(group_ids),
                success=0,
                failed=0,
                account_name=acc,  # 单个账号名
                message=message,
                parse_mode=parse_mode,
                disable_web_page_preview=1 if disable_web_page_preview else 0,
                delay_ms=delay_ms,
                rounds=rounds,
                round_interval_s=round_interval_s,
                current_index=0,
                group_ids_json=json.dumps(group_ids),
                request_id=request_id,
            )
            db.add(t)
            db.add(TaskEvent(
                task_id=task_id, 
                event="created", 
                detail="task_created",
                meta_json=json.dumps({"count": len(group_ids)}, ensure_ascii=False)
            ))
            task_ids.append({"account": acc, "task_id": task_id})
        db.commit()
    finally:
        db.close()
    
    # 为每个账号启动单独的发送任务，带有错开延迟
    cumulative_delay = 0.0
    for i, item in enumerate(task_ids):
        # 计算累积启动延迟 (每个账号间隔 stagger_min_s ~ stagger_max_s 秒)
        if i > 0:
            cumulative_delay += random.uniform(stagger_min_s, stagger_max_s)
        asyncio.create_task(_run_send_task_with_delay(
            task_id=item["task_id"],
            account=item["account"],
            group_ids=group_ids,
            message=message,
            parse_mode=parse_mode,
            disable_web_page_preview=disable_web_page_preview,
            delay_ms=delay_ms,
            rounds=rounds,
            round_interval_s=round_interval_s,
            start_delay=cumulative_delay,
        ))
    
    return JSONResponse({
        "tasks": task_ids,
        "accounts_count": len(accounts),
        "stagger_min_s": stagger_min_s,
        "stagger_max_s": stagger_max_s,
    })


@app.route("/api/groups/join", methods=["POST"])
async def join_group(request: Request):
    """
    使用指定账号加入群组
    
    请求体:
    {
        "account": "account_01",
        "invite_link": "https://t.me/+xxxxx" 或 "@groupname"
    }
    """
    token = request.headers.get("X-Admin-Token")
    if token != CONFIG.ADMIN_TOKEN:
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)
    
    body = await request.json()
    account = body.get("account") or CONFIG.DEFAULT_ACCOUNT
    invite_link = (body.get("invite_link") or "").strip()
    
    if not invite_link:
        return JSONResponse({"detail": "invite_link required"}, status_code=400)
    
    try:
        # 检查账号是否已授权
        authorized = await multi_manager.is_authorized(account)
        if not authorized:
            return JSONResponse({"detail": "account_not_authorized"}, status_code=403)
        
        result = await multi_manager.join_group(account, invite_link)
        return JSONResponse({"account": account, **result})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.route("/api/groups/join-batch", methods=["POST"])
async def join_groups_batch(request: Request):
    """
    批量加入群组 (多账号)
    
    请求体:
    {
        "accounts": ["account_01", "account_02", ...],  // 可选
        "invite_links": ["https://t.me/+xxx", "@group1", ...],
        "delay_ms": 5000  // 每次加群之间的延迟
    }
    """
    token = request.headers.get("X-Admin-Token")
    if token != CONFIG.ADMIN_TOKEN:
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)
    
    body = await request.json()
    invite_links = body.get("invite_links") or []
    delay_ms = int(body.get("delay_ms", 5000))
    
    if not invite_links:
        return JSONResponse({"detail": "invite_links required"}, status_code=400)
    
    # 获取账号列表
    accounts = body.get("accounts") or []
    if not accounts:
        count = getattr(CONFIG, "ACCOUNT_COUNT", 100)
        prefix = getattr(CONFIG, "ACCOUNT_PREFIX", "account")
        session_dir = CONFIG.SESSION_DIR
        for i in range(1, count + 1):
            name = f"{prefix}_{i:02d}"
            session_path = os.path.join(session_dir, f"{name}.session")
            if os.path.exists(session_path):
                accounts.append(name)
        for name in _discover_session_accounts():
            if name not in accounts and os.path.exists(os.path.join(session_dir, f"{name}.session")):
                accounts.append(name)
    
    if not accounts:
        return JSONResponse({"detail": "no_accounts_available"}, status_code=400)
    
    # 验证账号授权状态
    authorized_accounts = []
    for acc in accounts:
        try:
            if await multi_manager.is_authorized(acc):
                authorized_accounts.append(acc)
        except:
            pass
    
    if not authorized_accounts:
        return JSONResponse({"detail": "no_authorized_accounts"}, status_code=400)
    
    results = []
    sem = asyncio.Semaphore(3)  # 限制并发
    
    async def join_one(account: str, link: str, stagger_delay: float = 0):
        # 先等待错开延迟，再获取信号量
        if stagger_delay > 0:
            await asyncio.sleep(stagger_delay)
        async with sem:
            try:
                result = await multi_manager.join_group(account, link)
                return {"account": account, "invite_link": link, **result}
            except Exception as e:
                return {"account": account, "invite_link": link, "ok": False, "error": str(e)}
    
    # 为每个邀请链接选择一个账号，带错开延迟
    tasks = []
    for i, link in enumerate(invite_links):
        # 轮询分配账号
        acc = authorized_accounts[i % len(authorized_accounts)]
        # 计算错开延迟 (在任务内部执行)
        stagger_delay = i * (delay_ms / 1000.0) if delay_ms > 0 else 0
        tasks.append(join_one(acc, link, stagger_delay))
    
    results = await asyncio.gather(*tasks)
    
    summary = {
        "total": len(results),
        "success": sum(1 for r in results if r.get("ok")),
        "already_joined": sum(1 for r in results if r.get("already_joined")),
        "failed": sum(1 for r in results if not r.get("ok") and not r.get("already_joined")),
    }
    
    return JSONResponse({"summary": summary, "results": results})


@app.route("/api/groups/join-all-accounts", methods=["POST"])
async def join_group_all_accounts(request: Request):
    """
    让所有账号加入同一个群组
    
    请求体:
    {
        "accounts": ["account_01", ...],  // 可选
        "invite_link": "https://t.me/+xxxxx",
        "delay_ms": 3000  // 每个账号之间的延迟 (防风控)
    }
    """
    token = request.headers.get("X-Admin-Token")
    if token != CONFIG.ADMIN_TOKEN:
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)
    
    body = await request.json()
    invite_link = (body.get("invite_link") or "").strip()
    delay_ms = int(body.get("delay_ms", 3000))
    
    if not invite_link:
        return JSONResponse({"detail": "invite_link required"}, status_code=400)
    
    # 获取账号列表
    accounts = body.get("accounts") or []
    if not accounts:
        count = getattr(CONFIG, "ACCOUNT_COUNT", 100)
        prefix = getattr(CONFIG, "ACCOUNT_PREFIX", "account")
        session_dir = CONFIG.SESSION_DIR
        
        for i in range(1, count + 1):
            name = f"{prefix}_{i:02d}"
            session_path = os.path.join(session_dir, f"{name}.session")
            if os.path.exists(session_path):
                accounts.append(name)
        for name in _discover_session_accounts():
            if name not in accounts and os.path.exists(os.path.join(session_dir, f"{name}.session")):
                accounts.append(name)
    
    results = []
    
    for i, acc in enumerate(accounts):
        try:
            # 先检查授权
            authorized = await multi_manager.is_authorized(acc)
            if not authorized:
                results.append({"account": acc, "ok": False, "error": "not_authorized"})
                continue
            
            result = await multi_manager.join_group(acc, invite_link)
            results.append({"account": acc, **result})
            
            # 延迟 (防风控)
            if delay_ms > 0 and i < len(accounts) - 1:
                # 添加随机抖动
                jitter = random.uniform(-0.2, 0.2) * delay_ms
                await asyncio.sleep((delay_ms + jitter) / 1000.0)
                
        except Exception as e:
            results.append({"account": acc, "ok": False, "error": str(e)})
    
    summary = {
        "total": len(results),
        "success": sum(1 for r in results if r.get("ok")),
        "already_joined": sum(1 for r in results if r.get("already_joined")),
        "failed": sum(1 for r in results if not r.get("ok") and not r.get("already_joined")),
    }
    
    return JSONResponse({"summary": summary, "results": results})
