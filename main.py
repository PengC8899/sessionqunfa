from starlette.applications import Starlette
from starlette.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates
from starlette.responses import HTMLResponse, JSONResponse
from starlette.responses import StreamingResponse, Response
from starlette.requests import Request
from starlette.middleware.cors import CORSMiddleware
from app.config import CONFIG
from app.database import (
    Base,
    engine,
    export_task_migration_manifest,
    SessionLocal,
    export_task_runtime_snapshot,
    is_database_malformed_error,
    is_sqlite_enabled,
    sqlite_checkpoint,
    sqlite_ensure_task_runtime_indexes,
    sqlite_health_check,
)
from sqlalchemy import text, func
from sqlalchemy.exc import DatabaseError
from app.telegram_client import multi_manager, set_copy_receiver, set_on_private_message
from sqlalchemy.orm import Session
from app.models import SendLog, Task, TaskEvent, SystemKV
from app.services.send_service import send_to_groups
from app.services.group_service import (
    get_groups,
    clear_group_cache,
    add_banned_group,
    get_banned_group_ids,
    should_exclude_group_on_error,
    should_add_to_blist,
    should_add_to_global_blist,
)
# from app.services.multi_account_sender import get_multi_sender  # 暂不使用
from app.routers.accounts import (
    auto_assign_account_proxies,
    check_single_account,
    delete_account,
    bulk_update_profile,
    get_default_account_proxy_source,
    get_account_profile,
    get_account_proxy,
    get_authorized_profiles,
    update_default_account_proxy_source,
    update_account_proxy,
    update_account_profile,
)
from app.routers.system import reset_system
from app.services.account_service import account_service
from app.services.dispatch_layer import classify_groups, classify_account, select_groups_for_account, dynamic_delay_ms, randomize_message, recent_fail_rate, unique_group_ids, sort_groups_for_account, distribute_groups_unique
from app.services.proxy_service import (
    get_default_proxy_source,
    assign_proxy_pool_to_accounts,
    delete_proxy_binding,
    list_proxy_bindings,
    resolve_proxy_source_text,
    upsert_proxy_binding,
)
import json
import time
import uuid
import asyncio
import os
import random
import shutil
import logging
import io
import zipfile
from zoneinfo import ZoneInfo
from datetime import datetime, timedelta, timezone
import tempfile

app = Starlette()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

templates = Jinja2Templates(directory="templates")
app.mount("/static", StaticFiles(directory="static"), name="static")

def _normalize_account_names(accounts) -> list[str]:
    names: list[str] = []
    seen: set[str] = set()
    for account in accounts or []:
        name = str(account or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        names.append(name)
    return names


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


def _candidate_accounts(accounts=None) -> list[str]:
    if accounts:
        return _normalize_account_names(accounts)
    session_dir = CONFIG.SESSION_DIR
    names: list[str] = []
    count = getattr(CONFIG, "ACCOUNT_COUNT", 100)
    prefix = getattr(CONFIG, "ACCOUNT_PREFIX", "account")
    for i in range(1, count + 1):
        name = f"{prefix}_{i:02d}"
        if os.path.exists(os.path.join(session_dir, f"{name}.session")):
            names.append(name)
    names.extend(_discover_session_accounts())
    return _normalize_account_names(names)


async def _is_authorized_account(account: str, timeout_s: float | None = None) -> bool:
    timeout = max(float(timeout_s or getattr(CONFIG, "ACCOUNT_AUTH_CHECK_TIMEOUT_S", 12)), 3.0)
    try:
        return bool(await asyncio.wait_for(multi_manager.is_authorized(account), timeout=timeout))
    except Exception:
        return False


async def _split_authorized_accounts(accounts, timeout_s: float | None = None, concurrency: int = 4) -> tuple[list[str], list[str]]:
    names = _normalize_account_names(accounts)
    if not names:
        return [], []
    sem = asyncio.Semaphore(max(1, int(concurrency or 1)))

    async def check(account: str) -> tuple[str, bool]:
        async with sem:
            ok = await _is_authorized_account(account, timeout_s=timeout_s)
            return account, ok

    results = await asyncio.gather(*(check(account) for account in names))
    authorized = [account for account, ok in results if ok]
    rejected = [account for account, ok in results if not ok]
    return authorized, rejected


def _self_check_account_helpers() -> None:
    assert _normalize_account_names(["acc1", "", "acc1", None, " acc2 "]) == ["acc1", "acc2"]


_self_check_account_helpers()


def _purge_account_tasks(db: Session, account_name: str) -> dict:
    task_ids = [row[0] for row in db.query(Task.id).filter(Task.account_name == account_name).all()]
    active_tasks = db.query(Task).filter(Task.account_name == account_name, Task.status == "running").count()
    events_deleted = 0
    tasks_deleted = 0
    if task_ids:
        events_deleted = (
            db.query(TaskEvent)
            .filter(TaskEvent.task_id.in_(task_ids))
            .delete(synchronize_session=False)
        )
        tasks_deleted = (
            db.query(Task)
            .filter(Task.id.in_(task_ids))
            .delete(synchronize_session=False)
        )
        db.commit()
    return {
        "active_tasks_deleted": int(active_tasks or 0),
        "task_records_deleted": int(tasks_deleted or 0),
        "task_events_deleted": int(events_deleted or 0),
    }

@app.middleware("http")
async def admin_token_middleware(request, call_next):
    token = request.headers.get("X-Admin-Token")
    request.state.admin_token = token
    response = await call_next(request)
    return response


@app.route("/")
async def index(request: Request):
    return templates.TemplateResponse("vue_index.html", {"request": request})

@app.route("/api/accounts/status")
async def list_accounts_status(request: Request):
    token = request.headers.get("X-Admin-Token")
    if token != CONFIG.ADMIN_TOKEN:
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)
    session_dir = CONFIG.SESSION_DIR
    discovered = _discover_session_accounts()
    names: list[str] = list(discovered)
    seen = set(names)
    db: Session = SessionLocal()
    try:
        running_rows = db.query(Task.account_name).filter(Task.status == "running").all()
    finally:
        db.close()
    for row in running_rows:
        acc = (row[0] or "").strip()
        if acc and acc not in seen:
            names.append(acc)
            seen.add(acc)
    if not names:
        for name in list(getattr(CONFIG, "ACCOUNTS", {}).keys()):
            n = (name or "").strip()
            if n and n not in seen:
                names.append(n)
                seen.add(n)
    db = SessionLocal()
    try:
        proxy_map = list_proxy_bindings(db, names)
    finally:
        db.close()
    async def inspect_one(name: str) -> dict:
        session_path = os.path.join(session_dir, f"{name}.session")
        session_exists = os.path.exists(session_path)
        if not session_exists:
            return {
                "account": name,
                "authorized": False,
                "status": "missing_file",
                "detail": "Session 文件不存在",
                "proxy": proxy_map.get(name),
                "has_proxy": bool(proxy_map.get(name)),
            }
        try:
            result = await account_service.check_account(name, use_cache=True, include_group_send_check=False)
        except Exception as exc:
            result = {"status": "error", "valid": False, "detail": str(exc)[:120]}
        status = result.get("status") or "unknown"
        detail = result.get("detail")
        if not detail:
            if status == "ok":
                detail = "账号可用"
            elif status == "unauthorized":
                detail = "Telegram session 未授权"
            elif status == "banned":
                detail = "账号已被 Telegram 封禁"
            elif status == "connect_failed":
                detail = "连接 Telegram 失败"
        return {
            "account": name,
            "authorized": bool(result.get("valid")) and status not in {"cannot_send", "unauthorized", "banned", "auth_error", "connect_failed", "error", "unknown", "missing_file"},
            "status": status,
            "detail": detail,
            "phone": result.get("phone"),
            "proxy": proxy_map.get(name),
            "has_proxy": bool(proxy_map.get(name)),
        }

    data = await asyncio.gather(*(inspect_one(name) for name in names))
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
app.add_route("/api/accounts/bulk-update-profile", bulk_update_profile, methods=["POST"])
app.add_route("/api/accounts/profile", get_account_profile, methods=["GET"])
app.add_route("/api/accounts/profiles-authorized", get_authorized_profiles, methods=["GET"])
app.add_route("/api/accounts/profile", update_account_profile, methods=["POST"])
app.add_route("/api/accounts/proxy", get_account_proxy, methods=["GET"])
app.add_route("/api/accounts/proxy", update_account_proxy, methods=["POST"])
app.add_route("/api/accounts/proxy/default-source", get_default_account_proxy_source, methods=["GET"])
app.add_route("/api/accounts/proxy/default-source", update_default_account_proxy_source, methods=["POST"])
app.add_route("/api/accounts/proxy/auto-assign", auto_assign_account_proxies, methods=["POST"])
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
    uploaded_accounts: list[str] = []
    proxy_bindings_raw = form.get("proxy_bindings")
    proxy_source_text = form.get("proxy_source_text")
    resolved_proxy_source_text = str(proxy_source_text or "").strip()
    proxy_bindings: dict[str, str] = {}
    proxy_pool: list[str] = []
    proxy_meta: dict = {}
    if proxy_bindings_raw:
        try:
            parsed_bindings = json.loads(str(proxy_bindings_raw))
            if isinstance(parsed_bindings, dict):
                proxy_bindings = {
                    str(k).strip(): (str(v).strip() if v is not None else "")
                    for k, v in parsed_bindings.items()
                    if str(k).strip()
                }
        except Exception:
            errors.append("proxy_bindings: JSON 格式无效，已忽略代理绑定")
    if not resolved_proxy_source_text:
        db = SessionLocal()
        try:
            resolved_proxy_source_text = get_default_proxy_source(db)
        finally:
            db.close()
    extra_map, proxy_pool, proxy_meta = resolve_proxy_source_text(resolved_proxy_source_text, errors)
    proxy_bindings.update(extra_map)
    
    os.makedirs(CONFIG.SESSION_DIR, exist_ok=True)

    async def persist_session(account_name: str, content: bytes):
        nonlocal uploaded
        target_path = os.path.join(CONFIG.SESSION_DIR, f"{account_name}.session")

        with open(target_path, "wb") as f:
            f.write(content)

        if account_name in multi_manager.managers:
            try:
                await multi_manager.managers[account_name].disconnect()
                logging.info(f"[UPLOAD] Disconnected old session for {account_name}")
            except Exception:
                pass
        if account_name not in uploaded_accounts:
            uploaded_accounts.append(account_name)

        try:
            file_size = os.path.getsize(target_path)
            if file_size > 5000:
                if account_name not in validated_accounts:
                    validated_accounts.append(account_name)
                logging.info(f"[UPLOAD] Session file uploaded for {account_name} ({file_size} bytes)")
            else:
                errors.append(f"{account_name}: 文件太小，可能损坏 ({file_size} bytes)")
        except Exception as e:
            errors.append(f"{account_name}: 文件检查失败 - {str(e)[:50]}")

        uploaded += 1

    for file in files:
        try:
            filename = (file.filename or "").strip()
            lower_name = filename.lower()
            content = await file.read()

            if lower_name.endswith(".session"):
                account_name = os.path.basename(filename)[:-8]
                if not account_name:
                    errors.append(f"{filename}: 文件名无效")
                    continue
                await persist_session(account_name, content)
                continue

            if lower_name.endswith(".zip"):
                try:
                    archive = zipfile.ZipFile(io.BytesIO(content))
                except zipfile.BadZipFile:
                    errors.append(f"{filename}: ZIP 文件损坏或格式不正确")
                    continue

                members = [
                    info for info in archive.infolist()
                    if not info.is_dir() and info.filename.lower().endswith(".session")
                ]
                if not members:
                    errors.append(f"{filename}: ZIP 中未找到 .session 文件")
                    continue

                for info in members:
                    account_name = os.path.basename(info.filename)[:-8]
                    if not account_name:
                        errors.append(f"{filename}: {info.filename} 文件名无效")
                        continue
                    try:
                        await persist_session(account_name, archive.read(info))
                    except Exception as e:
                        errors.append(f"{filename}: {info.filename} - {str(e)[:80]}")
                continue

            errors.append(f"{filename}: 仅支持 .session 或 .zip 文件")
        except Exception as e:
            errors.append(f"{file.filename}: {str(e)}")

    assigned_proxy_bindings, assignment_meta = assign_proxy_pool_to_accounts(
        uploaded_accounts,
        proxy_bindings,
        proxy_pool,
    )
    if assignment_meta.get("unassigned_accounts"):
        errors.append(
            "代理数量不足："
            f"已绑定 {assignment_meta.get('assigned_total', 0)} / {assignment_meta.get('accounts_total', 0)} 个上传账号，"
            f"未分配账号 {', '.join(assignment_meta.get('unassigned_accounts', [])[:20])}"
        )

    if assigned_proxy_bindings:
        for account_name in uploaded_accounts:
            if account_name not in assigned_proxy_bindings:
                continue
            db = SessionLocal()
            try:
                try:
                    upsert_proxy_binding(db, account_name, assigned_proxy_bindings.get(account_name), enabled=True)
                except ValueError as e:
                    errors.append(f"{account_name}: 代理格式错误 - {str(e)}")
            finally:
                db.close()
    
    return JSONResponse({
        "uploaded": uploaded,
        "validated": len(validated_accounts),
        "validated_accounts": validated_accounts,
        "proxy_assigned": int(assignment_meta.get("assigned_total", 0)),
        "proxy_accounts_total": int(assignment_meta.get("accounts_total", 0)),
        "proxy_source_text": resolved_proxy_source_text,
        "proxy_source_meta": proxy_meta,
        "proxy_assignment_meta": assignment_meta,
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
            task_cleanup = {
                "active_tasks_deleted": 0,
                "task_records_deleted": 0,
                "task_events_deleted": 0,
            }
            try:
                task_cleanup = _purge_account_tasks(db, name)
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
                delete_proxy_binding(db, name)
            except Exception as e:
                error = str(e)[:200]
            results.append(
                {
                    "account": name,
                    "deleted": bool(deleted),
                    "tasks_stopped": task_cleanup["active_tasks_deleted"],
                    "task_records_deleted": task_cleanup["task_records_deleted"],
                    "task_events_deleted": task_cleanup["task_events_deleted"],
                    "error": error,
                }
            )
        return JSONResponse({"results": results})
    finally:
        db.close()

@app.route("/api/task-control", methods=["POST"])
async def task_control(request: Request):
    token = request.headers.get("X-Admin-Token")
    if token != CONFIG.ADMIN_TOKEN:
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"detail": "Invalid JSON"}, status_code=400)
    task_id = body.get("task_id")
    action = (body.get("action") or "").strip().lower()
    if not task_id or action not in ("pause", "resume", "stop"):
        return JSONResponse({"detail": "bad_request"}, status_code=400)
    db: Session = SessionLocal()
    try:
        t = db.query(Task).filter(Task.id == task_id).first()
        if not t:
            return JSONResponse({"detail": "Not Found"}, status_code=404)
        if action == "pause":
            t.paused = 1
            db.add(TaskEvent(task_id=task_id, event="paused", detail="task_paused", meta_json=json.dumps({}, ensure_ascii=False)))
        elif action == "resume":
            t.paused = 0
            db.add(TaskEvent(task_id=task_id, event="resumed", detail="task_resumed", meta_json=json.dumps({}, ensure_ascii=False)))
        elif action == "stop":
            t.stop_requested = 1
            db.add(TaskEvent(task_id=task_id, event="stop_requested", detail="task_stop_requested", meta_json=json.dumps({}, ensure_ascii=False)))
        db.commit()
        return JSONResponse({"ok": True})
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
    authorized = await _is_authorized_account(account)
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
        authorized = await _is_authorized_account(account)
        info["authorized"] = bool(authorized)
        if not authorized:
            return JSONResponse(info)
        await multi_manager.ensure_connected(account)
        client = multi_manager.get(account).client
        dialogs = await client.get_dialogs()
        info["dialogs_count"] = len(dialogs)
        sample = []
        from telethon.tl.types import Channel, Chat
        from telethon.utils import get_peer_id
        for d in dialogs[:10]:
            e = d.entity
            if isinstance(e, Chat):
                sample.append({"id": get_peer_id(e), "raw_id": e.id, "title": d.name, "type": "chat"})
            elif isinstance(e, Channel):
                is_megagroup = bool(getattr(e, "megagroup", False))
                if only_groups and not is_megagroup:
                    continue
                sample.append({"id": get_peer_id(e), "raw_id": e.id, "title": d.name, "type": "channel", "megagroup": is_megagroup})
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
    ok, _reason = _check_request_guard(token, request_id)
    if not ok:
        return JSONResponse({"detail": "Too Many Requests"}, status_code=429, headers={"Retry-After": "1"})
    authorized = await _is_authorized_account(account)
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
    ok, _reason = _check_request_guard(token, request_id)
    if not ok:
        return JSONResponse({"detail": "Too Many Requests"}, status_code=429, headers={"Retry-After": "1"})
    authorized = await _is_authorized_account(account)
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
                "account_name": getattr(r, "account_name", None),
                "group_id": r.group_id,
                "group_title": r.group_title,
                "message_preview": r.message_preview,
                "status": r.status,
                "error": r.error,
                "message_id": getattr(r, "message_id", None),
                "parse_mode": getattr(r, "parse_mode", None),
                "created_at": _serialize_log_created_at(r.created_at),
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
                _serialize_log_created_at(r.created_at) or "",
            ])
        csv_data = buf.getvalue()
        headers = {"Content-Type": "text/csv; charset=utf-8", "Content-Disposition": "attachment; filename=send_logs.csv"}
        return Response(content=csv_data, media_type="text/csv", headers=headers)
    finally:
        db.close()


