import os
from datetime import datetime, timezone as tz
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

load_dotenv()


class Settings:
    ADMIN_TOKEN: str
    DB_URL: str
    HOST: str
    PORT: int
    ACCOUNTS: dict
    DEFAULT_ACCOUNT: str
    SESSION_DIR: str
    SEND_RETRY_MAX: int
    SEND_RETRY_DELAY_MS: int
    SEND_MIN_DELAY_MS: int
    SEND_JITTER_PCT: float
    GROUP_CACHE_TTL_SECONDS: int
    GROUP_CACHE_ENABLED: int
    TIMEZONE: str
    # 多账号并发配置
    MULTI_ACCOUNT_ENABLED: int
    MULTI_ACCOUNT_MAX_CONCURRENT: int
    MULTI_ACCOUNT_STAGGER_MS: int
    # 调度与风控参数
    SCHEDULER_ENABLED: int
    GROUP_RECENT_WINDOW: int
    GROUP_BLACK_FAIL_RATE: float
    GROUP_GREY_FAIL_RATE: float
    GROUP_BLACK_CONSEC_FAILS: int
    ACCOUNT_RECENT_WINDOW: int
    ACCOUNT_SAFE_FAIL_RATE: float
    ACCOUNT_RISK_FAIL_RATE: float
    ACCOUNT_CONSEC_FAILS_PAUSE: int
    MESSAGE_FINGERPRINT_ENABLED: int
    MESSAGE_NUMBER_JITTER_PCT: float
    MESSAGE_EMOJI_TOGGLE_PROB: float
    MESSAGE_WHITESPACE_JITTER_PROB: float

    def __init__(self):
        admin_token = os.getenv("ADMIN_TOKEN") or os.getenv("ADMIN_PASSWORD")
        if not admin_token:
            raise RuntimeError("Missing ADMIN_TOKEN in .env")
        self.ADMIN_TOKEN = admin_token
        self.TG_API_ID = os.getenv("TG_API_ID")
        self.TG_API_HASH = os.getenv("TG_API_HASH")
        self.DB_URL = os.getenv("DB_URL", "sqlite:///./data.db")
        self.HOST = os.getenv("HOST", "0.0.0.0")
        self.PORT = int(os.getenv("PORT", "8000"))
        self.SESSION_DIR = os.getenv("SESSION_DIR", ".")
        self.SEND_RETRY_MAX = int(os.getenv("SEND_RETRY_MAX", "2"))
        self.SEND_RETRY_DELAY_MS = int(os.getenv("SEND_RETRY_DELAY_MS", "1500"))
        self.SEND_MIN_DELAY_MS = int(os.getenv("SEND_MIN_DELAY_MS", "1500"))
        try:
            self.SEND_JITTER_PCT = float(os.getenv("SEND_JITTER_PCT", "0.15"))
        except Exception:
            self.SEND_JITTER_PCT = 0.15
        self.GROUP_CACHE_TTL_SECONDS = int(os.getenv("GROUP_CACHE_TTL_SECONDS", "600"))
        self.GROUP_CACHE_ENABLED = int(os.getenv("GROUP_CACHE_ENABLED", "1"))
        try:
            self.ACCOUNT_COUNT = int(os.getenv("ACCOUNT_COUNT", "20"))
        except Exception:
            self.ACCOUNT_COUNT = 20
        self.ACCOUNT_PREFIX = os.getenv("ACCOUNT_PREFIX", "account")
        
        # 时区配置 - 默认使用 Asia/Singapore (新加坡) 或 Asia/Kolkata (孟买)
        self.TIMEZONE = os.getenv("TIMEZONE", "Asia/Singapore")
        
        # 多账号并发配置
        self.MULTI_ACCOUNT_ENABLED = int(os.getenv("MULTI_ACCOUNT_ENABLED", "1"))
        self.MULTI_ACCOUNT_MAX_CONCURRENT = int(os.getenv("MULTI_ACCOUNT_MAX_CONCURRENT", "5"))
        # 账号之间的发送间隔 (ms)，防止风控
        self.MULTI_ACCOUNT_STAGGER_MS = int(os.getenv("MULTI_ACCOUNT_STAGGER_MS", "3000"))
        self.SCHEDULER_ENABLED = int(os.getenv("SCHEDULER_ENABLED", "1"))
        self.GROUP_RECENT_WINDOW = int(os.getenv("GROUP_RECENT_WINDOW", "50"))
        try:
            self.GROUP_BLACK_FAIL_RATE = float(os.getenv("GROUP_BLACK_FAIL_RATE", "0.6"))
        except Exception:
            self.GROUP_BLACK_FAIL_RATE = 0.6
        try:
            self.GROUP_GREY_FAIL_RATE = float(os.getenv("GROUP_GREY_FAIL_RATE", "0.3"))
        except Exception:
            self.GROUP_GREY_FAIL_RATE = 0.3
        self.GROUP_BLACK_CONSEC_FAILS = int(os.getenv("GROUP_BLACK_CONSEC_FAILS", "3"))
        self.ACCOUNT_RECENT_WINDOW = int(os.getenv("ACCOUNT_RECENT_WINDOW", "40"))
        try:
            self.ACCOUNT_SAFE_FAIL_RATE = float(os.getenv("ACCOUNT_SAFE_FAIL_RATE", "0.15"))
        except Exception:
            self.ACCOUNT_SAFE_FAIL_RATE = 0.15
        try:
            self.ACCOUNT_RISK_FAIL_RATE = float(os.getenv("ACCOUNT_RISK_FAIL_RATE", "0.35"))
        except Exception:
            self.ACCOUNT_RISK_FAIL_RATE = 0.35
        self.ACCOUNT_CONSEC_FAILS_PAUSE = int(os.getenv("ACCOUNT_CONSEC_FAILS_PAUSE", "3"))
        self.MESSAGE_FINGERPRINT_ENABLED = int(os.getenv("MESSAGE_FINGERPRINT_ENABLED", "1"))
        try:
            self.MESSAGE_NUMBER_JITTER_PCT = float(os.getenv("MESSAGE_NUMBER_JITTER_PCT", "0.03"))
        except Exception:
            self.MESSAGE_NUMBER_JITTER_PCT = 0.03
        try:
            self.MESSAGE_EMOJI_TOGGLE_PROB = float(os.getenv("MESSAGE_EMOJI_TOGGLE_PROB", "0.25"))
        except Exception:
            self.MESSAGE_EMOJI_TOGGLE_PROB = 0.25
        try:
            self.MESSAGE_WHITESPACE_JITTER_PROB = float(os.getenv("MESSAGE_WHITESPACE_JITTER_PROB", "0.4"))
        except Exception:
            self.MESSAGE_WHITESPACE_JITTER_PROB = 0.4
        self.RISK_TRY_GROUPS = int(os.getenv("RISK_TRY_GROUPS", "2"))

        accounts_list = (os.getenv("TG_ACCOUNTS") or "").strip()
        accounts: dict = {}
        if accounts_list:
            names = [x.strip() for x in accounts_list.split(",") if x.strip()]
            for name in names:
                api_id = os.getenv(f"TG_{name}_API_ID") or os.getenv("TG_API_ID")
                api_hash = os.getenv(f"TG_{name}_API_HASH") or os.getenv("TG_API_HASH")
                session_name = os.getenv(f"TG_{name}_SESSION_NAME") or os.getenv("TG_SESSION_NAME") or name
                if not api_id or not api_hash:
                    raise RuntimeError(f"Missing TG_{name}_API_ID/TG_API_ID or TG_{name}_API_HASH/TG_API_HASH in .env")
                accounts[name] = {
                    "api_id": int(api_id),
                    "api_hash": api_hash,
                    "session_name": session_name,
                }
            self.DEFAULT_ACCOUNT = names[0]
        else:
            api_id = os.getenv("TG_API_ID")
            api_hash = os.getenv("TG_API_HASH")
            session_name = os.getenv("TG_SESSION_NAME")
            if not api_id or not api_hash or not session_name:
                raise RuntimeError("Missing TG_API_ID, TG_API_HASH, TG_SESSION_NAME in .env")
            accounts[session_name] = {
                "api_id": int(api_id),
                "api_hash": api_hash,
                "session_name": session_name,
            }
            self.DEFAULT_ACCOUNT = session_name

        self.ACCOUNTS = accounts

    def now(self) -> datetime:
        """获取当前时间 (服务器配置的时区)"""
        try:
            tz_info = ZoneInfo(self.TIMEZONE)
            return datetime.now(tz_info)
        except Exception:
            # 如果时区配置无效，回退到 UTC
            return datetime.now(tz.utc)

    def get_timezone(self):
        """获取配置的时区对象"""
        try:
            return ZoneInfo(self.TIMEZONE)
        except Exception:
            return tz.utc


CONFIG = Settings()
