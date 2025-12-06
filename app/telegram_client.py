import asyncio
from typing import List, Optional
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError, FloodWaitError, PhoneNumberInvalidError
from telethon.tl.types import Channel, Chat
from telethon.tl.functions.channels import GetFullChannelRequest
from telethon.tl.functions.messages import GetFullChatRequest
from app.config import CONFIG
import os


class AccountClientManager:
    def __init__(self, session_name: str, api_id: int, api_hash: str):
        self.session_name = session_name
        self.api_id = api_id
        self.api_hash = api_hash
        self.client: Optional[TelegramClient] = None
        self._connected = False

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

    async def _ensure_client(self):
        loop = asyncio.get_running_loop()
        if self.client is None:
            session_base = os.path.join(CONFIG.SESSION_DIR, self.session_name)
            self.client = TelegramClient(session_base, self.api_id, self.api_hash, loop=loop)
        await self.client.connect()

    async def send_login_code(self, phone: str, force_sms: bool = False):
        loop = asyncio.get_running_loop()
        if self.client is None:
            session_base = os.path.join(CONFIG.SESSION_DIR, self.session_name)
            self.client = TelegramClient(session_base, self.api_id, self.api_hash, loop=loop)
        # ensure a fresh connection
        await self.client.connect()
        # if connection dropped, reconnect
        if not self.client.is_connected():
            await self.client.connect()
        try:
            resp = await self.client.send_code_request(phone, force_sms=force_sms)
            print(resp)
            return {"ok": True}
        except FloodWaitError as e:
            return {"ok": False, "retry_after": getattr(e, "seconds", 60)}
        except PhoneNumberInvalidError:
            return {"ok": False, "error": "phone_invalid"}

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
