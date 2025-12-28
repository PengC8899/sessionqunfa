import os
import asyncio
from telethon import TelegramClient
from telethon.errors import (
    UserDeactivatedError,
    UserDeactivatedBanError,
    AuthKeyError,
    AuthKeyDuplicatedError,
    AuthKeyUnregisteredError,
    AuthKeyInvalidError,
    SessionRevokedError
)
from app.config import CONFIG

class AccountService:
    def __init__(self):
        self.semaphore = asyncio.Semaphore(5)  # Limit concurrent checks

    async def check_account(self, session_name: str):
        async with self.semaphore:
            session_base = os.path.join(CONFIG.SESSION_DIR, session_name)
            # Check if session file exists
            if not os.path.exists(f"{session_base}.session"):
                return {"account": session_name, "status": "missing_file", "valid": False}

            client = TelegramClient(session_base, CONFIG.TG_API_ID, CONFIG.TG_API_HASH)
            try:
                await client.connect()
                if not await client.is_user_authorized():
                    await client.disconnect()
                    return {"account": session_name, "status": "unauthorized", "valid": False}
                
                try:
                    me = await client.get_me()
                    if not me:
                         await client.disconnect()
                         return {"account": session_name, "status": "unknown_error", "valid": False}
                    
                    # Optionally check if restricted
                    status = "ok"
                    # You can add more checks here if needed
                    
                    await client.disconnect()
                    return {"account": session_name, "status": status, "valid": True, "phone": me.phone, "id": me.id}
                
                except (UserDeactivatedError, UserDeactivatedBanError):
                    await client.disconnect()
                    return {"account": session_name, "status": "banned", "valid": False}
                
            except (AuthKeyError, AuthKeyDuplicatedError, AuthKeyUnregisteredError, AuthKeyInvalidError, SessionRevokedError):
                 # Session is invalid
                 return {"account": session_name, "status": "invalid_session", "valid": False}
            except Exception as e:
                try:
                    await client.disconnect()
                except:
                    pass
                return {"account": session_name, "status": "error", "valid": False, "detail": str(e)}

    async def check_all_accounts(self):
        count = getattr(CONFIG, "ACCOUNT_COUNT", 100)
        prefix = getattr(CONFIG, "ACCOUNT_PREFIX", "account")
        names = [f"{prefix}_{i:02d}" for i in range(1, count + 1)]
        
        tasks = [self.check_account(name) for name in names]
        results = await asyncio.gather(*tasks)
        return results

    async def delete_session(self, session_name: str):
        session_base = os.path.join(CONFIG.SESSION_DIR, session_name)
        deleted = False
        for ext in [".session", ".session-journal"]:
            path = f"{session_base}{ext}"
            if os.path.exists(path):
                try:
                    os.remove(path)
                    deleted = True
                except Exception as e:
                    print(f"Error deleting {path}: {e}")
        return deleted

account_service = AccountService()
