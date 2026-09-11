from starlette.responses import JSONResponse
from starlette.requests import Request
from telethon.errors.rpcerrorlist import AboutTooLongError, FirstNameInvalidError
import asyncio
from app.config import CONFIG
from app.services.account_service import account_service
from app.services.proxy_service import (
    assign_proxy_pool_to_accounts,
    DEFAULT_PROXY_SOURCE,
    delete_proxy_binding,
    get_default_proxy_source,
    list_proxy_bindings,
    resolve_proxy_source_text,
    set_default_proxy_source,
    upsert_proxy_binding,
)
from app.database import SessionLocal
from app.models import Task, TaskEvent
from app.telegram_client import multi_manager
import os
import glob
import uuid


def _is_authorized_token(request: Request) -> bool:
    return request.headers.get("X-Admin-Token") == CONFIG.ADMIN_TOKEN


def _translate_profile_update_error(exc: Exception) -> str:
    err_text = str(exc)
    if "FROZEN_METHOD_INVALID" in err_text:
        return "该账号当前被 Telegram 限制，暂时不能修改昵称或简介"
    if isinstance(exc, AboutTooLongError):
        return "账号简介过长，请控制在 70 个字符以内"
    if isinstance(exc, FirstNameInvalidError):
        return "账号昵称不合法，请换一个昵称再试"
    return str(exc)


def _discover_authorized_accounts() -> list[str]:
    session_dir = CONFIG.SESSION_DIR
    files = glob.glob(os.path.join(session_dir, "*.session"))
    names = []
    for path in files:
        name = os.path.basename(path)[:-8]
        if name and name not in names:
            names.append(name)
    return names


def _purge_account_tasks(db, account: str) -> dict:
    task_ids = [row[0] for row in db.query(Task.id).filter(Task.account_name == account).all()]
    active_tasks = db.query(Task).filter(Task.account_name == account, Task.status == "running").count()
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


async def bulk_update_profile(request: Request):
    """
    批量更新所有账号的个人资料
    """
    if not _is_authorized_token(request):
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)
    
    try:
        form = await request.form()
        first_name = (form.get("first_name") or "").strip()
        last_name = form.get("last_name") or ""
        
        # Handle file upload
        photo_file = form.get("photo")
        photo_path = None
        
        if photo_file and getattr(photo_file, "filename", None):
            upload_dir = os.path.join(CONFIG.SESSION_DIR, "_profile_uploads")
            if not os.path.exists(upload_dir):
                os.makedirs(upload_dir, exist_ok=True)

            filename = photo_file.filename
            ext = os.path.splitext(filename)[1]
            if not ext:
                ext = ".jpg"

            filename = f"avatar_update_{uuid.uuid4()}{ext}"
            photo_path = os.path.join(upload_dir, filename)

            content = await photo_file.read()
            with open(photo_path, 'wb') as f:
                f.write(content)
        
        # Get all accounts
        session_dir = CONFIG.SESSION_DIR
        files = glob.glob(os.path.join(session_dir, "*.session"))
        accounts = [os.path.basename(f)[:-8] for f in files]
        
        results = {}
        try:
            for account in accounts:
                try:
                    res = await multi_manager.update_profile(account, first_name, last_name, photo_path)
                    results[account] = {"ok": True, "details": res}
                except Exception as e:
                    results[account] = {"ok": False, "error": str(e)}
        finally:
            # 清理上传的临时头像文件，避免磁盘泄漏
            if photo_path and os.path.exists(photo_path):
                try:
                    os.remove(photo_path)
                except Exception:
                    pass
                
        return JSONResponse(results)
        
    except Exception as e:
        return JSONResponse({"detail": str(e)}, status_code=500)


