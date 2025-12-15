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
from app.routers.accounts import check_single_account, delete_account
import json
import time
import uuid
import asyncio
import os
import random
from datetime import datetime, timedelta, timezone

app = Starlette()

templates = Jinja2Templates(directory="templates")
app.mount("/static", StaticFiles(directory="static"), name="static")


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
    names = [f"{prefix}_{i:02d}" for i in range(1, count + 1)]
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

app.add_route("/api/accounts/check-single", check_single_account, methods=["POST"])
app.add_route("/api/accounts/delete", delete_account, methods=["POST"])

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
    delay_ms = int(body.get("delay_ms", 60000))
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
    delay_ms = int(body.get("delay_ms", 60000))
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


async def _run_send_task(task_id: str, account: str, group_ids: list[int], message: str, parse_mode: str, disable_web_page_preview: bool, delay_ms: int, rounds: int, round_interval_s: int):
    db: Session = SessionLocal()
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
                    t.finished_at = datetime.now(timezone.utc)
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
                    else:
                        t.failed = (t.failed or 0) + 1
                    t.current_index = (t.current_index or 0) + 1
                    t.heartbeat_at = datetime.now(timezone.utc)
                    db.add(TaskEvent(task_id=task_id, event="progress", detail=f"{t.current_index}/{t.total}", meta_json=json.dumps({"gid": gid}, ensure_ascii=False)))
                db.commit()

                if delay > 0:
                    await asyncio.sleep(delay)

            if current_round < rounds:
                t = db.query(Task).filter(Task.id == task_id).first()
                if t:
                    t.next_round_at = datetime.now(timezone.utc) + timedelta(seconds=round_interval_s)
                    db.commit()
                await asyncio.sleep(round_interval_s)

        t = db.query(Task).filter(Task.id == task_id).first()
        if t and t.status not in ("stopped", "error"):
            t.status = "done"
            t.finished_at = datetime.now(timezone.utc)
            db.add(TaskEvent(task_id=task_id, event="finished", detail="task_done", meta_json=json.dumps({}, ensure_ascii=False)))
            db.commit()
    except Exception as e:
        t = db.query(Task).filter(Task.id == task_id).first()
        if t:
            t.status = "error"
            t.finished_at = datetime.now(timezone.utc)
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
    if not phone:
        return JSONResponse({"detail": "phone required"}, status_code=400)
    try:
        resp = await multi_manager.send_login_code(account, phone, force_sms=force_sms)
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
