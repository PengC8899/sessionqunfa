from starlette.responses import JSONResponse
from starlette.requests import Request
from starlette.exceptions import HTTPException
from app.config import CONFIG
from app.database import SessionLocal
from app.models import Task, SendLog, TaskEvent, GroupCache, AccountHealth
import os

async def reset_system(request: Request):
    token = request.headers.get("X-Admin-Token")
    if token != CONFIG.ADMIN_TOKEN:
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)

    db = SessionLocal()
    try:
        reset_sessions = request.query_params.get("sessions", "false").lower() in ("1", "true", "yes")
        # 1. Stop all running tasks
        db.query(Task).filter(Task.status == "running").update(
            {"stop_requested": 1, "status": "stopped"}
        )
        db.commit()

        # 2. Delete tables
        db.query(TaskEvent).delete()
        db.query(SendLog).delete()
        db.query(Task).delete()
        db.query(GroupCache).delete()
        db.query(AccountHealth).delete()
        
        db.commit()
        deleted_sessions = 0
        if reset_sessions:
            try:
                count = getattr(CONFIG, "ACCOUNT_COUNT", 100)
                prefix = getattr(CONFIG, "ACCOUNT_PREFIX", "account")
                for i in range(1, count + 1):
                    name = f"{prefix}_{i:02d}"
                    base = os.path.join(CONFIG.SESSION_DIR, name)
                    for ext in [".session", ".session-journal"]:
                        p = f"{base}{ext}"
                        if os.path.exists(p):
                            try:
                                os.remove(p)
                                deleted_sessions += 1
                            except Exception:
                                pass
            except Exception:
                pass
        return JSONResponse({"status": "ok", "message": "System reset successfully", "deleted_sessions": deleted_sessions})
    except Exception as e:
        db.rollback()
        return JSONResponse({"detail": str(e)}, status_code=500)
    finally:
        db.close()
