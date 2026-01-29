import random
import re
from typing import List, Dict, Tuple
from sqlalchemy.orm import Session
from app.models import SendLog
from app.config import CONFIG

def _recent_group_stats(db: Session, account: str, group_id: int, window: int) -> Tuple[int, int, int]:
    rows = (
        db.query(SendLog)
        .filter(SendLog.account_name == account, SendLog.group_id == group_id)
        .order_by(SendLog.created_at.desc())
        .limit(window)
        .all()
    )
    s = 0
    f = 0
    k = 0
    for r in rows:
        st = (r.status or "").lower()
        if st == "success":
            s += 1
            k = 0
        elif st == "failed":
            f += 1
            k += 1
        else:
            k = 0
    return len(rows), s, f

def classify_groups(db: Session, account: str, group_ids: List[int]) -> Dict[int, str]:
    window = int(getattr(CONFIG, "GROUP_RECENT_WINDOW_N", 20))
    fail_thr = float(getattr(CONFIG, "GROUP_BLACK_FAIL_RATE", 0.75))
    grey_low = float(getattr(CONFIG, "GROUP_GREY_LOW_FAIL_RATE", 0.3))
    black_min = int(getattr(CONFIG, "GROUP_BLACK_MIN_FAILS", 3))
    res: Dict[int, str] = {}
    for gid in group_ids:
        n, s, f = _recent_group_stats(db, account, gid, window)
        rate = (f / n) if n > 0 else 0.0
        if f >= black_min and rate >= fail_thr:
            res[gid] = "BLACK"
        elif n == 0:
            res[gid] = "GREY"
        elif rate >= grey_low:
            res[gid] = "GREY"
        else:
            res[gid] = "WHITE"
    return res

def _recent_account_stats(db: Session, account: str, window: int) -> Tuple[int, int]:
    rows = (
        db.query(SendLog)
        .filter(SendLog.account_name == account)
        .order_by(SendLog.created_at.desc())
        .limit(window)
        .all()
    )
    s = 0
    f = 0
    for r in rows:
        st = (r.status or "").lower()
        if st == "success":
            s += 1
        elif st == "failed":
            f += 1
    return s, f

def classify_account(db: Session, account: str) -> str:
    window = int(getattr(CONFIG, "ACCOUNT_RECENT_WINDOW_N", 50))
    safe_max_fail_rate = float(getattr(CONFIG, "ACCOUNT_SAFE_MAX_FAIL_RATE", 0.2))
    risk_min_fail_rate = float(getattr(CONFIG, "ACCOUNT_RISK_MIN_FAIL_RATE", 0.5))
    s, f = _recent_account_stats(db, account, window)
    n = max(1, s + f)
    rate = f / n
    if rate >= risk_min_fail_rate:
        return "RISK"
    if rate <= safe_max_fail_rate:
        return "SAFE"
    return "CORE"

def select_groups_for_account(role: str, group_ids: List[int], grades: Dict[int, str]) -> List[int]:
    whites = [g for g in group_ids if grades.get(g) == "WHITE"]
    greys = [g for g in group_ids if grades.get(g) == "GREY"]
    blacks = [g for g in group_ids if grades.get(g) == "BLACK"]
    if role == "SAFE":
        res = whites
        if not res:
            res = whites + greys
    elif role == "CORE":
        res = whites + greys
    else:
        max_probe = int(getattr(CONFIG, "RISK_MAX_GREY_PROBES", 2))
        random.shuffle(greys)
        res = whites + greys[:max_probe]
    if not res:
        res = [g for g in group_ids if g not in blacks]
    random.shuffle(res)
    return res

def dynamic_delay_ms(base_ms: int, recent_fail_rate: float) -> int:
    min_delay = int(getattr(CONFIG, "SEND_MIN_DELAY_MS", 1500))
    max_multiplier = float(getattr(CONFIG, "DELAY_MAX_MULTIPLIER", 3.0))
    factor = 1.0 + min(max(recent_fail_rate, 0.0), 1.0) * (max_multiplier - 1.0)
    return int(max(min_delay, base_ms) * factor)

def randomize_message(text: str) -> str:
    if int(getattr(CONFIG, "CONTENT_FINGERPRINT_ENABLED", 1)) == 0:
        return text
    t = text
    
    # 1. 基础替换
    if random.random() < 0.6:
        t = re.sub(r"[ \t]{2,}", " ", t)
    if random.random() < 0.5:
        t = re.sub(r"\n{2,}", "\n", t)
        
    # 2. 零宽字符注入 (Zero-width injection) - 最有效的反 Hash
    # \u200b (Zero Width Space), \u200c (Zero Width Non-Joiner), \u200d (Zero Width Joiner), \u2060 (Word Joiner)
    zw_chars = ["\u200b", "\u200c", "\u200d", "\u2060"]
    if random.random() < 0.8:  # 80% 概率启用
        chars = list(t)
        # 随机插入 1-3 个零宽字符
        num_inserts = random.randint(1, 3)
        for _ in range(num_inserts):
            if len(chars) > 0:
                pos = random.randint(0, len(chars))
                chars.insert(pos, random.choice(zw_chars))
        t = "".join(chars)

    # 3. 尾部 Emoji
    if random.random() < 0.4:
        ems = ["🔥", "✅", "✨", "📌", "💡", "🚀", "🎯", "⭐", "⚡"]
        if random.random() < 0.5:
            t = t + (" " if not t.endswith("\n") else "") + random.choice(ems)
        else:
            t = t.replace(random.choice(ems), "")
            
    return t

def recent_fail_rate(db: Session, account: str, window: int) -> float:
    s, f = _recent_account_stats(db, account, window)
    n = max(1, s + f)
    return f / n
