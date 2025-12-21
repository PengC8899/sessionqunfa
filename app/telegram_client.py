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
                
                # Send text reply
                reply_text = os.getenv("AUTO_REPLY_TEXT", "hello")
                await event.reply(reply_text, parse_mode=None, link_preview=False)
                print(f"[INFO] Auto-replied '{reply_text}' to {event.sender_id} on account {self.session_name}")
                    
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

    async def send_login_code(self, phone: str, force_sms: bool = False, force_new_session: bool = False):
        """
        发送登录验证码
        
        Args:
            phone: 手机号（带国家码）
            force_sms: 强制使用短信验证
            force_new_session: 强制创建新会话（删除现有session文件）
        """
        print(f"[DEBUG] send_login_code called for {phone} session {self.session_name}")
        loop = asyncio.get_running_loop()
        
        session_base = os.path.join(CONFIG.SESSION_DIR, self.session_name)
        
        # 1. 关闭现有连接
        if self.client:
            try:
                await self.client.disconnect()
            except Exception:
                pass
            self.client = None
            self._connected = False
        
        # 2. 检查是否需要删除现有 session
        # 只有在明确要求或者 session 文件损坏时才删除
        session_path = f"{session_base}.session"
        if force_new_session:
            print(f"[DEBUG] Force new session requested, deleting existing session...")
            for p in [session_path, f"{session_base}.session-journal"]:
                try:
                    if os.path.exists(p):
                        os.remove(p)
                        print(f"[DEBUG] Deleted {p}")
                except Exception as e:
                    print(f"[ERROR] Failed to delete {p}: {e}")
        elif os.path.exists(session_path):
            # 尝试使用现有 session
            print(f"[DEBUG] Found existing session file, checking if authorized...")
            try:
                temp_client = TelegramClient(session_base, self.api_id, self.api_hash, loop=loop)
                await temp_client.connect()
                if await temp_client.is_user_authorized():
                    await temp_client.disconnect()
                    return {"ok": True, "already_authorized": True, "message": "Session already authorized"}
                await temp_client.disconnect()
            except Exception as e:
                print(f"[DEBUG] Existing session check failed: {e}, will try fresh login")
                # Session 文件可能损坏或 API ID 不匹配，删除后重试
                for p in [session_path, f"{session_base}.session-journal"]:
                    try:
                        if os.path.exists(p):
                            os.remove(p)
                            print(f"[DEBUG] Deleted corrupted session: {p}")
                    except Exception as del_err:
                        print(f"[ERROR] Failed to delete {p}: {del_err}")

        # 3. 创建新客户端
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

    async def validate_session(self) -> dict:
        """
        验证 session 文件是否有效，不会删除 session
        返回: {"valid": bool, "authorized": bool, "user_id": int|None, "phone": str|None, "error": str|None}
        """
        session_base = os.path.join(CONFIG.SESSION_DIR, self.session_name)
        session_path = f"{session_base}.session"
        
        if not os.path.exists(session_path):
            return {"valid": False, "authorized": False, "error": "session_file_not_found"}
        
        loop = asyncio.get_running_loop()
        temp_client = None
        try:
            temp_client = TelegramClient(session_base, self.api_id, self.api_hash, loop=loop)
            await temp_client.connect()
            
            if not await temp_client.is_user_authorized():
                return {"valid": True, "authorized": False, "error": "not_authorized"}
            
            me = await temp_client.get_me()
            return {
                "valid": True,
                "authorized": True,
                "user_id": me.id if me else None,
                "phone": me.phone if me else None,
                "first_name": getattr(me, 'first_name', None),
                "username": getattr(me, 'username', None),
            }
        except Exception as e:
            error_type = type(e).__name__
            return {"valid": False, "authorized": False, "error": f"{error_type}: {str(e)}"}
        finally:
            if temp_client:
                try:
                    await temp_client.disconnect()
                except Exception:
                    pass

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

    async def join_group(self, invite_link: str) -> dict:
        """
        通过邀请链接加入群组
        
        Args:
            invite_link: 群组邀请链接 (如 https://t.me/+xxx 或 https://t.me/joinchat/xxx 或 @username)
        
        Returns:
            {"ok": bool, "group_id": int|None, "title": str|None, "error": str|None}
        """
        await self.ensure_connected()
        
        try:
            from telethon.tl.functions.messages import ImportChatInviteRequest, CheckChatInviteRequest
            from telethon.tl.types import ChatInviteAlready, ChatInvite
            import re
            
            # 解析邀请链接
            invite_hash = None
            username = None
            
            # 处理不同格式的链接
            if invite_link.startswith("@"):
                username = invite_link[1:]
            elif "t.me/+" in invite_link:
                # https://t.me/+xxxxx
                invite_hash = invite_link.split("+")[-1].split("?")[0]
            elif "t.me/joinchat/" in invite_link:
                # https://t.me/joinchat/xxxxx
                invite_hash = invite_link.split("joinchat/")[-1].split("?")[0]
            elif "t.me/" in invite_link:
                # https://t.me/groupname (public group)
                username = invite_link.split("t.me/")[-1].split("?")[0].split("/")[0]
            else:
                # 可能直接是 hash 或 username
                if re.match(r'^[a-zA-Z][a-zA-Z0-9_]{4,}$', invite_link):
                    username = invite_link
                else:
                    invite_hash = invite_link
            
            if username:
                # 公开群组，直接通过 username 加入
                try:
                    entity = await self.client.get_entity(username)
                    # 检查是否已经在群组中
                    dialogs = await self.client.get_dialogs()
                    for d in dialogs:
                        if hasattr(d.entity, 'id') and d.entity.id == entity.id:
                            return {
                                "ok": True,
                                "already_joined": True,
                                "group_id": entity.id,
                                "title": getattr(entity, 'title', None),
                            }
                    
                    # 加入群组
                    from telethon.tl.functions.channels import JoinChannelRequest
                    await self.client(JoinChannelRequest(entity))
                    return {
                        "ok": True,
                        "group_id": entity.id,
                        "title": getattr(entity, 'title', None),
                    }
                except Exception as e:
                    return {"ok": False, "error": f"join_public_failed: {e}"}
            
            elif invite_hash:
                # 私有群组，通过邀请链接加入
                try:
                    # 先检查邀请链接
                    result = await self.client(CheckChatInviteRequest(invite_hash))
                    
                    if isinstance(result, ChatInviteAlready):
                        # 已经在群组中
                        chat = result.chat
                        return {
                            "ok": True,
                            "already_joined": True,
                            "group_id": chat.id,
                            "title": getattr(chat, 'title', None),
                        }
                    elif isinstance(result, ChatInvite):
                        # 需要加入
                        updates = await self.client(ImportChatInviteRequest(invite_hash))
                        # 从 updates 中获取群组信息
                        chat = None
                        if hasattr(updates, 'chats') and updates.chats:
                            chat = updates.chats[0]
                        return {
                            "ok": True,
                            "group_id": chat.id if chat else None,
                            "title": getattr(chat, 'title', None) if chat else None,
                        }
                    else:
                        return {"ok": False, "error": "unknown_invite_type"}
                        
                except Exception as e:
                    error_str = str(e).lower()
                    if "userAlreadyParticipant" in str(e):
                        return {"ok": True, "already_joined": True, "error": None}
                    elif "invite" in error_str and "expire" in error_str:
                        return {"ok": False, "error": "invite_expired"}
                    elif "flood" in error_str:
                        return {"ok": False, "error": f"flood_wait: {e}"}
                    else:
                        return {"ok": False, "error": str(e)}
            else:
                return {"ok": False, "error": "invalid_invite_link"}
                
        except Exception as e:
            return {"ok": False, "error": str(e)}


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

    async def send_login_code(self, account: str, phone: str, force_sms: bool = False, force_new_session: bool = False):
        return await self.get(account).send_login_code(phone, force_sms=force_sms, force_new_session=force_new_session)

    async def validate_session(self, account: str) -> dict:
        """验证账号的 session 文件是否有效"""
        return await self.get(account).validate_session()

    async def confirm_login(self, account: str, phone: str, code: str, password: str | None = None):
        return await self.get(account).confirm_login(phone, code, password)

    async def is_authorized(self, account: str) -> bool:
        return await self.get(account).is_authorized()

    async def join_group(self, account: str, invite_link: str) -> dict:
        """通过邀请链接加入群组"""
        return await self.get(account).join_group(invite_link)


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