@app.route("/api/logs/clear", methods=["POST"])
async def clear_logs(request: Request):
    token = request.headers.get("X-Admin-Token")
    if token != CONFIG.ADMIN_TOKEN:
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)
    db: Session = SessionLocal()
    try:
        deleted = db.query(SendLog).delete()
        db.commit()
        return JSONResponse({"deleted": int(deleted)})
    except Exception as e:
        try:
            db.rollback()
        except Exception:
            pass
        return JSONResponse({"detail": str(e)}, status_code=500)
    finally:
        db.close()

async def startup_event():
    print("[STARTUP] Event triggered")
    Base.metadata.create_all(bind=engine)
    try:
        with engine.connect() as conn:
            conn.execute(text("PRAGMA journal_mode=WAL;"))
            cols = conn.execute(text("PRAGMA table_info('send_logs')")).fetchall()
            names = {c[1] for c in cols}
            if 'task_id' not in names:
                conn.execute(text("ALTER TABLE send_logs ADD COLUMN task_id VARCHAR(64)"))
            if 'task_round' not in names:
                conn.execute(text("ALTER TABLE send_logs ADD COLUMN task_round INTEGER"))
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
            if 'delay_scope' not in task_names:
                conn.execute(text("ALTER TABLE tasks ADD COLUMN delay_scope VARCHAR(32) DEFAULT 'per_account'"))
            conn.commit()
    except Exception:
        pass
    try:
        import os as _os
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
                        shutil.copy2(_src, _target)
                    except Exception:
                        pass
    except Exception:
        pass
    try:
        db: Session = SessionLocal()
        try:
            row = db.query(SystemKV).filter(SystemKV.k == "copy_receiver").first()
            if row and row.v:
                try:
                    d = json.loads(row.v)
                    acc = d.get("account")
                    enabled = bool(d.get("enabled", 0))
                    if acc:
                        set_copy_receiver(acc, enabled)
                except Exception:
                    pass
            dp = db.query(SystemKV).filter(SystemKV.k == "login_phone:main_account").first()
            if not dp:
                dp = SystemKV(k="login_phone:main_account", v=json.dumps({"phone": "+1 548 520 7895"}, ensure_ascii=False))
                db.add(dp)
                db.commit()
        finally:
            db.close()
    except Exception:
        pass

    # 设置 account_service 的 manager 引用（用于健康检查）
    account_service.set_manager(multi_manager)

    if is_sqlite_enabled():
        try:
            prepare_result = sqlite_ensure_task_runtime_indexes()
            manifest_path = export_task_migration_manifest("startup")
            health = sqlite_health_check("quick_check")
            if not health.get("ok", False):
                snapshot_path = _capture_task_db_snapshot("startup_healthcheck_failed")
                print(f"[STARTUP][TASK_DB] health_check failed: {health.get('result')} snapshot={snapshot_path}")
            else:
                snapshot_path = _capture_task_db_snapshot("startup")
                sqlite_checkpoint("PASSIVE")
                if snapshot_path:
                    print(f"[STARTUP][TASK_DB] startup snapshot saved: {snapshot_path}")
            if prepare_result.get("indexes") or manifest_path:
                print(
                    "[STARTUP][TASK_DB] prepared "
                    f"indexes={len(prepare_result.get('indexes', []))} "
                    f"manifest={manifest_path}"
                )
        except Exception as exc:
            print(f"[STARTUP][TASK_DB] health bootstrap failed: {exc}")
    
    # 启动定期清理空闲连接的后台任务
    asyncio.create_task(_periodic_connection_cleanup())
    asyncio.create_task(_periodic_task_cleanup())
    asyncio.create_task(_periodic_sqlite_maintenance())

    db: Session = SessionLocal()
    try:
        rows = db.query(Task).filter(Task.status == "running").limit(100).all()
        if int(getattr(CONFIG, "RESUME_TASKS_ON_STARTUP", 0)) != 1:
            now = CONFIG.now()
            for t in rows:
                t.status = "stopped"
                t.stop_requested = 1
                t.finished_at = now
                db.add(TaskEvent(
                    task_id=t.id,
                    event="startup_resume_skipped",
                    detail="resume_disabled_on_startup",
                    meta_json=json.dumps({}, ensure_ascii=False),
                ))
            db.commit()
            print(f"[STARTUP] Resume disabled, stopped {len(rows)} running task(s)")
            return
        for t in rows:
            try:
                start_delay = random.uniform(
                    max(int(getattr(CONFIG, "STARTUP_RESUME_MIN_DELAY_S", 5)), 0),
                    max(int(getattr(CONFIG, "STARTUP_RESUME_MAX_DELAY_S", 45)), 0),
                )
                print(
                    f"[STARTUP] Resuming task {t.id} for {t.account_name} "
                    f"at round {t.current_round} index {t.current_index} "
                    f"with delay {start_delay:.1f}s"
                )
                await _resume_existing_task(
                    t,
                    reason="startup_resume",
                    start_delay=start_delay,
                )
            except Exception:
                pass
    finally:
        db.close()


_REQ_IDS: dict[str, float] = {}
_LAST_TS: dict[str, float] = {}
_GLOBAL_SEND_NEXT_AT_BY_BUCKET: dict[str, float] = {}
_GLOBAL_SEND_SLOT_LOCK = asyncio.Lock()


async def _periodic_connection_cleanup():
    while True:
        try:
            await asyncio.sleep(max(int(getattr(CONFIG, "CONNECTION_CLEANUP_INTERVAL_S", 60)), 10))
            await multi_manager._cleanup_idle_connections()
        except Exception as e:
            print(f"[CLEANUP] Error: {e}")

