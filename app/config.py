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
        
        # Support for multiple API keys
        self.TG_API_KEYS = []
        api_keys_str = os.getenv("TG_API_KEYS")
        if api_keys_str:
            try:
                import json
                self.TG_API_KEYS = json.loads(api_keys_str)
            except Exception:
                print("Failed to parse TG_API_KEYS, falling back to single key")
        
        if not self.TG_API_KEYS:
            # Fallback to single key if list is empty or invalid
            api_id = os.getenv("TG_API_ID")
            api_hash = os.getenv("TG_API_HASH")
            if api_id and api_hash:
                self.TG_API_KEYS.append({"api_id": api_id, "api_hash": api_hash})
        
        # Keep these for backward compatibility, but they might be None
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
        self.MULTI_ACCOUNT_MAX_CONCURRENT = int(os.getenv("MULTI_ACCOUNT_MAX_CONCURRENT", "3"))
        # 账号之间的发送间隔 (ms)，防止风控
        self.MULTI_ACCOUNT_STAGGER_MS = int(os.getenv("MULTI_ACCOUNT_STAGGER_MS", "5000"))
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

        # Parse Accounts
        accounts = {}
        accounts_list = os.getenv("ACCOUNTS")
        if accounts_list:
            names = [x.strip() for x in accounts_list.split(",") if x.strip()]
            for name in names:
                api_id = os.getenv(f"TG_{name}_API_ID")
                api_hash = os.getenv(f"TG_{name}_API_HASH")
                session_name = os.getenv(f"TG_{name}_SESSION_NAME") or os.getenv("TG_SESSION_NAME") or name
                
                # 如果没有特定账号的配置，尝试使用全局配置，如果也没有，则留空让 AccountClientManager 自动从池中选择
                if not api_id:
                    api_id = os.getenv("TG_API_ID")
                if not api_hash:
                    api_hash = os.getenv("TG_API_HASH")
                
                # 只有在没有 API Pool 的情况下才强制要求 api_id/hash
                if (not api_id or not api_hash) and not self.TG_API_KEYS:
                    raise RuntimeError(f"Missing TG_{name}_API_ID/TG_API_ID or TG_{name}_API_HASH/TG_API_HASH in .env")
                
                accounts[name] = {
                    "api_id": int(api_id) if api_id else None,
                    "api_hash": api_hash,
                    "session_name": session_name,
                }
            self.DEFAULT_ACCOUNT = names[0]
        else:
            api_id = os.getenv("TG_API_ID")
            api_hash = os.getenv("TG_API_HASH")
            session_name = os.getenv("TG_SESSION_NAME")
            
            # 如果有 API Pool，则允许不配置单个 TG_API_ID
            if (not api_id or not api_hash) and not self.TG_API_KEYS:
                 # 只有当既没有单个配置也没有 Pool 时才报错，但为了兼容性，如果连 session_name 都没有，可能不需要配置 ACCOUNTS
                 if session_name:
                     raise RuntimeError("Missing TG_API_ID, TG_API_HASH in .env")
            
            if session_name:
                accounts[session_name] = {
                    "api_id": int(api_id) if api_id else None,
                    "api_hash": api_hash,
                    "session_name": session_name,
                }
                self.DEFAULT_ACCOUNT = session_name
            else:
                self.DEFAULT_ACCOUNT = "default"

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
