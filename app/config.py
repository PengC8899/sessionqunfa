import os
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


CONFIG = Settings()
