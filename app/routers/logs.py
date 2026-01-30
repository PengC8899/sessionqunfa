from fastapi import APIRouter, HTTPException, Depends
from starlette.requests import Request
from sqlalchemy.orm import Session
from app.config import CONFIG
from app.database import get_db
from app.models import SendLog


router = APIRouter()


@router.get("/logs")
def recent_logs(request: Request, limit: int = 50, db: Session = Depends(get_db)):
    token = request.headers.get("X-Admin-Token")
    if token != CONFIG.ADMIN_TOKEN:
        raise HTTPException(status_code=401, detail="Unauthorized")
    rows = (
        db.query(SendLog)
        .order_by(SendLog.created_at.desc())
        .limit(limit)
        .all()
    )
    return [
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
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in rows
    ]


@router.post("/logs/clear")
def clear_logs(request: Request, db: Session = Depends(get_db)):
    token = request.headers.get("X-Admin-Token")
    if token != CONFIG.ADMIN_TOKEN:
        raise HTTPException(status_code=401, detail="Unauthorized")
    
    try:
        db.query(SendLog).delete()
        db.commit()
        return {"ok": True, "message": "All logs cleared"}
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))