async def get_account_profile(request: Request):
    if not _is_authorized_token(request):
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)

    account = (request.query_params.get("account") or "").strip()
    if not account:
        return JSONResponse({"detail": "Missing account"}, status_code=400)

    session_path = os.path.join(CONFIG.SESSION_DIR, f"{account}.session")
    if not os.path.exists(session_path):
        return JSONResponse({"detail": "session_not_found"}, status_code=404)

    try:
        profile = await multi_manager.get_profile(account)
        return JSONResponse({"ok": True, **profile})
    except Exception as e:
        return JSONResponse({"detail": str(e)}, status_code=400)


async def get_authorized_profiles(request: Request):
    if not _is_authorized_token(request):
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)

    # #region debug-point A:profiles-route-start
    route_started_at = asyncio.get_running_loop().time()
    account_service_module = __import__("app.services.account_service", fromlist=["_report_health_check_debug"])
    account_service_module._report_health_check_debug("A", "app/routers/accounts.py:get_authorized_profiles:start", "authorized_profiles.start", {})
    # #endregion
    accounts = _discover_authorized_accounts()
    if not accounts:
        # #region debug-point A:profiles-route-empty
        account_service_module._report_health_check_debug("A", "app/routers/accounts.py:get_authorized_profiles:empty", "authorized_profiles.empty", {})
        # #endregion
        return JSONResponse({"ok": True, "accounts_total": 0, "profiles": []})

    db = SessionLocal()
    try:
        proxy_map = list_proxy_bindings(db, accounts)
    finally:
        db.close()

    sem = asyncio.Semaphore(4)

    async def fetch_one(account: str):
        async with sem:
            try:
                profile = await account_service.check_account(account, use_cache=False, include_group_send_check=False)
                if not profile.get("valid"):
                    raise RuntimeError(profile.get("detail") or profile.get("status") or "账号不可用")
                return {
                    "account": account,
                    "ok": True,
                    "phone": profile.get("phone"),
                    "nickname": profile.get("nickname") or profile.get("first_name") or profile.get("username") or "",
                    "about": profile.get("about") or "",
                    "proxy": proxy_map.get(account),
                }
            except Exception as e:
                return {
                    "account": account,
                    "ok": False,
                    "phone": None,
                    "nickname": "",
                    "about": "",
                    "proxy": proxy_map.get(account),
                    "error": str(e),
                }

    fetched_profiles = await asyncio.gather(*(fetch_one(account) for account in accounts))
    profiles = [item for item in fetched_profiles if item.get("ok")]
    failed_profiles = [item for item in fetched_profiles if not item.get("ok")]
    profiles.sort(key=lambda item: item.get("account") or "")
    failed_profiles.sort(key=lambda item: item.get("account") or "")
    # #region debug-point A:profiles-route-finish
    account_service_module._report_health_check_debug("A", "app/routers/accounts.py:get_authorized_profiles:finish", "authorized_profiles.finish", {
        "accounts": len(accounts),
        "profiles": len(profiles),
        "failed_profiles": len(failed_profiles),
        "elapsedMs": int((asyncio.get_running_loop().time() - route_started_at) * 1000),
        "failed": len(failed_profiles),
    })
    # #endregion
    return JSONResponse(
        {
            "ok": True,
            "accounts_total": len(accounts),
            "profiles": profiles,
            "failed_profiles": failed_profiles,
        }
    )


async def update_account_profile(request: Request):
    if not _is_authorized_token(request):
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"detail": "Invalid JSON"}, status_code=400)

    nickname = (body.get("nickname") or "").strip()
    about = body.get("about")

    if not nickname:
        return JSONResponse({"detail": "Missing nickname"}, status_code=400)
    if len(nickname) > 64:
        return JSONResponse({"detail": "nickname_too_long"}, status_code=400)
    if about is not None:
        about = str(about).strip()
        if len(about) > 70:
            return JSONResponse({"detail": "about_too_long"}, status_code=400)

    accounts = _discover_authorized_accounts()
    if not accounts:
        return JSONResponse({"detail": "no_authorized_accounts"}, status_code=400)

    results = {}
    success_count = 0
    failed_count = 0
    sample_profile = None

    for account in accounts:
        try:
            profile = await multi_manager.update_text_profile(account, nickname, about)
            results[account] = {"ok": True, "profile": profile}
            success_count += 1
            if sample_profile is None:
                sample_profile = profile
        except Exception as e:
            results[account] = {"ok": False, "error": _translate_profile_update_error(e)}
            failed_count += 1

    return JSONResponse(
        {
            "ok": success_count > 0,
            "nickname": nickname,
            "about": about or "",
            "accounts_total": len(accounts),
            "success_count": success_count,
            "failed_count": failed_count,
            "results": results,
            "sample_profile": sample_profile,
        }
    )


