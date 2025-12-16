import asyncio
from typing import List, Optional
from telethon import TelegramClient, events
from telethon.errors import SessionPasswordNeededError, FloodWaitError, PhoneNumberInvalidError
from telethon.tl.types import Channel, Chat
from telethon.tl.functions.channels import GetFullChannelRequest
from telethon.tl.functions.messages import GetFullChatRequest
from app.config import CONFIG
import os
import glob


INLINE_BOT_USERNAME = "PostBot"
INLINE_BOT_QUERY = "694014ffc3b8e"

# Old text fallback (kept for reference, can be removed)
# AUTO_REPLY_TEXT = ... 

class AccountClientManager:
    def __init__(self, session_name: str, api_id: int, api_hash: str):
        self.session_name = session_name
        self.api_id = api_id
        self.api_hash = api_hash
        self.client: Optional[TelegramClient] = None
        self._connected = False
        self._auto_reply_setup = False

    async def ensure_connected(self):
        if not self._connected:
            loop = asyncio.get_running_loop()
            if self.client is None:
                session_base = os.path.join(CONFIG.SESSION_DIR, self.session_name)
                self.client = TelegramClient(session_base, self.api_id, self.api_hash, loop=loop)
            await self.client.connect()
            authorized = await self.client.is_user_authorized()
            if not authorized:
                raise RuntimeError("Telegram session not authorized")
            self._connected = True
            self._setup_auto_reply()

    def _setup_auto_reply(self):
        if self._auto_reply_setup or not self.client:
            return
        
        # Check environment variable to enable/disable auto-reply
        # Default is True ("true", "1", "yes" all count as True)
        enable_auto_reply = os.getenv("ENABLE_AUTO_REPLY", "true").lower() in ("true", "1", "yes")
        
        if not enable_auto_reply:
            print(f"[INFO] Auto-reply DISABLED for account {self.session_name} (ENABLE_AUTO_REPLY={os.getenv('ENABLE_AUTO_REPLY')})")
            self._auto_reply_setup = True
            return

        @self.client.on(events.NewMessage(incoming=True, func=lambda e: e.is_private))
        async def handler(event):
            try:
                # Avoid replying to self
                if event.sender_id == (await self.client.get_me()).id:
                    return
                
                # Use Inline Bot for auto-reply
                results = await self.client.inline_query(INLINE_BOT_USERNAME, INLINE_BOT_QUERY)
                if results:
                    await results[0].click(event.chat_id)
                    print(f"[INFO] Auto-replied via @{INLINE_BOT_USERNAME} to {event.sender_id} on account {self.session_name}")
                else:
                    print(f"[WARNING] No inline results found for @{INLINE_BOT_USERNAME} {INLINE_BOT_QUERY}")
                    
            except Exception as e:
                print(f"[ERROR] Auto-reply failed: {e}")

        self._auto_reply_setup = True
        print(f"[INFO] Auto-reply setup for account {self.session_name}")

    async def _ensure_client(self):
        loop = asyncio.get_running_loop()
        if self.client is None:
            session_base = os.path.join(CONFIG.SESSION_DIR, self.session_name)
            self.client = TelegramClient(session_base, self.api_id, self.api_hash, loop=loop)
        await self.client.connect()
        try:
            authorized = await self.client.is_user_authorized()
        except Exception:
            authorized = False
        if authorized:
            self._connected = True
            self._setup_auto_reply()

    async def send_login_code(self, phone: str, force_sms: bool = False):
        print(f"[DEBUG] send_login_code called for {phone} session {self.session_name}")
        loop = asyncio.get_running_loop()
        
        # Force clean slate strategy:
        # If we are requesting a code, we assume we want to start fresh or the current session is invalid/unauthorized.
        # To avoid issues with old API IDs, DC mismatches, or corrupted sessions, we recreate the client.
        
        session_base = os.path.join(CONFIG.SESSION_DIR, self.session_name)
        
        # 1. Close existing connection if any
        if self.client:
            await self.client.disconnect()
            
        # 2. Check if session exists but is NOT authorized (if authorized, we shouldn't be here ideally, but if user insists...)
        # Actually, let's just assume if they call this, they want to login.
        # We won't delete the session file immediately unless we confirm it's broken, 
        # BUT for the "API ID Changed" scenario, it is safer to DELETE it if it was created with old API ID.
        # How do we know? We don't. So let's delete it to be safe.
        
        # CAUTION: Deleting session file means losing any existing login.
        # But the user says they can't login anyway.
        
        print(f"[DEBUG] Resetting session {self.session_name} to ensure clean state...")
        for p in [f"{session_base}.session", f"{session_base}.session-journal"]:
            try:
                if os.path.exists(p):
                    os.remove(p)
                    print(f"[DEBUG] Deleted {p}")
            except Exception as e:
                print(f"[ERROR] Failed to delete {p}: {e}")

        # 3. Create new client
        self.client = TelegramClient(session_base, self.api_id, self.api_hash, loop=loop)
        await self.client.connect()
        
        try:
            print(f"[DEBUG] Sending code request to {phone}")
            resp = await self.client.send_code_request(phone, force_sms=force_sms)
            print(f"[DEBUG] Send code response: {resp}")
            
            # Explicitly check for SentCode type
            from telethon.tl.types import auth
            code_type = "unknown"
            
            if isinstance(resp, auth.SentCode):
                t_name = type(resp.type).__name__
                print(f"[DEBUG] Code type name: {t_name}")
                if "App" in t_name:
                    code_type = "app"
                elif "Sms" in t_name:
                    code_type = "sms"
                elif "Call" in t_name:
                    code_type = "call"
                elif "FlashCall" in t_name:
                    code_type = "flash_call"
                    
            return {"ok": True, "type": code_type, "debug_info": str(resp)}
        except FloodWaitError as e:
            print(f"[ERROR] FloodWaitError: {e}")
            return {"ok": False, "retry_after": getattr(e, "seconds", 60)}
        except PhoneNumberInvalidError:
            print(f"[ERROR] PhoneNumberInvalidError for {phone}")
            return {"ok": False, "error": "phone_invalid"}
        except Exception as e:
            print(f"[ERROR] Generic Exception during send_code: {type(e).__name__}: {e}")
            return {"ok": False, "error": str(e)}

    async def confirm_login(self, phone: str, code: str, password: str | None = None):
        await self._ensure_client()
        try:
            await self.client.sign_in(phone=phone, code=code)
        except SessionPasswordNeededError:
            if not password:
                raise
            await self.client.sign_in(password=password)
        me = await self.client.get_me()
        self._connected = True
        self._setup_auto_reply()
        return {"id": getattr(me, "id", None)}

    async def is_authorized(self) -> bool:
        await self._ensure_client()
        return await self.client.is_user_authorized()

    async def get_joined_groups(self, only_groups: bool = True) -> List[dict]:
        await self._ensure_client()
        ok = await self.client.is_user_authorized()
        if not ok:
            return []
        await self.ensure_connected()
        dialogs = await self.client.get_dialogs()
        result: List[dict] = []
        for d in dialogs:
            e = d.entity
            if isinstance(e, Chat):
                member_count = None
                if getattr(CONFIG, "GROUP_MEMBER_COUNT_ENABLED", 0):
                    try:
                        full = await self.client(GetFullChatRequest(e.id))
                        member_count = getattr(full.full_chat, "participants_count", None)
                    except Exception:
                        member_count = None
                result.append({
                    "id": e.id,
                    "title": d.name,
                    "username": None,
                    "is_megagroup": False,
                    "is_channel": False,
                    "member_count": member_count,
                })
            elif isinstance(e, Channel):
                is_megagroup = bool(getattr(e, "megagroup", False))
                is_broadcast = bool(getattr(e, "broadcast", False))
                if only_groups and not is_megagroup:
                    continue
                member_count = None
                if getattr(CONFIG, "GROUP_MEMBER_COUNT_ENABLED", 0):
                    try:
                        full = await self.client(GetFullChannelRequest(e))
                        member_count = getattr(full.full_chat, "participants_count", None)
                    except Exception:
                        member_count = None
                result.append({
                    "id": e.id,
                    "title": d.name,
                    "username": getattr(e, "username", None),
                    "is_megagroup": is_megagroup,
                    "is_channel": (not is_megagroup) or is_broadcast,
                    "member_count": member_count,
                })
        return result

    async def send_message_to_group(
        self,
        group_id: int,
        text: str,
        parse_mode: Optional[str],
        disable_web_page_preview: bool,
    ) -> tuple[bool, Optional[str], Optional[int]]:
        await self.ensure_connected()
        pm = None
        if parse_mode == "markdown":
            pm = "markdown"
        elif parse_mode == "html":
            pm = "html"
        try:
            msg = await self.client.send_message(
                entity=group_id,
                message=text,
                parse_mode=pm,
                link_preview=not disable_web_page_preview,
            )
            mid = getattr(msg, 'id', None)
            return True, None, mid
        except Exception as e:
            return False, str(e), None

