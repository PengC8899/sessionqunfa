import asyncio
import time
import hashlib
import random
import json
from sqlalchemy.orm import Session
from app.telegram_client import MultiTelegramManager
from app.models import SendLog, Task, TaskEvent
from app.config import CONFIG
from app.services.send_scheduler import SendScheduler


_SEND_CACHE: dict[str, float] = {}


def _msg_key(account: str, gid: int, message: str, parse_mode: str, disable_web_page_preview: bool) -> str:
    h = hashlib.sha256((parse_mode or "")
                       .encode("utf-8") + b"|" +
                       (b"1" if disable_web_page_preview else b"0") + b"|" +
                       message.encode("utf-8")).hexdigest()[:16]
    return f"{account}:{gid}:{h}"


def _should_skip(account: str, gid: int, message: str, parse_mode: str, disable_web_page_preview: bool, window_s: int = 120) -> bool:
    now = time.monotonic()
    key = _msg_key(account, gid, message, parse_mode, disable_web_page_preview)
    ts = _SEND_CACHE.get(key)
    for k, t in list(_SEND_CACHE.items()):
        if now - t > window_s * 2:
            del _SEND_CACHE[k]
    if ts and (now - ts) < window_s:
        return True
    _SEND_CACHE[key] = now
    return False


async def send_to_groups(
    manager: MultiTelegramManager,
    db: Session,
    account: str,
    group_ids: list[int],
    message: str,
    parse_mode: str,
    disable_web_page_preview: bool,
    delay_ms: int,
    retry_max: int = 0,
    retry_delay_ms: int = 1500,
):
    use_sched = bool(getattr(CONFIG, "SCHEDULER_ENABLED", 1))
    ids = list(group_ids)
    if use_sched:
        sched = SendScheduler(db)
        grading = sched.grade_groups(ids)
        role = sched.account_role(account)
        ids = sched.select_groups_for_account(role, grading.get("WHITE", []), grading.get("GREY", []))
        black = set(grading.get("BLACK", []))
        ids = [g for g in ids if g not in black]
    random.shuffle(ids)
    total = len(ids)
    success = 0
    failed = 0
    base_delay_ms = max(delay_ms, 0)
    min_delay_ms = max(getattr(CONFIG, "SEND_MIN_DELAY_MS", 1500), 0)
    jitter_pct = max(0.0, min(getattr(CONFIG, "SEND_JITTER_PCT", 0.15), 0.5))
    base = max(base_delay_ms, min_delay_ms)
    for idx, gid in enumerate(ids):
        skipped = _should_skip(account, gid, message, parse_mode, disable_web_page_preview)
        msg_id = None
        err = None
        if skipped:
            status = "skipped"
        else:
            if use_sched and sched.should_pause_account(account):
                status = "skipped"
                err = "account_paused"
                msg_id = None
                preview = message[:200]
                title = str(gid)
                try:
                    ent = await manager.get(account).client.get_entity(gid)
                    title = getattr(ent, 'title', None) or getattr(ent, 'username', None) or getattr(ent, 'first_name', None) or str(gid)
                except Exception:
                    title = str(gid)
                db.add(
                    SendLog(
                        account_name=account,
                        group_id=gid,
                        group_title=title,
                        message_preview=preview,
                        status=status,
                        error=None if status == "success" else (err or ("" if status == "skipped" else "send_failed")),
                        message_id=msg_id,
                        parse_mode=parse_mode,
                    )
                )
                db.commit()
                continue
            attempt = 0
            ok = False
            while attempt <= max(0, retry_max):
                send_text = message
                if use_sched:
                    send_text = sched.fingerprint_message(message, parse_mode if parse_mode != "plain" else None)
                ok, err, msg_id = await manager.send_message_to_group(
                    account,
                    group_id=gid,
                    text=send_text,
                    parse_mode=parse_mode,
                    disable_web_page_preview=disable_web_page_preview,
                )
                if ok:
                    break
                attempt += 1
                if attempt <= retry_max:
                    await asyncio.sleep(max(retry_delay_ms, 0) / 1000.0)
            status = "success" if ok else "failed"
            if status == "success":
                success += 1
            elif status == "failed":
                failed += 1
        preview = message[:200]
        title = str(gid)
        try:
            ent = await manager.get(account).client.get_entity(gid)
            title = getattr(ent, 'title', None) or getattr(ent, 'username', None) or getattr(ent, 'first_name', None) or str(gid)
        except Exception:
            title = str(gid)
        db.add(
            SendLog(
                account_name=account,
                group_id=gid,
                group_title=title,
                message_preview=preview,
                status=status,
                error=None if status == "success" else (err or ("" if status == "skipped" else "send_failed")),
                message_id=msg_id,
                parse_mode=parse_mode,
            )
        )
        db.commit()
        if base > 0:
            jitter = random.uniform(-jitter_pct, jitter_pct) * base
            wait_ms = max(0.0, base + jitter)
            await asyncio.sleep(wait_ms / 1000.0)
    return {"total": total, "success": success, "failed": failed}
