import os
import asyncio
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
        self._manager = None  # Will be set by main.py
        self.check_timeout = 15  # 15 秒超时
    
    def set_manager(self, manager):
        """Set the multi_manager reference"""
        self._manager = manager

    async def check_account(self, session_name: str):
        """使用已有的 multi_manager 检查账号状态（避免 database is locked 错误）"""
        async with self.semaphore:
            try:
                # 添加整体超时保护
                return await asyncio.wait_for(
                    self._do_check(session_name),
                    timeout=self.check_timeout
                )
            except asyncio.TimeoutError:
                return {"account": session_name, "status": "timeout", "valid": False, "detail": f"Check timed out after {self.check_timeout}s"}

    async def _do_check(self, session_name: str):
        """实际的检查逻辑"""
        session_base = os.path.join(CONFIG.SESSION_DIR, session_name)
        
        # 检查 session 文件是否存在
        if not os.path.exists(f"{session_base}.session"):
            return {"account": session_name, "status": "missing_file", "valid": False}
        
        # 优先使用 multi_manager（如果已设置）
        if self._manager:
            try:
                # 确保账号已连接（get 会自动创建 manager）
                try:
                    await self._manager.ensure_connected(session_name)
                except Exception as e:
                    return {"account": session_name, "status": "connect_failed", "valid": False, "detail": str(e)}
                
                # 检查授权状态
                try:
                    authorized = await self._manager.is_authorized(session_name)
                    if not authorized:
                        return {"account": session_name, "status": "unauthorized", "valid": False}
                except Exception as e:
                    return {"account": session_name, "status": "auth_check_failed", "valid": False, "detail": str(e)}
                
                # 获取用户信息
                try:
                    client = self._manager.get(session_name)
                    if client and client.client:
                        me = await client.client.get_me()
                        if me:
                            return {
                                "account": session_name, 
                                "status": "ok", 
                                "valid": True, 
                                "phone": me.phone, 
                                "id": me.id,
                                "username": getattr(me, 'username', None),
                                "first_name": getattr(me, 'first_name', None),
                            }
                except (UserDeactivatedError, UserDeactivatedBanError):
                    return {"account": session_name, "status": "banned", "valid": False}
                except Exception as e:
                    return {"account": session_name, "status": "error", "valid": False, "detail": str(e)}
                
                return {"account": session_name, "status": "unknown", "valid": False}
                
            except Exception as e:
                return {"account": session_name, "status": "error", "valid": False, "detail": str(e)}
        
        # 如果没有 manager，返回文件存在但未检查
        return {"account": session_name, "status": "not_loaded", "valid": False, "detail": "Manager not available"}

    async def check_all_accounts(self):
        count = getattr(CONFIG, "ACCOUNT_COUNT", 100)
        prefix = getattr(CONFIG, "ACCOUNT_PREFIX", "account")
        names = [f"{prefix}_{i:02d}" for i in range(1, count + 1)]
        
        tasks = [self.check_account(name) for name in names]
        results = await asyncio.gather(*tasks)
        return results

    def delete_session(self, session_name: str):
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
