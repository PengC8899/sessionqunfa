import asyncio
import re
from typing import List, Optional
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError, FloodWaitError, PhoneNumberInvalidError
from telethon.tl.types import Channel, Chat, User
from telethon.tl.functions.channels import GetFullChannelRequest
from telethon.tl.functions.messages import GetFullChatRequest
from app.config import CONFIG
import os


INLINE_BOT_USERNAME = "PostBot"


def _looks_like_postbot_code(value: str) -> bool:
    raw = (value or "").strip()
    if not raw or " " in raw or len(raw) < 10 or len(raw) > 128:
        return False
    if not re.fullmatch(r"[A-Za-z0-9_-]+", raw):
        return False
    return bool(re.search(r"\d", raw))


def _extract_inline_bot_query(text: str) -> tuple[Optional[str], Optional[str]]:
    raw = (text or "").strip()
    if not raw:
        return None, None

    inline_match = re.match(r"^@([A-Za-z0-9_]{5,32})\s+(.+)$", raw)
    if inline_match:
        bot_username = inline_match.group(1)
        query = inline_match.group(2).strip()
        return (bot_username, query) if query else (None, None)

    prefix_match = re.match(r"^(postbot)\s*[:\s]\s*(.+)$", raw, flags=re.IGNORECASE)
    if prefix_match:
        query = prefix_match.group(2).strip()
        return (INLINE_BOT_USERNAME, query) if query else (None, None)

    if _looks_like_postbot_code(raw):
        return INLINE_BOT_USERNAME, raw

    return None, None


def _extract_forward_source(text: str) -> tuple[Optional[object], Optional[int]]:
    raw = (text or "").strip()
    if not raw:
        return None, None

    private_match = re.match(
        r"^(?:https?://)?t\.me/c/(\d+)/(\d+)(?:\?.*)?$",
        raw,
        flags=re.IGNORECASE,
    )
    if private_match:
        return int(f"-100{private_match.group(1)}"), int(private_match.group(2))

    public_match = re.match(
        r"^(?:https?://)?t\.me/(?:s/)?([A-Za-z0-9_]{5,32})/(\d+)(?:\?.*)?$",
        raw,
        flags=re.IGNORECASE,
    )
    if public_match:
        return public_match.group(1), int(public_match.group(2))

    return None, None


class AccountClientManager:
    def __init__(self, session_name: str, api_id: int, api_hash: str):
        self.session_name = session_name
        self.api_id = api_id
        self.api_hash = api_hash
        self.client: Optional[TelegramClient] = None
        self._connected = False
        self._started_inline_bots: set[str] = set()

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
        await self._ensure_client()
        try:
            await self.client.send_code_request(phone, force_sms=force_sms)
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
        source_peer, source_message_id = _extract_forward_source(text)
        if source_peer is not None and source_message_id is not None:
            try:
                target_ent = await self.client.get_entity(group_id)
                if isinstance(target_ent, User):
                    return False, "peer_is_user", None
                if isinstance(source_peer, str):
                    source_ent = await self.client.get_entity(source_peer)
                else:
                    source_ent = await self.client.get_entity(source_peer)
                if isinstance(source_ent, Channel):
                    try:
                        from telethon.tl.functions.channels import JoinChannelRequest
                        await self.client(JoinChannelRequest(source_ent))
                    except Exception:
                        pass
                src_msg = await self.client.get_messages(source_ent, ids=source_message_id)
                if not src_msg:
                    return False, "forward_source_message_not_found", None
                msg = await self.client.forward_messages(
                    entity=target_ent,
                    messages=[source_message_id],
                    from_peer=source_ent,
                )
                if isinstance(msg, list):
                    msg = msg[0] if msg else None
                mid = getattr(msg, "id", None) if msg else None
                return True, None, mid
            except Exception as e:
                return False, f"forward_send_failed: {e}", None
        bot_username, inline_query = _extract_inline_bot_query(text)
        if bot_username and inline_query:
            try:
                normalized = (bot_username or "").lstrip("@").strip()
                cache_key = normalized.lower()
                if cache_key not in self._started_inline_bots:
                    try:
                        dialogs = await self.client.get_dialogs(limit=200)
                        has_dialog = any(
                            getattr(d.entity, "username", None)
                            and getattr(d.entity, "username", "").lower() == cache_key
                            for d in dialogs
                        )
                    except Exception:
                        has_dialog = False
                    if not has_dialog:
                        try:
                            bot_entity = await self.client.get_entity(normalized)
                            await self.client.send_message(bot_entity, "/start")
                        except Exception:
                            pass
                    self._started_inline_bots.add(cache_key)
                ent = await self.client.get_entity(group_id)
                if isinstance(ent, User):
                    return False, "peer_is_user", None
                results = await self.client.inline_query(bot_username, inline_query, entity=ent)
                if not results:
                    return False, "inline_no_results", None
                msg = await results[0].click(entity=ent, hide_via=True)
                mid = getattr(msg, "id", None)
                return True, None, mid
            except Exception as e:
                return False, f"inline_send_failed: {e}", None
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
            raise RuntimeError("Unknown account")
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