class MultiTelegramManager:
    def __init__(self, accounts: dict):
        self.managers: dict[str, AccountClientManager] = {}
        for name, cfg in accounts.items():
            self.managers[name] = AccountClientManager(cfg["session_name"], cfg["api_id"], cfg["api_hash"])

    def get(self, account: str) -> AccountClientManager:
        if account not in self.managers:
            # Dynamically create a manager for the account if it doesn't exist.
            # This assumes a standard naming convention for session files.
            session_name = account
            api_id = int(CONFIG.TG_API_ID) if CONFIG.TG_API_ID is not None else None
            api_hash = CONFIG.TG_API_HASH
            if not api_id or not api_hash:
                raise RuntimeError("TG_API_ID and TG_API_HASH must be configured in .env")
            self.managers[account] = AccountClientManager(session_name, api_id, api_hash)
        return self.managers[account]

    async def ensure_connected(self, account: str):
        await self.get(account).ensure_connected()

    async def get_joined_groups(self, account: str, only_groups: bool = True) -> List[dict]:
        return await self.get(account).get_joined_groups(only_groups=only_groups)

    async def send_message_to_group(self, account: str, *args, **kwargs):
        return await self.get(account).send_message_to_group(*args, **kwargs)

    async def send_login_code(self, account: str, phone: str, force_sms: bool = False):
        return await self.get(account).send_login_code(phone, force_sms=force_sms)

    async def confirm_login(self, account: str, phone: str, code: str, password: str | None = None):
        return await self.get(account).confirm_login(phone, code, password)

    async def is_authorized(self, account: str) -> bool:
        return await self.get(account).is_authorized()


multi_manager = MultiTelegramManager(CONFIG.ACCOUNTS)


async def setup_auto_reply_for_all_sessions():
    session_dir = CONFIG.SESSION_DIR
    if not os.path.isdir(session_dir):
        return
    pattern = os.path.join(session_dir, "*.session")
    for path in glob.glob(pattern):
        name = os.path.basename(path).rsplit(".", 1)[0]
        try:
            mgr = multi_manager.get(name)
            await mgr._ensure_client()
            print(f"[INFO] Auto-reply startup check for {name}, setup={mgr._auto_reply_setup}")
        except Exception as e:
            print(f"[ERROR] Auto-reply startup failed for {name}: {e}")
