from app.telegram_client import MultiTelegramManager
from sqlalchemy.orm import Session
from app.models import GroupCache, SystemKV
from app.config import CONFIG
from typing import Optional
import json
import time

_GROUP_CACHE: dict[tuple[str, bool], dict] = {}
_CACHE_TTL_SECONDS = getattr(CONFIG, "GROUP_CACHE_TTL_SECONDS", 600)

def _normalize_group_items(data: list[dict]) -> list[dict]:
    normalized: list[dict] = []
    for item in data or []:
        if not isinstance(item, dict):
            continue
        g = dict(item)
        try:
            gid = int(g.get("id"))
        except Exception:
            normalized.append(g)
            continue
        if "raw_id" not in g and gid > 0:
            g["raw_id"] = gid
            if bool(g.get("is_megagroup")) or bool(g.get("is_channel")):
                g["id"] = -(1000000000000 + gid)
            else:
                g["id"] = -gid
        normalized.append(g)
    return normalized

def should_exclude_group_on_error(error_text: str | None) -> bool:
    if not error_text:
        return False
    err = str(error_text).lower()
    markers = (
        "banned from sending messages",
        "user_banned_in_channel",
        "chat_write_forbidden",
        "chat_send_plain_forbidden",
        "chat_send_media_forbidden",
        "peer error",
        "invalid peer",
        "could not find the input entity",
        "peer_is_user",
        "the chat is restricted and cannot be used in that request",
        "chat restricted",
    )
    if any(marker in err for marker in markers):
        return True
    return False

def get_banned_group_ids(db: Session, account: Optional[str] = None) -> list[int]:
    if not account:
        return []
    row = db.query(SystemKV).filter(SystemKV.k == f"banned_groups:{account}").first()
    if not row or not (row.v or "").strip():
        return []
    try:
        data = json.loads(row.v)
        if isinstance(data, list):
            return [int(x) for x in data if isinstance(x, (int, str)) and str(x).lstrip("-").isdigit()]
    except Exception:
        return []
    return []

def add_banned_group(db: Session, account: str, gid: int):
    gids = set(get_banned_group_ids(db, account))
    gids.add(int(gid))
    payload = json.dumps(sorted(gids), ensure_ascii=False)
    row = db.query(SystemKV).filter(SystemKV.k == f"banned_groups:{account}").first()
    if row:
        row.v = payload
    else:
        row = SystemKV(k=f"banned_groups:{account}", v=payload)
        db.add(row)
    db.commit()
    for k in list(_GROUP_CACHE.keys()):
        acc, og = k
        if acc == account:
            _GROUP_CACHE[k]["ts"] = 0.0

async def get_groups(manager: MultiTelegramManager, account: str, only_groups: bool = True, refresh: bool = False, db: Session | None = None):
    key = (account, bool(only_groups))
    if not refresh:
        c = _GROUP_CACHE.get(key)
        if c and (time.monotonic() - c.get("ts", 0)) < _CACHE_TTL_SECONDS:
            data = _normalize_group_items(c.get("data", []))
            if db is not None:
                banned = set(get_banned_group_ids(db, account))
                if banned:
                    data = [g for g in data if int(g.get("id")) not in banned]
            return data
        if db is not None and getattr(CONFIG, "GROUP_CACHE_ENABLED", 1):
            row = (
                db.query(GroupCache)
                .filter(GroupCache.account_name == account, GroupCache.only_groups == (1 if only_groups else 0))
                .order_by(GroupCache.updated_at.desc())
                .first()
            )
            if row:
                try:
                    data = _normalize_group_items(json.loads(row.data_json or "[]"))
                    if db is not None:
                        banned = set(get_banned_group_ids(db, account))
                        if banned:
                            data = [g for g in data if int(g.get("id")) not in banned]
                    _GROUP_CACHE[key] = {"data": data, "ts": time.monotonic()}
                    return data
                except Exception:
                    pass
    data = _normalize_group_items(await manager.get_joined_groups(account, only_groups=only_groups))
    if db is not None:
        banned = set(get_banned_group_ids(db, account))
        if banned:
            data = [g for g in data if int(g.get("id")) not in banned]
    _GROUP_CACHE[key] = {"data": data, "ts": time.monotonic()}
    if db is not None and getattr(CONFIG, "GROUP_CACHE_ENABLED", 1):
        try:
            payload = json.dumps(data, ensure_ascii=False)
            row = (
                db.query(GroupCache)
                .filter(GroupCache.account_name == account, GroupCache.only_groups == (1 if only_groups else 0))
                .first()
            )
            if row:
                row.data_json = payload
            else:
                row = GroupCache(account_name=account, only_groups=(1 if only_groups else 0), data_json=payload)
                db.add(row)
            db.commit()
        except Exception:
            pass
    return data

def clear_group_cache(account: Optional[str] = None, only_groups: Optional[bool] = None, db: Session | None = None):
    keys = list(_GROUP_CACHE.keys())
    removed = 0
    for k in keys:
        acc, og = k
        if (account is None or acc == account) and (only_groups is None or og == bool(only_groups)):
            if k in _GROUP_CACHE:
                del _GROUP_CACHE[k]
                removed += 1
    if db is not None and getattr(CONFIG, "GROUP_CACHE_ENABLED", 1):
        q = db.query(GroupCache)
        if account is not None:
            q = q.filter(GroupCache.account_name == account)
        if only_groups is not None:
            q = q.filter(GroupCache.only_groups == (1 if only_groups else 0))
        q.delete(synchronize_session=False)
        db.commit()
    return {"removed_memory": removed}