async def get_account_proxy(request: Request):
    if not _is_authorized_token(request):
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)

    account = (request.query_params.get("account") or "").strip()
    if not account:
        return JSONResponse({"detail": "Missing account"}, status_code=400)

    db = SessionLocal()
    try:
        proxy_map = list_proxy_bindings(db, [account])
        return JSONResponse({"ok": True, "account": account, "proxy": proxy_map.get(account)})
    finally:
        db.close()


async def update_account_proxy(request: Request):
    if not _is_authorized_token(request):
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"detail": "Invalid JSON"}, status_code=400)

    account = (body.get("account") or "").strip()
    proxy_url = (body.get("proxy_url") or "").strip()
    enabled = bool(body.get("enabled", True))
    if not account:
        return JSONResponse({"detail": "Missing account"}, status_code=400)

    db = SessionLocal()
    try:
        try:
            proxy_data = upsert_proxy_binding(db, account, proxy_url, enabled=enabled)
        except ValueError as e:
            return JSONResponse({"detail": str(e)}, status_code=400)
    finally:
        db.close()

    try:
        await multi_manager.refresh_account_client(account)
    except Exception:
        pass

    return JSONResponse({"ok": True, "account": account, "proxy": proxy_data})


async def get_default_account_proxy_source(request: Request):
    if not _is_authorized_token(request):
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)

    db = SessionLocal()
    try:
        source = get_default_proxy_source(db)
        return JSONResponse({"ok": True, "proxy_source_text": source, "default_proxy_source": DEFAULT_PROXY_SOURCE})
    finally:
        db.close()


async def update_default_account_proxy_source(request: Request):
    if not _is_authorized_token(request):
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"detail": "Invalid JSON"}, status_code=400)

    source_value = body.get("proxy_source_text")
    db = SessionLocal()
    try:
        saved = set_default_proxy_source(db, source_value)
        return JSONResponse({"ok": True, "proxy_source_text": saved, "default_proxy_source": DEFAULT_PROXY_SOURCE})
    finally:
        db.close()


async def auto_assign_account_proxies(request: Request):
    if not _is_authorized_token(request):
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"detail": "Invalid JSON"}, status_code=400)

    raw_accounts = body.get("accounts") or []
    proxy_source_text = body.get("proxy_source_text") or body.get("proxy_source") or ""
    if not str(proxy_source_text).strip():
        return JSONResponse({"detail": "proxy_source_text required"}, status_code=400)

    accounts: list[str] = []
    if isinstance(raw_accounts, list):
        for item in raw_accounts:
            name = str(item or "").strip()
            if name and name not in accounts:
                accounts.append(name)
    if not accounts:
        accounts = _discover_authorized_accounts()
    if not accounts:
        return JSONResponse({"detail": "no_authorized_accounts"}, status_code=400)

    errors: list[str] = []
    explicit_map, proxy_pool, source_meta = resolve_proxy_source_text(str(proxy_source_text), errors)
    assigned_bindings, assignment_meta = assign_proxy_pool_to_accounts(accounts, explicit_map, proxy_pool)

    saved_accounts: list[str] = []
    db = SessionLocal()
    try:
        for account in accounts:
            proxy_url = assigned_bindings.get(account)
            if not proxy_url:
                continue
            try:
                upsert_proxy_binding(db, account, proxy_url, enabled=True)
                saved_accounts.append(account)
            except ValueError as exc:
                errors.append(f"{account}: 代理格式错误 - {str(exc)}")
    finally:
        db.close()

    for account in saved_accounts:
        try:
            await multi_manager.refresh_account_client(account)
        except Exception:
            pass

    return JSONResponse(
        {
            "ok": len(saved_accounts) > 0,
            "accounts": accounts,
            "assigned_accounts": saved_accounts,
            "assigned_total": len(saved_accounts),
            "source_meta": source_meta,
            "assignment_meta": assignment_meta,
            "errors": errors if errors else None,
        }
    )

