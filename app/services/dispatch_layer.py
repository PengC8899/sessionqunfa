import random
import re
from datetime import datetime, timezone
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


def unique_group_ids(group_ids: List[int]) -> List[int]:
    seen = set()
    res: List[int] = []
    for gid in group_ids:
        try:
            value = int(gid)
        except Exception:
            continue
        if value in seen:
            continue
        seen.add(value)
        res.append(value)
    return res


def _latest_group_activity_map(db: Session, group_ids: List[int]) -> Dict[int, datetime]:
    rows = (
        db.query(SendLog)
        .filter(SendLog.group_id.in_(group_ids))
        .order_by(SendLog.created_at.desc())
        .all()
    )
    latest: Dict[int, datetime] = {}
    for row in rows:
        gid = int(row.group_id)
        if gid not in latest and row.created_at is not None:
            latest[gid] = row.created_at
    return latest


def filter_groups_by_global_cooldown(db: Session, group_ids: List[int], cooldown_s: int) -> List[int]:
    ids = unique_group_ids(group_ids)
    if cooldown_s <= 0 or not ids:
        return ids
    now = CONFIG.now()
    latest = _latest_group_activity_map(db, ids)
    res: List[int] = []
    for gid in ids:
        ts = latest.get(gid)
        if ts is None:
            res.append(gid)
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=now.tzinfo or timezone.utc)
        age = (now - ts).total_seconds()
        if age >= cooldown_s:
            res.append(gid)
    return res


def sort_groups_for_account(db: Session, account: str, group_ids: List[int]) -> List[int]:
    ids = unique_group_ids(group_ids)
    if not ids:
        return []
    account_rows = (
        db.query(SendLog)
        .filter(SendLog.account_name == account, SendLog.group_id.in_(ids))
        .order_by(SendLog.created_at.desc())
        .all()
    )
    account_latest: Dict[int, datetime] = {}
    for row in account_rows:
        gid = int(row.group_id)
        if gid not in account_latest and row.created_at is not None:
            account_latest[gid] = row.created_at
    global_latest = _latest_group_activity_map(db, ids)
    rng = random.Random(f"{account}:{len(ids)}")
    jitter = {gid: rng.random() for gid in ids}
    return sorted(
        ids,
        key=lambda gid: (
            account_latest.get(gid) is not None,
            account_latest.get(gid) or datetime.min,
            global_latest.get(gid) is not None,
            global_latest.get(gid) or datetime.min,
            jitter[gid],
        ),
    )


def distribute_groups_unique(db: Session, accounts: List[str], group_ids: List[int]) -> Dict[str, List[int]]:
    accounts = [a for a in accounts if a]
    distribution: Dict[str, List[int]] = {acc: [] for acc in accounts}
    ids = unique_group_ids(group_ids)
    if not accounts or not ids:
        return distribution

    sorted_groups = ids
    global_latest = _latest_group_activity_map(db, sorted_groups)
    roles = {acc: classify_account(db, acc) for acc in accounts}
    weight_map = {
        "SAFE": float(getattr(CONFIG, "SAFE_ACCOUNT_GROUP_WEIGHT", 1.25)),
        "CORE": float(getattr(CONFIG, "CORE_ACCOUNT_GROUP_WEIGHT", 1.0)),
        "RISK": float(getattr(CONFIG, "RISK_ACCOUNT_GROUP_WEIGHT", 0.6)),
    }
    weights = {acc: max(0.1, weight_map.get(roles.get(acc, "CORE"), 1.0)) for acc in accounts}
    total_weight = sum(weights.values()) or float(len(accounts))
    raw_targets = {acc: (len(sorted_groups) * weights[acc] / total_weight) for acc in accounts}
    targets = {acc: int(raw_targets[acc]) for acc in accounts}
    assigned = sum(targets.values())
    if assigned < len(sorted_groups):
        remainders = sorted(
            accounts,
            key=lambda acc: (raw_targets[acc] - targets[acc], weights[acc]),
            reverse=True,
        )
        for acc in remainders[: len(sorted_groups) - assigned]:
            targets[acc] += 1
    elif assigned > len(sorted_groups):
        remainders = sorted(accounts, key=lambda acc: (raw_targets[acc] - targets[acc], weights[acc]))
        overflow = assigned - len(sorted_groups)
        for acc in remainders:
            if overflow <= 0:
                break
            if targets[acc] > 0:
                targets[acc] -= 1
                overflow -= 1

    account_latest: Dict[str, Dict[int, datetime]] = {}
    account_rows = (
        db.query(SendLog)
        .filter(SendLog.account_name.in_(accounts), SendLog.group_id.in_(sorted_groups))
        .order_by(SendLog.created_at.desc())
        .all()
    )
    for row in account_rows:
        acc = row.account_name
        gid = int(row.group_id)
        if acc not in account_latest:
            account_latest[acc] = {}
        if gid not in account_latest[acc] and row.created_at is not None:
            account_latest[acc][gid] = row.created_at

    sorted_groups = sorted(
        sorted_groups,
        key=lambda gid: (
            global_latest.get(gid) is not None,
            global_latest.get(gid) or datetime.min,
        ),
    )

    for gid in sorted_groups:
        best_acc = min(
            accounts,
            key=lambda acc: (
                gid in account_latest.get(acc, {}),
                len(distribution[acc]) / max(targets.get(acc, 1), 1),
                len(distribution[acc]) >= targets.get(acc, 0),
                account_latest.get(acc, {}).get(gid) or datetime.min,
                weights[acc] <= 1.0,
            ),
        )
        distribution[best_acc].append(gid)
    return distribution
