from starlette.responses import JSONResponse
from starlette.requests import Request
from telethon.errors.rpcerrorlist import AboutTooLongError, FirstNameInvalidError
import asyncio
from app.config import CONFIG
from app.services.account_service import account_service
from app.database import SessionLocal
from app.models import Task, TaskEvent
from app.telegram_client import multi_manager
import json
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


async def bulk_update_profile(request: Request):
    """
    批量更新所有账号的个人资料
    """
    if not _is_authorized_token(request):
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)
    
    try:
        form = await request.form()
        first_name = form.get("first_name")
        last_name = form.get("last_name") or ""
        
        # Handle file upload
        photo_file = form.get("photo")
        photo_path = None
        
        if photo_file and getattr(photo_file, "filename", None):
            upload_dir = "/app/data/profile_uploads"
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
        for account in accounts:
            try:
                res = await multi_manager.update_profile(account, first_name, last_name, photo_path)
                results[account] = {"ok": True, "details": res}
            except Exception as e:
                results[account] = {"ok": False, "error": str(e)}
                
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

    accounts = _discover_authorized_accounts()
    if not accounts:
        return JSONResponse({"ok": True, "accounts_total": 0, "profiles": []})

    sem = asyncio.Semaphore(4)

    async def fetch_one(account: str):
        async with sem:
            try:
                profile = await multi_manager.get_profile(account)
                return {
                    "account": account,
                    "ok": True,
                    "phone": profile.get("phone"),
                    "nickname": profile.get("nickname") or profile.get("first_name") or "",
                    "about": profile.get("about") or "",
                }
            except Exception as e:
                return {
                    "account": account,
                    "ok": False,
                    "phone": None,
                    "nickname": "",
                    "about": "",
                    "error": str(e),
                }

    profiles = await asyncio.gather(*(fetch_one(account) for account in accounts))
    profiles.sort(key=lambda item: item.get("account") or "")
    return JSONResponse(
        {
            "ok": True,
            "accounts_total": len(accounts),
            "profiles": profiles,
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

async def check_single_account(request: Request):
    if not _is_authorized_token(request):
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)
    
    try:
        body = await request.json()
        account = body.get("account")
        if not account:
            return JSONResponse({"detail": "Missing account"}, status_code=400)
    except:
         return JSONResponse({"detail": "Invalid JSON"}, status_code=400)
    
    try:
        result = await account_service.check_account(account)
        return JSONResponse(result)
    except Exception as e:
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

    # 1. 停止并清理该账号的所有运行中任务
    db = SessionLocal()
    tasks_stopped = 0
    try:
        # 查找该账号的所有运行中任务
        running_tasks = db.query(Task).filter(Task.account_name == account, Task.status == "running").all()
        for t in running_tasks:
            t.stop_requested = 1
            t.status = "stopped"  # 标记为停止
            t.finished_at = CONFIG.now()
            # 记录事件
            db.add(TaskEvent(
                task_id=t.id,
                event="account_deleted",
                detail="account_session_deleted_by_admin",
                meta_json=json.dumps({"account": account}, ensure_ascii=False)
            ))
        if running_tasks:
            db.commit()
            tasks_stopped = len(running_tasks)
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
    
    return JSONResponse({
        "account": account, 
        "deleted": deleted,
        "tasks_stopped": tasks_stopped
    })