async def _periodic_task_cleanup():
    while True:
        try:
            await asyncio.sleep(max(int(getattr(CONFIG, "TASK_CLEANUP_INTERVAL_S", 60)), 10))
            db: Session = SessionLocal()
            try:
                now = CONFIG.now()
                timeout_s = int(getattr(CONFIG, "TASK_STUCK_TIMEOUT_S", 1800))
                missing_runner_timeout_s = int(getattr(CONFIG, "TASK_RUNNER_MISSING_TIMEOUT_S", 120))
                rows = db.query(Task).filter(Task.status == "running").limit(500).all()
                for t in rows:
                    if int(t.paused or 0) == 1 or int(t.stop_requested or 0) == 1:
                        continue
                    hb = t.heartbeat_at or t.started_at
                    if not hb:
                        continue
                    heartbeat_age_s = _seconds_since_task_timestamp(hb)
                    if not _task_runner_active(t.id):
                        if (
                            int(getattr(CONFIG, "RESUME_TASKS_ON_STARTUP", 1)) == 1
                            and heartbeat_age_s >= missing_runner_timeout_s
                        ):
                            if await _resume_existing_task(
                                t,
                                reason="watchdog_runner_missing",
                                start_delay=0.0,
                            ):
                                continue
                    if heartbeat_age_s > timeout_s:
                        t.status = "error"
                        t.stop_requested = 1
                        t.finished_at = now
                        db.add(TaskEvent(
                            task_id=t.id,
                            event="stuck_timeout",
                            detail=str(timeout_s),
                            meta_json=json.dumps({"heartbeat_age_s": int(heartbeat_age_s)}, ensure_ascii=False),
                        ))
                try:
                    db.commit()
                except DatabaseError as exc:
                    db.rollback()
                    _handle_task_db_error("periodic_task_cleanup_commit", exc)
            finally:
                db.close()
        except DatabaseError as exc:
            _handle_task_db_error("periodic_task_cleanup", exc)
        except Exception as e:
            print(f"[CLEANUP] Error: {e}")


async def _periodic_sqlite_maintenance():
    if not is_sqlite_enabled():
        return
    while True:
        try:
            await asyncio.sleep(max(int(getattr(CONFIG, "SQLITE_HEALTHCHECK_INTERVAL_S", 300)), 60))
            health = sqlite_health_check("quick_check")
            if not health.get("ok", False):
                snapshot_path = _capture_task_db_snapshot("sqlite_healthcheck_failed")
                print(f"[TASK_DB] quick_check failed: {health.get('result')} snapshot={snapshot_path}")
                continue
            sqlite_ensure_task_runtime_indexes()
            sqlite_checkpoint("PASSIVE")
        except Exception as e:
            print(f"[TASK_DB] maintenance error: {e}")


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


def _normalize_delay_scope(raw_scope: str | None, default: str = "per_account") -> str:
    scope = str(raw_scope or default).strip().lower()
    if scope not in {"per_account", "global"}:
        return default
    return scope


def _prefer_per_account_delay_scope(
    raw_scope: str | None,
    request_id: str | None,
    has_sibling_tasks: bool,
) -> str:
    scope = _normalize_delay_scope(raw_scope, "per_account")
    if scope == "global" and str(request_id or "").strip() and has_sibling_tasks:
        return "per_account"
    return scope


def _prefer_batch_delay_ms(delay_ms: int, has_sibling_tasks: bool) -> int:
    value = max(0, int(delay_ms or 0))
    if has_sibling_tasks and value >= 11000:
        return 3000
    return value


def _task_has_sibling_tasks(db: Session, task: Task | None, task_id: str) -> tuple[str, bool]:
    if task is None:
        return "", False
    request_id = str(getattr(task, "request_id", "") or "").strip()
    if not request_id:
        return "", False
    has_sibling_tasks = (
        db.query(Task.id)
        .filter(Task.request_id == request_id, Task.id != task_id)
        .first()
        is not None
    )
    return request_id, has_sibling_tasks


def _effective_task_timing(db: Session, task: Task | None, task_id: str, delay_ms: int) -> tuple[str, int]:
    if task is None:
        return "per_account", max(0, int(delay_ms or 0))
    request_id, has_sibling_tasks = _task_has_sibling_tasks(db, task, task_id)
    scope = _prefer_per_account_delay_scope(getattr(task, "delay_scope", None), request_id, has_sibling_tasks)
    effective_delay_ms = _prefer_batch_delay_ms(delay_ms, has_sibling_tasks)
    return scope, effective_delay_ms


def _self_check_delay_scope_helpers() -> None:
    assert _prefer_per_account_delay_scope("global", "req-1", True) == "per_account"
    assert _prefer_per_account_delay_scope("global", "req-1", False) == "global"
    assert _prefer_per_account_delay_scope("per_account", "req-1", True) == "per_account"
    assert _prefer_batch_delay_ms(11000, True) == 3000
    assert _prefer_batch_delay_ms(3000, True) == 3000


_self_check_delay_scope_helpers()


def _compute_effective_task_delay_ms(account: str, delay_ms: int) -> int:
    base_ms = max(int(delay_ms or 0), int(getattr(CONFIG, "SEND_MIN_DELAY_MS", 1500)))
    if int(getattr(CONFIG, "SMART_SCHEDULER_ENABLED", 1)) != 1:
        return base_ms
    with SessionLocal() as db:
        rate = recent_fail_rate(db, account, int(getattr(CONFIG, "ACCOUNT_RECENT_WINDOW_N", 50)))
    adjusted_ms = dynamic_delay_ms(base_ms, rate)
    jitter_pct = float(getattr(CONFIG, "SEND_JITTER_PCT", 0.15))
    jitter = random.uniform(-jitter_pct, jitter_pct) * adjusted_ms
    return max(0, int(adjusted_ms + jitter))


def _global_send_bucket_key(task: Task | None, task_id: str) -> str:
    if task is None:
        return task_id
    request_id = str(getattr(task, "request_id", "") or "").strip()
    if request_id:
        return f"request:{request_id}"
    return f"task:{task_id}"


async def _reserve_global_send_slot(task_id: str, bucket_key: str, wait_s: float) -> bool:
    """在全局发送 bucket 上预留一个槽位，并等待到槽位到期再返回。

    每次调用都会把 bucket 的“下次可用时间”往后推 wait_s，并真实等待到
    自己这轮槽位到期，从而保证同一个 bucket（同一个 request/task）内相邻
    两次发送之间有至少 wait_s 的间隔，真正起到防风控作用。
    """
    delay_s = max(0.0, float(wait_s))
    async with _GLOBAL_SEND_SLOT_LOCK:
        now = time.monotonic()
        last_at = _GLOBAL_SEND_NEXT_AT_BY_BUCKET.get(bucket_key, 0.0)
        ready_at = max(now, last_at)
        _GLOBAL_SEND_NEXT_AT_BY_BUCKET[bucket_key] = ready_at + delay_s
    wait_needed_s = ready_at - now
    if wait_needed_s > 0:
        # 等待期间保持心跳，并响应暂停/停止请求
        if not await _sleep_with_task_checks(task_id, wait_needed_s, refresh_heartbeat=True):
            return False
    return True


def _get_existing_tasks_by_request_id(db: Session, request_id: str | None) -> list[Task]:
    if not request_id:
        return []
    return (
        db.query(Task)
        .filter(Task.request_id == request_id)
        .order_by(Task.started_at.desc())
        .all()
    )


def _planned_group_count(group_ids_json: str | None) -> int:
    try:
        group_ids = json.loads(group_ids_json or "[]")
    except Exception:
        return 0
    return len(group_ids) if isinstance(group_ids, list) else 0


def _filter_account_banned_group_ids(db: Session, account: str, group_ids: list[int]) -> list[int]:
    banned = set(get_banned_group_ids(db, account))
    if not banned:
        return unique_group_ids(group_ids)
    return [gid for gid in unique_group_ids(group_ids) if int(gid) not in banned]


def _remove_group_id_once(group_ids: list[int], gid: int) -> list[int]:
    target = int(gid)
    return [item for item in group_ids if int(item) != target]


def _self_check_group_skip_helpers() -> None:
    assert _remove_group_id_once([1, 2, 3], 2) == [1, 3]


_self_check_group_skip_helpers()


def _collect_task_log_counters(
    db: Session,
    task_ids: list[str],
    current_round_by_task: dict[str, int] | None = None,
    task_rows_by_id: dict[str, Task] | None = None,
) -> dict[str, dict[str, int]]:
    counters: dict[str, dict[str, int]] = {
        task_id: {
            "success": 0,
            "failed": 0,
            "overall_completed": 0,
            "current_round_completed": 0,
            "current_round_success": 0,
            "current_round_failed": 0,
        }
        for task_id in task_ids
        if task_id
    }
    if not counters:
        return {}

    rows = (
        db.query(
            SendLog.task_id,
            SendLog.task_round,
            SendLog.status,
            func.count(SendLog.id),
        )
        .filter(
            SendLog.task_id.in_(list(counters.keys())),
            SendLog.status.in_(("success", "failed")),
        )
        .group_by(SendLog.task_id, SendLog.task_round, SendLog.status)
        .all()
    )

    current_round_by_task = current_round_by_task or {}
    for task_id, task_round, status, count in rows:
        bucket = counters.get(task_id)
        if not bucket:
            continue
        n = int(count or 0)
        if status == "success":
            bucket["success"] += n
        elif status == "failed":
            bucket["failed"] += n
        bucket["overall_completed"] += n

        if current_round_by_task.get(task_id) == int(task_round or 0):
            bucket["current_round_completed"] += n
            if status == "success":
                bucket["current_round_success"] += n
            elif status == "failed":
                bucket["current_round_failed"] += n

    # 兼容历史日志：旧版本 send_logs 没有 task_id/task_round，
    # 回退到“账号 + 任务开始时间”聚合累计成功/失败。
    task_rows_by_id = task_rows_by_id or {}
    for task_id, bucket in counters.items():
        if bucket["overall_completed"] > 0:
            continue
        task = task_rows_by_id.get(task_id)
        if not task or not task.started_at or not task.account_name:
            continue
        legacy_rows = (
            db.query(SendLog.status, func.count(SendLog.id))
            .filter(
                SendLog.task_id.is_(None),
                SendLog.account_name == task.account_name,
                SendLog.created_at >= task.started_at,
                SendLog.status.in_(("success", "failed")),
            )
            .group_by(SendLog.status)
            .all()
        )
        for status, count in legacy_rows:
            n = int(count or 0)
            if status == "success":
                bucket["success"] += n
            elif status == "failed":
                bucket["failed"] += n
            bucket["overall_completed"] += n

    return counters


def _serialize_log_created_at(value: datetime | None) -> str | None:
    if value is None:
        return None
    try:
        target_tz = CONFIG.get_timezone()
    except Exception:
        target_tz = timezone.utc
    dt = value
    if dt.tzinfo is None:
        # SQLite often returns naive datetimes even when the original value was
        # written in the configured local timezone. Treat naive log timestamps
        # as local configured time to avoid shifting them by the timezone offset.
        dt = dt.replace(tzinfo=target_tz)
    try:
        dt = dt.astimezone(target_tz)
    except Exception:
        pass
    return dt.isoformat()


def _mark_task_stopped_if_requested(db: Session, task_id: str, detail: str) -> bool:
    t = db.query(Task).filter(Task.id == task_id).first()
    if not t:
        return True
    if not t.stop_requested:
        return False
    if t.status == "error":
        return True
    if t.status != "stopped":
        t.status = "stopped"
        t.finished_at = CONFIG.now()
        db.add(TaskEvent(task_id=task_id, event="stopped", detail=detail, meta_json=json.dumps({}, ensure_ascii=False)))
        db.commit()
    return True


