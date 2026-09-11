import asyncio
import time
import hashlib
import random
from sqlalchemy.orm import Session
from app.telegram_client import MultiTelegramManager
from app.models import SendLog
from app.config import CONFIG
from app.services.send_scheduler import SendScheduler
from app.services.group_service import get_banned_group_ids, add_banned_group, should_exclude_group_on_error, should_add_to_global_blist


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
    try:
        banned = set(get_banned_group_ids(db, account))
        if banned:
            ids = [g for g in ids if g not in banned]
    except Exception:
        pass
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
    for gid in ids:
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
                title = await manager.get_group_title(account, gid)
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
                        created_at=CONFIG.now(),
                    )
                )
                db.commit()
                continue
            attempt = 0
            flood_retry = 0
            max_flood_retry = max(int(getattr(CONFIG, "MAX_FLOOD_RETRY", 5)), 0)
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
                
                # 处理 FloodWait 错误
                if err and "FloodWait" in str(err):
                    wait_seconds = 60
                    try:
                        if ":" in str(err):
                            wait_seconds = int(str(err).split(":")[1])
                        else:
                            wait_seconds = int(''.join(filter(str.isdigit, str(err)[:20]))) or 60
                    except:
                        pass
                    # 如果需要等待时间太长，直接跳过重试
                    if wait_seconds > 300:
                        break
                    # 限制连续 FloodWait 等待次数，避免该分支永不递增
                    # attempt 导致 retry_max=0 时无限循环卡住
                    flood_retry += 1
                    if flood_retry > max_flood_retry:
                        break
                    await asyncio.sleep(wait_seconds)
                else:
                    attempt += 1
                    if attempt <= retry_max:
                        await asyncio.sleep(max(retry_delay_ms, 0) / 1000.0)
                        
            status = "success" if ok else "failed"
            if not ok and err:
                if should_exclude_group_on_error(err):
                    try:
                        add_banned_group(db, account, gid, global_scope=should_add_to_global_blist(err))
                    except Exception:
                        pass
            if status == "success":
                success += 1
            elif status == "failed":
                failed += 1
        preview = message[:200]
        title = await manager.get_group_title(account, gid)
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
                created_at=CONFIG.now(),
            )
        )
        db.commit()
        if base > 0:
            jitter = random.uniform(-jitter_pct, jitter_pct) * base
            wait_ms = max(0.0, base + jitter)
            await asyncio.sleep(wait_ms / 1000.0)
    return {"total": total, "success": success, "failed": failed}