async def check_single_account(request: Request):
    if not _is_authorized_token(request):
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)
    
    try:
        body = await request.json()
        account = body.get("account")
        if not account:
            return JSONResponse({"detail": "Missing account"}, status_code=400)
        include_group_send_check = bool(body.get("include_group_send_check", True))
    except:
         return JSONResponse({"detail": "Invalid JSON"}, status_code=400)
    
    try:
        # #region debug-point C:check-single-route-start
        route_started_at = asyncio.get_running_loop().time()
        account_service_module = __import__("app.services.account_service", fromlist=["_report_health_check_debug"])
        account_service_module._report_health_check_debug("C", "app/routers/accounts.py:check_single_account:start", "check_single_account.start", {
            "account": account,
            "include_group_send_check": include_group_send_check,
        })
        # #endregion
        result = await account_service.check_account(account, include_group_send_check=include_group_send_check)
        # #region debug-point C:check-single-route-finish
        account_service_module._report_health_check_debug("C", "app/routers/accounts.py:check_single_account:finish", "check_single_account.finish", {
            "account": account,
            "elapsedMs": int((asyncio.get_running_loop().time() - route_started_at) * 1000),
            "status": result.get("status"),
            "valid": result.get("valid"),
        })
        # #endregion
        return JSONResponse(result)
    except Exception as e:
        # #region debug-point C:check-single-route-error
        account_service_module._report_health_check_debug("C", "app/routers/accounts.py:check_single_account:error", "check_single_account.error", {
            "account": account,
            "error": str(e)[:120],
        })
        # #endregion
        return JSONResponse(
            {"account": account, "status": "error", "valid": False, "detail": str(e)[:120]},
            status_code=200,
        )

async def delete_account(request: Request):
    if not _is_authorized_token(request):
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)
        
    try:
        body = await request.json()
        account = body.get("account")
        if not account:
            return JSONResponse({"detail": "Missing account"}, status_code=400)
    except:
         return JSONResponse({"detail": "Invalid JSON"}, status_code=400)

    # 1. 清理该账号关联的任务记录，运行中的 worker 会在下次检查时自动退出
    db = SessionLocal()
    task_cleanup = {
        "active_tasks_deleted": 0,
        "task_records_deleted": 0,
        "task_events_deleted": 0,
    }
    try:
        task_cleanup = _purge_account_tasks(db, account)
    except Exception as e:
        db.rollback()
        print(f"[DELETE_ACCOUNT] Error stopping tasks for {account}: {e}")
    finally:
        db.close()

    # 2. 如果账号已连接，断开连接
    if account in multi_manager.managers:
        try:
            await multi_manager.managers[account].disconnect()
        except Exception:
            pass

    # 3. 删除 session 文件
    deleted = await account_service.delete_session(account)
    db = SessionLocal()
    try:
        delete_proxy_binding(db, account)
    finally:
        db.close()
    
    return JSONResponse({
        "account": account, 
        "deleted": deleted,
        "tasks_stopped": task_cleanup["active_tasks_deleted"],
        "task_records_deleted": task_cleanup["task_records_deleted"],
        "task_events_deleted": task_cleanup["task_events_deleted"],
    })