async def _sleep_with_task_checks(
    task_id: str,
    seconds: float,
    step_s: float = 1.0,
    refresh_heartbeat: bool = True,
    heartbeat_interval_s: float | None = None,
) -> bool:
    remaining = max(0.0, float(seconds))
    if heartbeat_interval_s is None:
        heartbeat_interval_s = float(getattr(CONFIG, "TASK_HEARTBEAT_INTERVAL_S", 20))
    next_heartbeat_at = time.monotonic()
    while remaining > 0:
        db: Session | None = None
        try:
            db = SessionLocal()
            if _mark_task_stopped_if_requested(db, task_id, "task_stopped_during_wait"):
                return False
            if refresh_heartbeat and time.monotonic() >= next_heartbeat_at:
                t = db.query(Task).filter(Task.id == task_id).first()
                if not t:
                    return False
                if t.status == "running":
                    t.heartbeat_at = CONFIG.now()
                    db.commit()
                next_heartbeat_at = time.monotonic() + max(1.0, float(heartbeat_interval_s))
        except DatabaseError as exc:
            if db is not None:
                db.rollback()
            _handle_task_db_error("sleep_with_task_checks", exc)
            if refresh_heartbeat and time.monotonic() >= next_heartbeat_at:
                _update_runtime_task_state(
                    task_id,
                    status=TASKS.get(task_id, {}).get("status") or "running",
                    db_warning="task_db_unavailable",
                    heartbeat_at=_runtime_state_iso_now(),
                )
                next_heartbeat_at = time.monotonic() + max(1.0, float(heartbeat_interval_s))
        finally:
            if db is not None:
                db.close()
        wait_s = min(step_s, remaining)
        await asyncio.sleep(wait_s)
        remaining -= wait_s
    return True

app.add_event_handler("startup", startup_event)

TASKS: dict[str, dict] = {}
_LAST_TASK_DB_SNAPSHOT_TS = 0.0

LAST_COPY_MESSAGE: dict = {}


def _runtime_state_iso_now() -> str:
    try:
        return CONFIG.now().isoformat()
    except Exception:
        return datetime.now(timezone.utc).isoformat()


def _update_runtime_task_state(task_id: str, **fields) -> None:
    state = TASKS.get(task_id)
    if not state:
        state = {}
        TASKS[task_id] = state
    state.update(fields)
    state["last_updated_at"] = _runtime_state_iso_now()


def _capture_task_db_snapshot(reason: str) -> str | None:
    global _LAST_TASK_DB_SNAPSHOT_TS
    now = time.monotonic()
    if now - _LAST_TASK_DB_SNAPSHOT_TS < 60:
        return None
    _LAST_TASK_DB_SNAPSHOT_TS = now
    try:
        return export_task_runtime_snapshot(reason)
    except Exception as exc:
        print(f"[TASK_DB] snapshot failed ({reason}): {exc}")
        return None


def _runtime_task_list_rows() -> list[dict]:
    rows: list[dict] = []
    for task_id, state in TASKS.items():
        account = state.get("account")
        if not account:
            continue
        rows.append(
            {
                "task_id": task_id,
                "status": state.get("status") or ("running" if _task_runner_active(task_id) else "unknown"),
                "total": int(state.get("total") or 0),
                "success": int(state.get("success") or 0),
                "failed": int(state.get("failed") or 0),
                "current_index": int(state.get("current_index") or 0),
                "account": account,
                "delay_scope": _normalize_delay_scope(state.get("delay_scope"), "per_account"),
                "started_at": state.get("started_at"),
                "finished_at": state.get("finished_at"),
                "rounds": int(state.get("rounds") or 0),
                "current_round": int(state.get("current_round") or 0),
                "next_round_at": state.get("next_round_at"),
                "source": "runtime_fallback",
                "db_warning": "task_db_unavailable",
            }
        )
    rows.sort(key=lambda item: item.get("started_at") or "", reverse=True)
    return rows


def _runtime_task_summary_rows() -> list[dict]:
    selected: dict[str, dict] = {}
    for item in _runtime_task_list_rows():
        account = item.get("account")
        if not account:
            continue
        existing = selected.get(account)
        if not existing:
            selected[account] = item
            continue
        if existing.get("status") != "running" and item.get("status") == "running":
            selected[account] = item
            continue
        if (item.get("started_at") or "") > (existing.get("started_at") or ""):
            selected[account] = item

    data: list[dict] = []
    for item in selected.values():
        completed = int(item.get("current_index") or 0)
        total = int(item.get("total") or 0)
        data.append(
            {
                "account": item.get("account"),
                "status": item.get("status"),
                "task_id": item.get("task_id"),
                "delay_scope": item.get("delay_scope"),
                "tasks_count": 1,
                "total": total,
                "success": int(item.get("success") or 0),
                "failed": int(item.get("failed") or 0),
                "completed": completed,
                "progress_total": total,
                "progress_completed": completed,
                "current_round": int(item.get("current_round") or 0),
                "rounds": int(item.get("rounds") or 0),
                "current_round_planned": total,
                "current_round_sent": completed,
                "overall_planned": total,
                "overall_completed": completed,
                "last_updated_at": item.get("finished_at") or item.get("started_at") or item.get("last_updated_at"),
                "source": "runtime_fallback",
                "db_warning": "task_db_unavailable",
            }
        )
    data.sort(key=lambda item: item.get("last_updated_at") or "", reverse=True)
    return data


def _runtime_task_status_payload(task_id: str) -> dict | None:
    state = TASKS.get(task_id)
    if not state:
        return None
    total = int(state.get("total") or 0)
    completed = int(state.get("current_index") or 0)
    return {
        "task_id": task_id,
        "status": state.get("status") or ("running" if _task_runner_active(task_id) else "unknown"),
        "total": total,
        "delay_scope": _normalize_delay_scope(state.get("delay_scope"), "per_account"),
        "success": int(state.get("success") or 0),
        "failed": int(state.get("failed") or 0),
        "current_index": completed,
        "current_round_planned": total,
        "current_round_sent": completed,
        "current_round_completed": completed,
        "previous_rounds_completed": 0,
        "overall_planned": total,
        "overall_completed": completed,
        "started_at": state.get("started_at"),
        "finished_at": state.get("finished_at"),
        "rounds": int(state.get("rounds") or 0),
        "current_round": int(state.get("current_round") or 0),
        "round_interval_s": int(state.get("round_interval_s") or 0),
        "next_round_at": state.get("next_round_at"),
        "source": "runtime_fallback",
        "db_warning": "task_db_unavailable",
    }


def _handle_task_db_error(context: str, exc: Exception) -> None:
    snapshot_path = _capture_task_db_snapshot(f"{context}_db_error")
    suffix = f", snapshot={snapshot_path}" if snapshot_path else ""
    malformed = " malformed" if is_database_malformed_error(exc) else ""
    print(f"[TASK_DB]{malformed} {context} degraded: {exc}{suffix}")


def _task_runner_active(task_id: str) -> bool:
    state = TASKS.get(task_id)
    if not state:
        return False
    runner = state.get("runner")
    if runner is None or runner.done():
        return False
    return True


def _register_task_runner(task_id: str, runner: asyncio.Task, meta: dict | None = None) -> None:
    payload = {"runner": runner}
    if meta:
        payload.update(meta)
    TASKS[task_id] = payload
    _update_runtime_task_state(task_id, runner_done=False)

    def _cleanup(done_task: asyncio.Task, tid: str = task_id):
        current = TASKS.get(tid)
        if current and current.get("runner") is done_task:
            current["runner_done"] = True
            current["last_updated_at"] = _runtime_state_iso_now()
        try:
            done_task.result()
        except asyncio.CancelledError:
            _update_runtime_task_state(tid, status=current.get("status") or "stopped", finished_at=_runtime_state_iso_now())
        except Exception as exc:
            _update_runtime_task_state(tid, status="error", last_error=str(exc)[:200], finished_at=_runtime_state_iso_now())
            print(f"[TASK] runner crashed for {tid}: {exc}")

    runner.add_done_callback(_cleanup)


def _seconds_since_task_timestamp(value: datetime | None) -> float:
    if value is None:
        return 0.0
    try:
        target_tz = CONFIG.get_timezone()
    except Exception:
        target_tz = timezone.utc
    ts = value
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=target_tz)
    now = CONFIG.now()
    if now.tzinfo is None:
        now = now.replace(tzinfo=target_tz)
    try:
        delta = (now.astimezone(target_tz) - ts.astimezone(target_tz)).total_seconds()
    except Exception:
        now_naive = datetime.now(timezone.utc).replace(tzinfo=None)
        ts_naive = value if value.tzinfo is None else value.astimezone(timezone.utc).replace(tzinfo=None)
        delta = (now_naive - ts_naive).total_seconds()
    return max(0.0, float(delta))


def _start_send_task_runner(
    *,
    task_id: str,
    account: str,
    group_ids: list[int],
    message: str,
    parse_mode: str,
    disable_web_page_preview: bool,
    delay_ms: int,
    rounds: int,
    round_interval_s: int,
    start_delay: float = 0.0,
    start_round_idx: int = 0,
    start_group_idx: int = 0,
    delay_scope: str = "per_account",
) -> bool:
    if _task_runner_active(task_id):
        return False

    runner = asyncio.create_task(
        _run_send_task_with_delay(
            task_id=task_id,
            account=account,
            group_ids=group_ids,
            message=message,
            parse_mode=parse_mode,
            disable_web_page_preview=disable_web_page_preview,
            delay_ms=delay_ms,
            rounds=rounds,
            round_interval_s=round_interval_s,
            start_delay=start_delay,
            start_round_idx=start_round_idx,
            start_group_idx=start_group_idx,
        )
    )
    _register_task_runner(
        task_id,
        runner,
        {
            "account": account,
            "status": "running",
            "started_at": _runtime_state_iso_now(),
            "delay_scope": delay_scope,
            "total": len(group_ids),
            "success": 0,
            "failed": 0,
            "current_index": max(0, int(start_group_idx or 0)),
            "rounds": int(rounds or 0),
            "current_round": max(1, int(start_round_idx or 0) + 1),
            "round_interval_s": int(round_interval_s or 0),
        },
    )
    return True


async def _resume_existing_task(t: Task, reason: str, start_delay: float = 0.0) -> bool:
    if _task_runner_active(t.id):
        return False
    try:
        gids = json.loads(t.group_ids_json or "[]")
    except Exception:
        gids = []
    if not gids:
        return False
    started = _start_send_task_runner(
        task_id=t.id,
        account=t.account_name,
        group_ids=gids,
        message=t.message,
        parse_mode=t.parse_mode,
        disable_web_page_preview=bool(t.disable_web_page_preview),
        delay_ms=t.delay_ms,
        rounds=t.rounds or 1,
        round_interval_s=t.round_interval_s or 0,
        start_delay=max(0.0, float(start_delay or 0.0)),
        start_round_idx=max(0, (t.current_round or 1) - 1),
        start_group_idx=max(0, (t.current_index or 0)),
        delay_scope=_normalize_delay_scope(getattr(t, "delay_scope", None), "per_account"),
    )
    if not started:
        return False

    with SessionLocal() as db:
        fresh = db.query(Task).filter(Task.id == t.id).first()
        if fresh and fresh.status == "running":
            fresh.heartbeat_at = CONFIG.now()
            db.add(TaskEvent(
                task_id=t.id,
                event="runner_started",
                detail=reason,
                meta_json=json.dumps(
                    {
                        "round": int(fresh.current_round or 1),
                        "index": int(fresh.current_index or 0),
                    },
                    ensure_ascii=False,
                ),
            ))
            try:
                db.commit()
            except DatabaseError as exc:
                db.rollback()
                _handle_task_db_error("resume_existing_task_commit", exc)
    return True


