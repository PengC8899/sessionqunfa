import random
import re
import time
from typing import List, Dict, Tuple, Optional
from sqlalchemy.orm import Session
from app.models import SendLog
from app.config import CONFIG

class SendScheduler:
    def __init__(self, db: Session):
        self.db = db

    def grade_groups(self, group_ids: List[int]) -> Dict[str, List[int]]:
        window = int(getattr(CONFIG, "GROUP_RECENT_WINDOW", 50))
        black_rate = float(getattr(CONFIG, "GROUP_BLACK_FAIL_RATE", 0.6))
        grey_rate = float(getattr(CONFIG, "GROUP_GREY_FAIL_RATE", 0.3))
        black_consec = int(getattr(CONFIG, "GROUP_BLACK_CONSEC_FAILS", 3))
        stats: Dict[int, Tuple[int, int, int]] = {}
        rows = (
            self.db.query(SendLog)
            .filter(SendLog.group_id.in_(group_ids))
            .order_by(SendLog.created_at.desc())
            .limit(window * max(1, len(group_ids)))
            .all()
        )
        for r in rows:
            gid = int(r.group_id)
            if gid not in group_ids:
                continue
            s, f, c = stats.get(gid, (0, 0, 0))
            if r.status == "success":
                s += 1
                c = 0
            elif r.status == "failed":
                f += 1
                c = c + 1
            stats[gid] = (s, f, c)
        white: List[int] = []
        grey: List[int] = []
        black: List[int] = []
        for gid in group_ids:
            s, f, c = stats.get(gid, (0, 0, 0))
            total = s + f
            rate = (f / total) if total > 0 else 0.0
            if c >= black_consec or rate >= black_rate:
                black.append(gid)
            elif rate >= grey_rate:
                grey.append(gid)
            else:
                white.append(gid)
        return {"WHITE": white, "GREY": grey, "BLACK": black}

    def account_role(self, account: str) -> str:
        window = int(getattr(CONFIG, "ACCOUNT_RECENT_WINDOW", 40))
        safe_rate = float(getattr(CONFIG, "ACCOUNT_SAFE_FAIL_RATE", 0.15))
        risk_rate = float(getattr(CONFIG, "ACCOUNT_RISK_FAIL_RATE", 0.35))
        rows = (
            self.db.query(SendLog)
            .filter(SendLog.account_name == account)
            .order_by(SendLog.created_at.desc())
            .limit(window)
            .all()
        )
        s = 0
        f = 0
        for r in rows:
            if r.status == "success":
                s += 1
            elif r.status == "failed":
                f += 1
        total = s + f
        rate = (f / total) if total > 0 else 0.0
        if rate <= safe_rate:
            return "SAFE"
        if rate >= risk_rate:
            return "RISK"
        return "CORE"

    def select_groups_for_account(self, role: str, white: List[int], grey: List[int]) -> List[int]:
        risk_try = int(getattr(CONFIG, "RISK_TRY_GROUPS", 2))
        groups: List[int] = []
        if role == "SAFE":
            groups = list(white)
        elif role == "CORE":
            groups = list(white) + list(grey)
        else:
            w = list(white)
            g = list(grey)
            random.shuffle(w)
            random.shuffle(g)
            groups = w + g[:max(0, risk_try)]
        random.shuffle(groups)
        return groups

    def fingerprint_message(self, message: str, parse_mode: Optional[str]) -> str:
        if not bool(getattr(CONFIG, "MESSAGE_FINGERPRINT_ENABLED", 1)):
            return message
        if parse_mode and parse_mode in ("markdown", "html"):
            return self._whitespace_jitter(message)
        m = self._number_jitter(message)
        m = self._emoji_toggle(m)
        m = self._whitespace_jitter(m)
        return m

    def _number_jitter(self, text: str) -> str:
        pct = float(getattr(CONFIG, "MESSAGE_NUMBER_JITTER_PCT", 0.03))
        def repl(m: re.Match):
            i, j = m.start(), m.end()
            start = i
            while start > 0:
                ch = text[start - 1]
                if ch.isalnum() or ch == "_":
                    start -= 1
                else:
                    break
            token_left = text[start:i]
            if "@" in token_left:
                return m.group(0)
            s = m.group(0)
            try:
                v = float(s)
            except:
                return s
            delta = v * random.uniform(-pct, pct)
            nv = v + delta
            if "." in s:
                prec = max(0, len(s.split(".")[1]))
                fmt = f"{{:.{prec}f}}"
                return fmt.format(nv)
            return str(int(round(nv)))
        return re.sub(r"\b\d+(\.\d+)?\b", repl, text)

    def _emoji_toggle(self, text: str) -> str:
        p = float(getattr(CONFIG, "MESSAGE_EMOJI_TOGGLE_PROB", 0.25))
        if random.random() > p:
            return text
        choices = ["🔥", "✅", "⭐", "📌", "⚡", "🎯"]
        e = random.choice(choices)
        if random.random() < 0.5 and e in text:
            return text.replace(e, "")
        return text + (" " if not text.endswith(" ") else "") + e

    def _whitespace_jitter(self, text: str) -> str:
        p = float(getattr(CONFIG, "MESSAGE_WHITESPACE_JITTER_PROB", 0.4))
        if random.random() > p:
            return text
        parts = re.split(r"(\s)", text)
        for i in range(len(parts)):
            if parts[i] == " " and random.random() < 0.5:
                parts[i] = "  "
            elif parts[i] == "\n" and random.random() < 0.5:
                parts[i] = "\n\n"
        return "".join(parts)

    def should_pause_account(self, account: str) -> bool:
        consec_th = int(getattr(CONFIG, "ACCOUNT_CONSEC_FAILS_PAUSE", 3))
        rows = (
            self.db.query(SendLog)
            .filter(SendLog.account_name == account)
            .order_by(SendLog.created_at.desc())
            .limit(consec_th)
            .all()
        )
        c = 0
        for r in rows:
            if r.status == "failed":
                c += 1
            else:
                break
        return c >= consec_th
