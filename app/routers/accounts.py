from starlette.responses import JSONResponse
from starlette.requests import Request
from starlette.exceptions import HTTPException
from app.config import CONFIG
from app.services.account_service import account_service
from app.database import SessionLocal
from app.models import Task, TaskEvent
from app.telegram_client import multi_manager
import json
import os
import glob
import uuid

async def bulk_update_profile(request: Request):
    """
    批量更新所有账号的个人资料
    """
    token = request.headers.get("X-Admin-Token")
    if token != CONFIG.ADMIN_TOKEN:
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)
    
    try:
        form = await request.form()
        first_name = form.get("first_name")
        last_name = form.get("last_name") or ""
        
        # Handle file upload
        photo_file = form.get("photo")
        photo_path = None
        
        if photo_file and getattr(photo_file, "filename", None):
            # Save to static folder
            static_dir = os.path.join(os.getcwd(), "static")
            if not os.path.exists(static_dir):
                os.makedirs(static_dir)
                
            filename = photo_file.filename
            ext = os.path.splitext(filename)[1]
            if not ext:
                ext = ".jpg" # Default extension
            
            # Use a unique name
            filename = f"avatar_update_{uuid.uuid4()}{ext}"
            photo_path = os.path.join(static_dir, filename)
            
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

async def check_single_account(request: Request):
    token = request.headers.get("X-Admin-Token")
    if token != CONFIG.ADMIN_TOKEN:
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)
    
    try:
        body = await request.json()
        account = body.get("account")
        if not account:
            return JSONResponse({"detail": "Missing account"}, status_code=400)
    except:
         return JSONResponse({"detail": "Invalid JSON"}, status_code=400)
    
    result = await account_service.check_account(account)
    return JSONResponse(result)

async def delete_account(request: Request):
    token = request.headers.get("X-Admin-Token")
    if token != CONFIG.ADMIN_TOKEN:
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