async def _handle_private_copy(account: str, event):
    try:
        msg_id = event.message.id
        chat_id = event.message.chat_id
        text = getattr(event.message, "message", "") or ""
        media_path = None
        if getattr(event.message, "media", None):
            tmp = tempfile.NamedTemporaryFile(delete=False)
            try:
                await event.message.download_media(file=tmp.name)
                media_path = tmp.name
            finally:
                try:
                    tmp.close()
                except Exception:
                    pass
        session = SessionLocal()
        try:
            LAST_COPY_MESSAGE["account"] = account
            LAST_COPY_MESSAGE["chat_id"] = chat_id
            LAST_COPY_MESSAGE["message_id"] = msg_id
        finally:
            session.close()
        accounts = list(CONFIG.ACCOUNTS.keys())
        authorized, _skipped = await _split_authorized_accounts(accounts)
        for acc in authorized:
            try:
                gids = []
                try:
                    groups = await multi_manager.get_joined_groups(acc, only_groups=True)
                    for g in groups:
                        gid = g.get("id")
                        if gid:
                            gids.append(gid)
                except Exception:
                    pass
                base = int(getattr(CONFIG, "SEND_MIN_DELAY_MS", 1500))
                jitter_pct = float(getattr(CONFIG, "SEND_JITTER_PCT", 0.15))
                db = SessionLocal()
                try:
                    for gid in gids:
                        ok = False
                        err = None
                        mid = None
                        try:
                            await multi_manager.ensure_connected(acc)
                            if media_path:
                                msg = await multi_manager.get(acc).client.send_file(entity=gid, file=media_path, caption=text)
                                mid = getattr(msg, "id", None)
                                ok = True
                            else:
                                ok, err, mid = await multi_manager.send_message_to_group(acc, gid, text=text, parse_mode=None, disable_web_page_preview=True)
                        except Exception as e:
                            err = str(e)
                            ok = False
                        title = await multi_manager.get_group_title(acc, gid)
                        db.add(SendLog(
                            account_name=acc,
                            group_id=gid,
                            group_title=title,
                            message_preview=(text or "")[:200],
                            status="success" if ok else "failed",
                            error=None if ok else err,
                            message_id=mid,
                            parse_mode=None,
                        ))
                        db.commit()
                        jitter = random.uniform(-jitter_pct, jitter_pct) * base
                        wait_ms = max(0, base + jitter)
                        await asyncio.sleep(wait_ms / 1000.0)
                finally:
                    db.close()
            except Exception:
                pass
        if media_path:
            try:
                os.unlink(media_path)
            except Exception:
                pass
    except Exception:
        pass


set_on_private_message(_handle_private_copy)


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
    delay_scope = _normalize_delay_scope(body.get("delay_scope"), "per_account")
    rounds = max(1, int(body.get("rounds", 100)))
    round_interval_s = int(body.get("round_interval_s", 600))
    account = body.get("account") or CONFIG.DEFAULT_ACCOUNT
    request_id = body.get("request_id")
    ok, _reason = _check_request_guard(token, request_id)
    if not ok:
        return JSONResponse({"detail": "Too Many Requests"}, status_code=429, headers={"Retry-After": "1"})
    authorized = await _is_authorized_account(account)
    if not authorized:
        return JSONResponse({"detail": "session_not_authorized"}, status_code=403)
    group_ids = unique_group_ids(group_ids)
    if not group_ids or not message:
        return JSONResponse({"detail": "group_ids and message required"}, status_code=400)
    task_id = uuid.uuid4().hex[:24]
    db: Session = SessionLocal()
    try:
        existing = _get_existing_tasks_by_request_id(db, request_id)
        if existing:
            return JSONResponse({
                "task_id": existing[0].id,
                "duplicate": True,
                "delay_scope": _normalize_delay_scope(getattr(existing[0], "delay_scope", None), delay_scope),
            })
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
            delay_scope=delay_scope,
            rounds=rounds,
            round_interval_s=round_interval_s,
            current_index=0,
            group_ids_json=json.dumps(group_ids),
            request_id=request_id,
            heartbeat_at=CONFIG.now(),
        )
        db.add(t)
        db.add(TaskEvent(task_id=task_id, event="created", detail="task_created", meta_json=json.dumps({"count": len(group_ids)}, ensure_ascii=False)))
        try:
            db.commit()
        except DatabaseError as exc:
            db.rollback()
            _handle_task_db_error("send_async_create", exc)
            return JSONResponse({"detail": "task_db_unavailable"}, status_code=503)
    except DatabaseError as exc:
        db.rollback()
        _handle_task_db_error("send_async_query", exc)
        return JSONResponse({"detail": "task_db_unavailable"}, status_code=503)
    finally:
        db.close()
    _start_send_task_runner(
        task_id=task_id,
        account=account,
        group_ids=group_ids,
        message=message,
        parse_mode=parse_mode,
        disable_web_page_preview=disable_web_page_preview,
        delay_ms=delay_ms,
        rounds=rounds,
        round_interval_s=round_interval_s,
        delay_scope=delay_scope,
    )
    return JSONResponse({"task_id": task_id, "delay_scope": delay_scope})


@app.route("/api/copy-receiver", methods=["POST"])
async def set_copy_receiver_account(request: Request):
    token = request.headers.get("X-Admin-Token")
    if token != CONFIG.ADMIN_TOKEN:
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)
    body = await request.json()
    account = body.get("account")
    enabled = bool(body.get("enabled", True))
    if not account:
        set_copy_receiver(None, False)
        try:
            db: Session = SessionLocal()
            try:
                row = db.query(SystemKV).filter(SystemKV.k == "copy_receiver").first()
                if not row:
                    row = SystemKV(k="copy_receiver", v=json.dumps({"account": None, "enabled": 0}, ensure_ascii=False))
                    db.add(row)
                else:
                    row.v = json.dumps({"account": None, "enabled": 0}, ensure_ascii=False)
                db.commit()
            finally:
                db.close()
        except Exception:
            pass
        return JSONResponse({"ok": True, "enabled": False})
    set_copy_receiver(account, enabled)
    try:
        db: Session = SessionLocal()
        try:
            row = db.query(SystemKV).filter(SystemKV.k == "copy_receiver").first()
            payload = {"account": account, "enabled": 1 if enabled else 0}
            if not row:
                row = SystemKV(k="copy_receiver", v=json.dumps(payload, ensure_ascii=False))
                db.add(row)
            else:
                row.v = json.dumps(payload, ensure_ascii=False)
            db.commit()
        finally:
            db.close()
    except Exception:
        pass
    return JSONResponse({"ok": True, "enabled": enabled, "account": account})


@app.route("/api/copy-latest")
async def get_copy_latest(request: Request):
    token = request.headers.get("X-Admin-Token")
    if token != CONFIG.ADMIN_TOKEN:
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)
    data = {
        "account": LAST_COPY_MESSAGE.get("account"),
        "chat_id": LAST_COPY_MESSAGE.get("chat_id"),
        "message_id": LAST_COPY_MESSAGE.get("message_id"),
    }
    return JSONResponse(data)


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
            fallback = _runtime_task_status_payload(task_id)
            if fallback:
                return JSONResponse(fallback)
            return JSONResponse({"detail": "Not Found"}, status_code=404)
        counters = _collect_task_log_counters(
            db,
            [t.id],
            {t.id: int(t.current_round or 0)},
            {t.id: t},
        ).get(t.id, {})

        success = int(counters.get("success", 0))
        failed = int(counters.get("failed", 0))
        overall_completed = int(counters.get("overall_completed", 0))
        current_round_planned = _planned_group_count(t.group_ids_json)
        current_round_completed = min(
            max(0, int(counters.get("current_round_completed", 0))),
            current_round_planned,
        )
        current_round_sent = current_round_completed
        previous_rounds_sent = max(0, overall_completed - current_round_completed)
        overall_planned = previous_rounds_sent + current_round_planned

        data = {
            "task_id": t.id,
            "status": t.status,
            "total": t.total,
            "delay_scope": _normalize_delay_scope(getattr(t, "delay_scope", None), "per_account"),
            "success": success,
            "failed": failed,
            "current_index": current_round_completed,
            "current_round_planned": current_round_planned,
            "current_round_sent": current_round_sent,
            "current_round_completed": current_round_completed,
            "previous_rounds_completed": previous_rounds_sent,
            "overall_planned": overall_planned,
            "overall_completed": overall_completed,
            "started_at": t.started_at.isoformat() if t.started_at else None,
            "finished_at": t.finished_at.isoformat() if t.finished_at else None,
            "rounds": t.rounds,
            "current_round": t.current_round,
            "round_interval_s": t.round_interval_s,
            "next_round_at": t.next_round_at.isoformat() if t.next_round_at else None,
        }
        return JSONResponse(data)
    except DatabaseError as exc:
        db.rollback()
        _handle_task_db_error("task_status", exc)
        fallback = _runtime_task_status_payload(task_id)
        if fallback:
            return JSONResponse(fallback)
        return JSONResponse({"detail": "task_db_unavailable"}, status_code=503)
    finally:
        db.close()

@app.route("/api/tasks/summary")
async def tasks_summary(request: Request):
    token = request.headers.get("X-Admin-Token")
    if token != CONFIG.ADMIN_TOKEN:
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)
    db: Session = SessionLocal()
    try:
        rows = (
            db.query(Task)
            .order_by(Task.started_at.desc())
            .limit(300)
            .all()
        )
        selected_tasks: dict[str, Task] = {}
        for t in rows:
            k = t.account_name
            existing = selected_tasks.get(k)
            current_ts = t.heartbeat_at or t.finished_at or t.started_at
            existing_ts = None
            if existing:
                existing_ts = existing.heartbeat_at or existing.finished_at or existing.started_at

            # Running tasks always win. Otherwise keep the most recent task
            # so the monitor panel can still show failed/stopped/done tasks.
            should_replace = False
            if not existing:
                should_replace = True
            elif existing.status != "running" and t.status == "running":
                should_replace = True
            elif existing.status != "running" and t.status != "running":
                if current_ts and ((not existing_ts) or current_ts > existing_ts):
                    should_replace = True

            if not should_replace:
                continue
            selected_tasks[k] = t

        current_round_by_task = {
            t.id: int(t.current_round or 0)
            for t in selected_tasks.values()
            if t and t.id
        }
        counters_by_task = _collect_task_log_counters(
            db,
            list(current_round_by_task.keys()),
            current_round_by_task,
            {t.id: t for t in selected_tasks.values() if t and t.id},
        )

        data = []
        for account, t in selected_tasks.items():
            current_ts = t.heartbeat_at or t.finished_at or t.started_at
            round_total = _planned_group_count(t.group_ids_json)
            counters = counters_by_task.get(t.id, {})
            round_completed = min(
                max(0, int(counters.get("current_round_completed", 0))),
                round_total,
            )
            overall_completed = int(counters.get("overall_completed", 0))
            previous_rounds_sent = max(0, overall_completed - round_completed)
            overall_planned = previous_rounds_sent + round_total
            next_round_at = t.next_round_at.isoformat() if t.next_round_at else None
            wait_seconds = 0
            status = t.status
            if t.status == "running" and round_total == 0:
                status = "no_sendable_groups"
            elif t.status == "running" and t.next_round_at:
                target = t.next_round_at
                now = CONFIG.now()
                if target.tzinfo is None and now.tzinfo is not None:
                    target = target.replace(tzinfo=now.tzinfo)
                wait_seconds = max(0, int((target - now).total_seconds()))
                if wait_seconds > 0 and round_total > 0 and round_completed >= round_total:
                    status = "waiting_next_round"

            data.append({
                "account": account,
                "status": status,
                "task_id": t.id,
                "delay_scope": _normalize_delay_scope(getattr(t, "delay_scope", None), "per_account"),
                "tasks_count": 1,
                "total": round_total,
                "success": int(counters.get("success", 0)),
                "failed": int(counters.get("failed", 0)),
                "completed": round_completed,
                "progress_total": round_total,
                "progress_completed": round_completed,
                "current_round": int(t.current_round or 0),
                "rounds": int(t.rounds or 0),
                "current_round_planned": round_total,
                "current_round_sent": round_completed,
                "overall_planned": overall_planned,
                "overall_completed": overall_completed,
                "next_round_at": next_round_at,
                "wait_seconds": wait_seconds,
                "last_updated_at": current_ts.isoformat() if current_ts else None,
            })
        data.sort(key=lambda x: x.get("last_updated_at") or "", reverse=True)
        return JSONResponse(data)
    except DatabaseError as exc:
        db.rollback()
        _handle_task_db_error("tasks_summary", exc)
        return JSONResponse(_runtime_task_summary_rows())
    finally:
        db.close()

@app.route("/api/tasks")
async def list_tasks(request: Request):
    token = request.headers.get("X-Admin-Token")
    if token != CONFIG.ADMIN_TOKEN:
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)
    db: Session = SessionLocal()
    try:
        rows = db.query(Task).order_by(Task.started_at.desc()).limit(100).all()
        data = [
            {
                "task_id": r.id,
                "status": r.status,
                "total": r.total,
                "success": r.success,
                "failed": r.failed,
                "current_index": r.current_index,
                "account": r.account_name,
                "delay_scope": _normalize_delay_scope(getattr(r, "delay_scope", None), "per_account"),
                "started_at": r.started_at.isoformat() if r.started_at else None,
                "finished_at": r.finished_at.isoformat() if r.finished_at else None,
                "rounds": getattr(r, "rounds", None),
                "current_round": getattr(r, "current_round", None),
                "next_round_at": r.next_round_at.isoformat() if getattr(r, "next_round_at", None) else None,
            }
            for r in rows
        ]
        return JSONResponse(data)
    except DatabaseError as exc:
        db.rollback()
        _handle_task_db_error("list_tasks", exc)
        return JSONResponse(_runtime_task_list_rows())
    finally:
        db.close()

@app.route("/api/task-events")
async def task_events(request: Request):
    token = request.headers.get("X-Admin-Token")
    if token != CONFIG.ADMIN_TOKEN:
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)
    task_id = request.query_params.get("task_id")
    page = int(request.query_params.get("page", 1))
    size = int(request.query_params.get("size", 50))
    if not task_id:
        return JSONResponse({"detail": "task_id required"}, status_code=400)
    db: Session = SessionLocal()
    try:
        q = db.query(TaskEvent).filter(TaskEvent.task_id == task_id).order_by(TaskEvent.ts.desc())
        rows = q.offset((page - 1) * size).limit(size).all()
        data = [
            {
                "id": r.id,
                "task_id": r.task_id,
                "ts": r.ts.isoformat() if r.ts else None,
                "event": r.event,
                "detail": r.detail,
                "meta_json": r.meta_json,
            }
            for r in rows
        ]
        return JSONResponse({"page": page, "size": size, "items": data})
    finally:
        db.close()

@app.route("/api/tasks/stop-all", methods=["POST"])
async def stop_all_tasks(request: Request):
    token = request.headers.get("X-Admin-Token")
    if token != CONFIG.ADMIN_TOKEN:
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        body = {}
    account = (body.get("account") or "").strip()
    db: Session = SessionLocal()
    try:
        q = db.query(Task).filter(Task.status == "running")
        if account:
            q = q.filter(Task.account_name == account)
        rows = q.all()
        for t in rows:
            t.stop_requested = 1
            db.add(TaskEvent(task_id=t.id, event="stop_requested", detail="task_stop_requested", meta_json=json.dumps({"account": t.account_name}, ensure_ascii=False)))
        db.commit()
        return JSONResponse({"ok": True, "tasks_marked": len(rows), "account": account or None})
    finally:
        db.close()


@app.route("/api/tasks/delete", methods=["POST"])
async def delete_task(request: Request):
    token = request.headers.get("X-Admin-Token")
    if token != CONFIG.ADMIN_TOKEN:
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"detail": "Invalid JSON"}, status_code=400)
    task_id = (body.get("task_id") or "").strip()
    if not task_id:
        return JSONResponse({"detail": "task_id required"}, status_code=400)
    db: Session = SessionLocal()
    try:
        task = db.query(Task).filter(Task.id == task_id).first()
        if not task:
            return JSONResponse({"detail": "Not Found"}, status_code=404)
        events_deleted = (
            db.query(TaskEvent)
            .filter(TaskEvent.task_id == task_id)
            .delete(synchronize_session=False)
        )
        db.delete(task)
        db.commit()
        return JSONResponse({
            "ok": True,
            "deleted_task_id": task_id,
            "events_deleted": int(events_deleted or 0),
        })
    finally:
        db.close()


@app.route("/api/tasks/clear", methods=["POST"])
async def clear_all_tasks(request: Request):
    token = request.headers.get("X-Admin-Token")
    if token != CONFIG.ADMIN_TOKEN:
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)
    db: Session = SessionLocal()
    try:
        task_ids = [row[0] for row in db.query(Task.id).all()]
        events_deleted = 0
        if task_ids:
            events_deleted = (
                db.query(TaskEvent)
                .filter(TaskEvent.task_id.in_(task_ids))
                .delete(synchronize_session=False)
            )
        tasks_deleted = db.query(Task).delete(synchronize_session=False)
        db.commit()
        return JSONResponse({
            "ok": True,
            "tasks_deleted": int(tasks_deleted or 0),
            "events_deleted": int(events_deleted or 0),
        })
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
    start_round_idx: int = 0,
    start_group_idx: int = 0,
):
    """带延迟启动的发送任务包装器"""
    if start_delay > 0:
        _update_runtime_task_state(task_id, status="scheduled")
        print(f"[TASK] {account}: waiting {start_delay:.1f}s before starting...")
        if not await _sleep_with_task_checks(task_id, start_delay, refresh_heartbeat=True):
            return
    print(f"[TASK] {account}: starting send task (task_id={task_id[:8]}...)")
    _update_runtime_task_state(task_id, status="running")
    await _run_send_task(task_id, account, group_ids, message, parse_mode, disable_web_page_preview, delay_ms, rounds, round_interval_s, start_round_idx, start_group_idx)


async def _run_send_task(task_id: str, account: str, group_ids: list[int], message: str, parse_mode: str, disable_web_page_preview: bool, delay_ms: int, rounds: int, round_interval_s: int, start_round_idx: int = 0, start_group_idx: int = 0):
    consecutive_failures = 0
    MAX_CONSECUTIVE_FAILURES = int(getattr(CONFIG, "MAX_CONSECUTIVE_FAILURES", 10))
    active_group_ids = unique_group_ids(group_ids)

    def _should_defer_group(error_text: str | None) -> bool:
        if not error_text:
            return True
        err = str(error_text).lower()
        if should_add_to_blist(err):
            return False
        hard_fail_markers = (
            "peer error",
            "invalid peer",
            "could not find the input entity",
            "channel_private",
            "session database corrupted",
            "not authorized",
        )
        return not any(marker in err for marker in hard_fail_markers)
    
    try:
        for i in range(start_round_idx, rounds):
            current_round = i + 1
            
            # --- Round Setup ---
            resume_current_round = (i == start_round_idx and start_group_idx > 0)
            with SessionLocal() as db:
                t = db.query(Task).filter(Task.id == task_id).first()
                if not t:
                    print(f"[TASK] {account}: Task record missing, terminating task loop")
                    _update_runtime_task_state(task_id, status="stopped", finished_at=_runtime_state_iso_now())
                    return
                else:
                    t.current_round = current_round
                    # 每轮开始时重置索引（无论是新轮次还是从恢复点继续）
                    if i == start_round_idx and start_group_idx > 0:
                        # 恢复任务，从断点继续
                        pass  # 保留 current_index 用于恢复逻辑
                    else:
                        # 新轮次开始，重置索引
                        t.current_index = 0

                active_group_ids = _filter_account_banned_group_ids(db, account, active_group_ids)
                if not active_group_ids:
                    t.status = "completed"
                    t.finished_at = CONFIG.now()
                    t.next_round_at = None
                    t.group_ids_json = "[]"
                    db.add(TaskEvent(task_id=task_id, event="completed", detail="no_sendable_groups", meta_json=json.dumps({"account": account}, ensure_ascii=False)))
                    db.commit()
                    _update_runtime_task_state(task_id, status="completed", total=0, finished_at=_runtime_state_iso_now())
                    return

                if resume_current_round:
                    ids = list(active_group_ids)
                elif int(getattr(CONFIG, "SMART_SCHEDULER_ENABLED", 1)) == 1:
                    grades = classify_groups(db, account, active_group_ids)
                    role = classify_account(db, account)
                    ids = select_groups_for_account(role, active_group_ids, grades)
                else:
                    ids = list(active_group_ids)
                    random.shuffle(ids)
                ids = unique_group_ids(ids)
                if not resume_current_round:
                    ids = sort_groups_for_account(db, account, ids)
                t.group_ids_json = json.dumps(ids)
                db.commit()
            _update_runtime_task_state(
                task_id,
                status="running",
                current_round=int(current_round),
                total=len(ids),
            )
            deferred_once: set[int] = set()
            
            # Resumption logic: derive the real resume point from send_logs.
            # Because deferred/blacklisted groups reorder the working list during
            # a round, the persisted current_index no longer matches the array
            # position. So on resume we skip any group that already has a
            # "success" SendLog for this round and continue from the first one
            # that does not, avoiding duplicate sends and skipped groups.
            start_pos = 0
            if i == start_round_idx and start_group_idx > 0:
                print(f"[TASK] {account}: Resuming round {current_round} from index {start_group_idx}")
                if not ids:
                    ids = []
                else:
                    with SessionLocal() as db:
                        sent_success_in_round = {
                            row[0]
                            for row in db.query(SendLog.group_id)
                            .filter(
                                SendLog.task_id == task_id,
                                SendLog.task_round == current_round,
                                SendLog.status == "success",
                            )
                            .all()
                        }
                    start_pos = next(
                        (pos for pos, gid in enumerate(ids) if gid not in sent_success_in_round),
                        len(ids),
                    )
                    if start_pos >= len(ids):
                        # 本轮所有群都已成功发送，直接结束当前轮
                        ids = []
                        start_pos = 0
            
            print(f"[TASK] {account}: Processing round {current_round}, {max(0, len(ids) - start_pos)} groups remaining")

            # --- Process Groups ---
            pos = start_pos
            while pos < len(ids):
                gid = ids[pos]
                task_delay_scope = "per_account"
                # Use a fresh session for each message to avoid long-lived session issues
                with SessionLocal() as db:
                    t = db.query(Task).filter(Task.id == task_id).first()
                    
                    # If task row has been deleted (e.g., system reset), terminate immediately
                    if not t:
                        print(f"[TASK] {account}: Task record deleted, stopping immediately")
                        _update_runtime_task_state(task_id, status="stopped", finished_at=_runtime_state_iso_now())
                        return
                    
                    # Check stop/pause
                    if t and t.stop_requested:
                        print(f"[TASK] {account}: Stop requested")
                        t.status = "stopped"
                        t.finished_at = CONFIG.now()
                        db.add(TaskEvent(task_id=task_id, event="stopped", detail="task_stopped", meta_json=json.dumps({}, ensure_ascii=False)))
                        db.commit()
                        _update_runtime_task_state(task_id, status="stopped", finished_at=_runtime_state_iso_now())
                        return

                    while t and t.paused:
                        print(f"[TASK] {account}: Paused")
                        db.commit() # Commit any pending
                        db.close() # Close while sleeping
                        await asyncio.sleep(1)
                        db = SessionLocal() # Re-open
                        t = db.query(Task).filter(Task.id == task_id).first()
                    
                    # Check again after pause loop
                    if not t or (t and t.stop_requested):
                        print(f"[TASK] {account}: Stop requested after pause")
                        if t:
                            t.status = "stopped"
                            t.finished_at = CONFIG.now()
                            db.add(TaskEvent(task_id=task_id, event="stopped", detail="task_stopped", meta_json=json.dumps({}, ensure_ascii=False)))
                            db.commit()
                        _update_runtime_task_state(task_id, status="stopped", finished_at=_runtime_state_iso_now())
                        return

                    task_delay_scope, task_delay_ms = _effective_task_timing(db, t, task_id, delay_ms)
                    if task_delay_scope == "global":
                        wait_ms = _compute_effective_task_delay_ms(account, task_delay_ms)
                        bucket_key = _global_send_bucket_key(t, task_id)
                        if not await _reserve_global_send_slot(task_id, bucket_key, wait_ms / 1000.0):
                            return

                    # Send Logic
                    send_text = message
                    if int(getattr(CONFIG, "SMART_SCHEDULER_ENABLED", 1)) == 1:
                        send_text = randomize_message(message)
                    
                    # Call async send (no DB involved here)
                    # print(f"[TASK] {account}: Sending to group {gid}")
                    ok, err, msg_id = await multi_manager.send_message_to_group(
                        account,
                        group_id=gid,
                        text=send_text,
                        parse_mode=parse_mode,
                        disable_web_page_preview=disable_web_page_preview,
                    )
                    
                    # Logging
                    status = "success" if ok else "failed"
                    if not ok:
                        print(f"[TASK] {account}: Failed to send to {gid}: {err}")
                    preview = message[:200]
                    title = await multi_manager.get_group_title(account, gid)

                    log = SendLog(
                        task_id=task_id,
                        task_round=current_round,
                        account_name=account,
                        group_id=gid,
                        group_title=title,
                        message_preview=preview,
                        status=status,
                        error=None if ok else (err or "send_failed"),
                        message_id=msg_id,
                        parse_mode=parse_mode,
                        created_at=CONFIG.now(),
                    )
                    db.add(log)
                    
                    t = db.query(Task).filter(Task.id == task_id).first()
                    if t:
                        finalized = False
                        advance_pos = True
                        if ok:
                            t.success = (t.success or 0) + 1
                            consecutive_failures = 0
                            finalized = True
                        else:
                            consecutive_failures += 1
                            if should_add_to_blist(err):
                                try:
                                    add_banned_group(db, account, gid, global_scope=should_add_to_global_blist(err))
                                except Exception:
                                    pass
                                active_group_ids = _remove_group_id_once(active_group_ids, gid)
                                ids.pop(pos)
                                t.group_ids_json = json.dumps(ids)
                                advance_pos = False
                                db.add(TaskEvent(
                                    task_id=task_id,
                                    event="skip",
                                    detail="group_blocked_for_account",
                                    meta_json=json.dumps({"gid": gid, "error": err, "account": account}, ensure_ascii=False),
                                ))
                            if gid not in deferred_once and _should_defer_group(err):
                                deferred_once.add(gid)
                                ids.pop(pos)
                                ids.append(gid)
                                t.group_ids_json = json.dumps(ids)
                                db.add(TaskEvent(
                                    task_id=task_id,
                                    event="deferred",
                                    detail="group_deferred_to_tail",
                                    meta_json=json.dumps({"gid": gid, "error": err}, ensure_ascii=False),
                                ))
                            else:
                                t.failed = (t.failed or 0) + 1
                                finalized = True

                        if finalized:
                            t.current_index = (t.current_index or 0) + 1
                            if advance_pos:
                                pos += 1
                        t.heartbeat_at = CONFIG.now()
                        # 获取当前轮次的实际计划数
                        current_round_planned = _planned_group_count(t.group_ids_json)
                        # 计算当前轮次已完成数
                        current_round_completed = min(int(t.current_index or 0), current_round_planned)
                        # 计算前面轮次的累计完成数
                        previous_rounds_completed = max(0, int((t.success or 0) + (t.failed or 0)) - current_round_completed)
                        # 总体进度：前面轮次累计 + 当前轮次实际计划数
                        overall_total = previous_rounds_completed + current_round_planned
                        overall_completed = previous_rounds_completed + current_round_completed
                        _update_runtime_task_state(
                            task_id,
                            status="running",
                            success=int(t.success or 0),
                            failed=int(t.failed or 0),
                            current_index=int(t.current_index or 0),
                            current_round=int(t.current_round or current_round),
                            total=int(current_round_planned or len(ids)),
                        )
                        db.add(TaskEvent(task_id=task_id, event="progress", detail=f"{overall_completed}/{overall_total}", meta_json=json.dumps({"gid": gid, "round": t.current_round, "round_completed": current_round_completed, "round_planned": current_round_planned}, ensure_ascii=False)))
                    
                    # Auto-pause logic
                    if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                         db.add(TaskEvent(
                            task_id=task_id, 
                            event="auto_pause", 
                            detail=f"sleeping_10m_failures_{consecutive_failures}",
                            meta_json=json.dumps({"consecutive_failures": consecutive_failures, "last_error": err}, ensure_ascii=False)
                        ))
                    
                    try:
                        db.commit()
                    except DatabaseError as exc:
                        db.rollback()
                        _handle_task_db_error("task_runner_commit", exc)
                        _update_runtime_task_state(
                            task_id,
                            status="error",
                            last_error=f"task_db_write_error: {str(exc)[:160]}",
                            finished_at=_runtime_state_iso_now(),
                        )
                        return
                
                # Handle auto-pause outside of DB session
                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    if not await _sleep_with_task_checks(task_id, 600, refresh_heartbeat=True):
                        return
                    consecutive_failures = 0
                    continue

                # Sleep between messages
                if task_delay_scope != "global":
                    wait_ms = _compute_effective_task_delay_ms(account, task_delay_ms)
                    if wait_ms > 0:
                        if not await _sleep_with_task_checks(task_id, wait_ms / 1000.0, refresh_heartbeat=True):
                            return

            # --- End of Round ---
            if not active_group_ids:
                with SessionLocal() as db:
                    t = db.query(Task).filter(Task.id == task_id).first()
                    if t:
                        t.status = "completed"
                        t.finished_at = CONFIG.now()
                        t.next_round_at = None
                        t.group_ids_json = "[]"
                        db.add(TaskEvent(
                            task_id=task_id,
                            event="completed",
                            detail="no_sendable_groups",
                            meta_json=json.dumps({"account": account, "round": current_round}, ensure_ascii=False),
                        ))
                        try:
                            db.commit()
                        except DatabaseError as exc:
                            db.rollback()
                            _handle_task_db_error("task_complete_no_groups", exc)
                _update_runtime_task_state(task_id, status="completed", total=0, finished_at=_runtime_state_iso_now(), next_round_at=None)
                return

            if current_round < rounds:
                sleep_time = round_interval_s
                update_db_time = True
                
                # Resuming a finished round?
                if i == start_round_idx and len(ids) == 0:
                    with SessionLocal() as db:
                        t = db.query(Task).filter(Task.id == task_id).first()
                        if t and t.next_round_at:
                            target = t.next_round_at
                            if target.tzinfo is None:
                                # Assume it's in the configured timezone
                                try:
                                    tz = ZoneInfo(getattr(CONFIG, "TIMEZONE", "UTC"))
                                except Exception:
                                    tz = timezone.utc
                                target = target.replace(tzinfo=tz)
                            
                            now = datetime.now(target.tzinfo)
                            remaining = (target - now).total_seconds()
                            
                            if remaining <= 0:
                                sleep_time = 0
                                update_db_time = False
                                print(f"[TASK] {account}: Skipping sleep, next round was due at {target}")
                            else:
                                sleep_time = remaining
                                update_db_time = False
                                print(f"[TASK] {account}: Sleeping remaining {remaining:.1f}s until {target}")

                if update_db_time:
                    with SessionLocal() as db:
                        t = db.query(Task).filter(Task.id == task_id).first()
                        if t:
                            t.next_round_at = CONFIG.now() + timedelta(seconds=round_interval_s)
                            try:
                                db.commit()
                            except DatabaseError as exc:
                                db.rollback()
                                _handle_task_db_error("task_runner_next_round_commit", exc)
                                _update_runtime_task_state(task_id, next_round_at=t.next_round_at.isoformat() if t.next_round_at else None)
                
                if sleep_time > 0:
                    _update_runtime_task_state(task_id, next_round_at=(CONFIG.now() + timedelta(seconds=sleep_time)).isoformat())
                    if not await _sleep_with_task_checks(task_id, sleep_time, refresh_heartbeat=True):
                        return

        # --- Task Done ---
        with SessionLocal() as db:
            t = db.query(Task).filter(Task.id == task_id).first()
            if t and t.status not in ("stopped", "error"):
                t.status = "done"
                t.finished_at = CONFIG.now()
                db.add(TaskEvent(task_id=task_id, event="finished", detail="task_done", meta_json=json.dumps({}, ensure_ascii=False)))
                try:
                    db.commit()
                except DatabaseError as exc:
                    db.rollback()
                    _handle_task_db_error("task_runner_finish_commit", exc)
        _update_runtime_task_state(task_id, status="done", finished_at=_runtime_state_iso_now())

    except Exception as e:
        _update_runtime_task_state(task_id, status="error", last_error=str(e)[:200], finished_at=_runtime_state_iso_now())
        try:
            with SessionLocal() as db:
                t = db.query(Task).filter(Task.id == task_id).first()
                if t:
                    t.status = "error"
                    t.finished_at = CONFIG.now()
                    db.add(TaskEvent(task_id=task_id, event="error", detail=f"task_error: {e}", meta_json=json.dumps({}, ensure_ascii=False)))
                    db.commit()
        except Exception:
            print(f"CRITICAL: Failed to log error to DB for task {task_id}: {e}")

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
        try:
            db: Session = SessionLocal()
            try:
                row = db.query(SystemKV).filter(SystemKV.k == f"login_phone:{account}").first()
                if row and row.v:
                    try:
                        d = json.loads(row.v)
                        ph = (d.get("phone") or "").strip()
                        if ph:
                            phone = ph
                    except Exception:
                        pass
            finally:
                db.close()
        except Exception:
            pass
        if not phone:
            return JSONResponse({"detail": "phone required"}, status_code=400)
    try:
        resp = await multi_manager.send_login_code(account, phone, force_sms=force_sms, force_new_session=force_new_session)
        try:
            if isinstance(resp, dict) and not resp.get("ok", True):
                if "retry_after" in resp:
                    return JSONResponse(resp, status_code=429, headers={"Retry-After": str(resp.get("retry_after", 60))})
                err = str(resp.get("error", "")).lower()
                if "phone_invalid" in err or "phone" in err:
                    return JSONResponse(resp, status_code=400)
                return JSONResponse(resp, status_code=500)
        except Exception:
            pass
        return JSONResponse(resp)
    except Exception as e:
        return JSONResponse({"detail": str(e)}, status_code=500)


@app.route("/api/login/default-phone", methods=["POST"])
async def set_default_phone(request: Request):
    token = request.headers.get("X-Admin-Token")
    if token != CONFIG.ADMIN_TOKEN:
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)
    body = await request.json()
    account = (body.get("account") or "").strip()
    phone = (body.get("phone") or "").strip()
    if not account or not phone:
        return JSONResponse({"detail": "account and phone required"}, status_code=400)
    db: Session = SessionLocal()
    try:
        row = db.query(SystemKV).filter(SystemKV.k == f"login_phone:{account}").first()
        payload = {"phone": phone}
        if not row:
            row = SystemKV(k=f"login_phone:{account}", v=json.dumps(payload, ensure_ascii=False))
            db.add(row)
        else:
            row.v = json.dumps(payload, ensure_ascii=False)
        db.commit()
        return JSONResponse({"ok": True})
    finally:
        db.close()


@app.route("/api/login/default-phone")
async def get_default_phone(request: Request):
    token = request.headers.get("X-Admin-Token")
    if token != CONFIG.ADMIN_TOKEN:
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)
    account = (request.query_params.get("account") or "").strip()
    if not account:
        return JSONResponse({"detail": "account required"}, status_code=400)
    db: Session = SessionLocal()
    try:
        row = db.query(SystemKV).filter(SystemKV.k == f"login_phone:{account}").first()
        phone = None
        if row and row.v:
            try:
                d = json.loads(row.v)
                phone = (d.get("phone") or "").strip() or None
            except Exception:
                phone = None
        return JSONResponse({"account": account, "phone": phone})
    finally:
        db.close()


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
        return JSONResponse({"authorized": await _is_authorized_account(account)})
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
        "delay_scope": "per_account",  // 批量群发默认每个账号各自发送
        "rounds": 100,
        "round_interval_s": 600,
        "stagger_min_s": 0,  // 账号启动间隔最小秒数
        "stagger_max_s": 1   // 账号启动间隔最大秒数
    }
    """
    token = request.headers.get("X-Admin-Token")
    if token != CONFIG.ADMIN_TOKEN:
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)
    
    body = await request.json()
    strategy = str(body.get("strategy") or "full_broadcast_per_account").strip().lower()
    group_ids = body.get("group_ids") or []
    distribution_map = body.get("distribution_map") or {}
    message = (body.get("message") or "").strip()
    parse_mode = body.get("parse_mode") or "plain"
    disable_web_page_preview = bool(body.get("disable_web_page_preview", True))
    delay_ms = int(body.get("delay_ms", 11000))  # 默认 11 秒
    delay_ms = max(delay_ms, getattr(CONFIG, "SEND_MIN_DELAY_MS", 1500))
    delay_scope = _normalize_delay_scope(body.get("delay_scope"), "per_account")
    rounds = max(1, int(body.get("rounds", 100)))
    round_interval_s = int(body.get("round_interval_s", 600))
    # ponytail: 保留极小错峰，避免所有账号同一瞬间起跑。
    stagger_min_s = float(body.get("stagger_min_s", 0))
    stagger_max_s = float(body.get("stagger_max_s", 1))
    request_id = body.get("request_id")
    
    # 防重复请求
    ok, _reason = _check_request_guard(token, request_id)
    if not ok:
        return JSONResponse({"detail": "Too Many Requests"}, status_code=429, headers={"Retry-After": "1"})
    
    if not message:
        return JSONResponse({"detail": "message required"}, status_code=400)
    
    # 获取账号列表
    accounts = _candidate_accounts(body.get("accounts") or [])
    if not accounts:
        return JSONResponse({"detail": "no_accounts_available"}, status_code=400)
    authorized_accounts, skipped_unauthorized_accounts = await _split_authorized_accounts(accounts)

    if not authorized_accounts:
        return JSONResponse({
            "detail": "no_authorized_accounts",
            "skipped_unauthorized_accounts": skipped_unauthorized_accounts,
        }, status_code=403)
    accounts = authorized_accounts
    
    if strategy == "distributed_join_only":
        if not isinstance(distribution_map, dict):
            return JSONResponse({"detail": "distribution_map required"}, status_code=400)
        planned_distribution = {
            acc: unique_group_ids(distribution_map.get(acc) or [])
            for acc in accounts
        }
        full_group_ids = unique_group_ids([
            gid
            for gids in planned_distribution.values()
            for gid in gids
        ])
        if not full_group_ids:
            return JSONResponse({"detail": "no_groups_available_for_distributed_join_mode"}, status_code=400)
    else:
        # 批量群发默认应让每个账号都发送完整的所选群组列表。
        # 之前这里按账号对群组做了唯一分摊，导致用户选了 100+ 个群后，
        # 每个账号只拿到十几个群，表现成“跑了两轮却只有 20 多条成功记录”。
        full_group_ids = unique_group_ids(group_ids)
        if not full_group_ids:
            return JSONResponse({"detail": "group_ids required"}, status_code=400)
        planned_distribution = {acc: list(full_group_ids) for acc in accounts}

    # 为每个账号创建单独任务；提前过滤账号级禁发群，避免创建“空气任务”。
    task_ids = []
    skipped_no_sendable_accounts: list[str] = []
    db: Session = SessionLocal()
    try:
        existing = _get_existing_tasks_by_request_id(db, request_id)
        if existing:
            existing_tasks = []
            for t in existing:
                try:
                    existing_group_ids = json.loads(t.group_ids_json or "[]")
                except Exception:
                    existing_group_ids = []
                existing_tasks.append({
                    "account": t.account_name,
                    "task_id": t.id,
                    "group_ids": existing_group_ids,
                })
            return JSONResponse({
                "tasks": existing_tasks,
                "accounts_count": len(existing_tasks),
                "planned_groups": sum(len(it["group_ids"]) for it in existing_tasks),
                "unique_groups": len(full_group_ids),
                "strategy": strategy,
                "delay_scope": delay_scope,
                "duplicate": True,
                "stagger_min_s": stagger_min_s,
                "stagger_max_s": stagger_max_s,
                "skipped_unauthorized_accounts": skipped_unauthorized_accounts,
                "skipped_no_sendable_accounts": skipped_no_sendable_accounts,
            })
        filtered_distribution: dict[str, list[int]] = {}
        for acc in accounts:
            acc_group_ids = _filter_account_banned_group_ids(
                db,
                acc,
                unique_group_ids(planned_distribution.get(acc) or []),
            )
            if not acc_group_ids:
                skipped_no_sendable_accounts.append(acc)
                continue
            filtered_distribution[acc] = acc_group_ids
        if not filtered_distribution:
            return JSONResponse({
                "detail": "no_sendable_groups_for_accounts",
                "skipped_unauthorized_accounts": skipped_unauthorized_accounts,
                "skipped_no_sendable_accounts": skipped_no_sendable_accounts,
            }, status_code=400)
        planned_distribution = filtered_distribution
        accounts = list(filtered_distribution.keys())
        full_group_ids = unique_group_ids([
            gid
            for gids in planned_distribution.values()
            for gid in gids
        ])
        for acc in accounts:
            acc_group_ids = planned_distribution.get(acc, [])
            if not acc_group_ids:
                continue
            task_id = uuid.uuid4().hex[:24]
            t = Task(
                id=task_id,
                status="running",
                total=len(acc_group_ids),
                success=0,
                failed=0,
                account_name=acc,  # 单个账号名
                message=message,
                parse_mode=parse_mode,
                disable_web_page_preview=1 if disable_web_page_preview else 0,
                delay_ms=delay_ms,
                delay_scope=delay_scope,
                rounds=rounds,
                round_interval_s=round_interval_s,
                current_index=0,
                group_ids_json=json.dumps(acc_group_ids),
                request_id=request_id,
                heartbeat_at=CONFIG.now(),
            )
            db.add(t)
            db.add(TaskEvent(
                task_id=task_id, 
                event="created", 
                detail="task_created",
                meta_json=json.dumps({"count": len(acc_group_ids)}, ensure_ascii=False)
            ))
            task_ids.append({"account": acc, "task_id": task_id, "group_ids": acc_group_ids})
        try:
            db.commit()
        except DatabaseError as exc:
            db.rollback()
            _handle_task_db_error("send_async_batch_create", exc)
            return JSONResponse({"detail": "task_db_unavailable"}, status_code=503)
    except DatabaseError as exc:
        db.rollback()
        _handle_task_db_error("send_async_batch_query", exc)
        return JSONResponse({"detail": "task_db_unavailable"}, status_code=503)
    finally:
        db.close()

    if not task_ids:
        return JSONResponse({
            "detail": "no_groups_available_after_planning",
            "message": "当前群都在冷却期内，暂时不安排重复发送",
        }, status_code=400)
    
    if int(getattr(CONFIG, "SMART_SCHEDULER_ENABLED", 1)) == 1:
        db2: Session = SessionLocal()
        try:
            items = []
            for it in task_ids:
                role = classify_account(db2, it["account"])
                items.append({
                    "account": it["account"],
                    "task_id": it["task_id"],
                    "role": role,
                    "group_ids": it["group_ids"],
                })
            role_order = {"SAFE": 0, "CORE": 1, "RISK": 2}
            items.sort(key=lambda x: role_order.get(x["role"], 1))
            cumulative_delay = 0.0
            for i, it in enumerate(items):
                if i > 0:
                    factor = 1.0
                    if it["role"] == "SAFE":
                        factor = 0.6
                    elif it["role"] == "CORE":
                        factor = 1.0
                    elif it["role"] == "RISK":
                        factor = 1.6
                    cumulative_delay += random.uniform(stagger_min_s, stagger_max_s) * factor
                _start_send_task_runner(
                    task_id=it["task_id"],
                    account=it["account"],
                    group_ids=it["group_ids"],
                    message=message,
                    parse_mode=parse_mode,
                    disable_web_page_preview=disable_web_page_preview,
                    delay_ms=delay_ms,
                    rounds=rounds,
                    round_interval_s=round_interval_s,
                    start_delay=cumulative_delay,
                    delay_scope=delay_scope,
                )
        finally:
            db2.close()
    else:
        cumulative_delay = 0.0
        for i, item in enumerate(task_ids):
            if i > 0:
                cumulative_delay += random.uniform(stagger_min_s, stagger_max_s)
            _start_send_task_runner(
                task_id=item["task_id"],
                account=item["account"],
                group_ids=item["group_ids"],
                message=message,
                parse_mode=parse_mode,
                disable_web_page_preview=disable_web_page_preview,
                delay_ms=delay_ms,
                rounds=rounds,
                round_interval_s=round_interval_s,
                start_delay=cumulative_delay,
                delay_scope=delay_scope,
            )
    
    return JSONResponse({
        "tasks": task_ids,
        "accounts_count": len(task_ids),
        "requested_accounts_count": len(accounts) + len(skipped_no_sendable_accounts),
        "planned_groups": sum(len(it["group_ids"]) for it in task_ids),
        "unique_groups": len(full_group_ids),
        "strategy": strategy,
        "delay_scope": delay_scope,
        "stagger_min_s": stagger_min_s,
        "stagger_max_s": stagger_max_s,
        "skipped_unauthorized_accounts": skipped_unauthorized_accounts,
        "skipped_no_sendable_accounts": skipped_no_sendable_accounts,
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
        if not await _is_authorized_account(account):
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
    accounts = _candidate_accounts(body.get("accounts") or [])
    if not accounts:
        return JSONResponse({"detail": "no_accounts_available"}, status_code=400)
    
    # 验证账号授权状态
    authorized_accounts, _skipped_accounts = await _split_authorized_accounts(accounts)
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

@app.on_event("shutdown")
async def shutdown_event():
    print("[SHUTDOWN] Closing all Telegram connections...")
    await multi_manager.disconnect_all()
    print("[SHUTDOWN] Cleanup complete.")


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
    accounts = _candidate_accounts(body.get("accounts") or [])
    
    results = []
    _authorized_accounts, unauthorized_accounts = await _split_authorized_accounts(accounts)
    unauthorized_set = set(unauthorized_accounts)
    
    for i, acc in enumerate(accounts):
        try:
            if acc in unauthorized_set:
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
