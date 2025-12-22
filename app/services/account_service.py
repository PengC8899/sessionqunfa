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
        self.semaphore = asyncio.Semaphore(3)  # 减少并发，避免网络拥塞
        self._manager = None
        self.check_timeout = 10  # 减少超时到 10 秒
        self._cache = {}  # 缓存检查结果
        self._cache_ttl = 60  # 缓存 60 秒
    
    def set_manager(self, manager):
        """Set the multi_manager reference"""
        self._manager = manager

    async def check_account(self, session_name: str, use_cache: bool = True):
        """检查账号状态，支持缓存"""
        # 检查缓存
        if use_cache and session_name in self._cache:
            cached_time, cached_result = self._cache[session_name]
            import time
            if time.time() - cached_time < self._cache_ttl:
                return cached_result
        
        async with self.semaphore:
            try:
                result = await asyncio.wait_for(
                    self._do_check(session_name),
                    timeout=self.check_timeout
                )
            except asyncio.TimeoutError:
                result = {"account": session_name, "status": "timeout", "valid": False, "detail": f"连接超时 ({self.check_timeout}s)"}
            
            # 缓存结果
            import time
            self._cache[session_name] = (time.time(), result)
            return result

    async def _do_check(self, session_name: str):
        """实际的检查逻辑 - 优化版"""
        session_base = os.path.join(CONFIG.SESSION_DIR, session_name)
        
        # 1. 检查 session 文件是否存在
        if not os.path.exists(f"{session_base}.session"):
            return {"account": session_name, "status": "missing_file", "valid": False}
        
        if not self._manager:
            return {"account": session_name, "status": "file_exists", "valid": True, "detail": "Session 文件存在"}
        
        try:
            # 2. 检查是否已在 manager 中加载
            mgr = self._manager.get(session_name)
            
            # 3. 快速检查：如果客户端已连接，直接用缓存的信息
            if mgr.client and mgr.client.is_connected():
                try:
                    # 使用超短超时获取用户信息
                    me = await asyncio.wait_for(mgr.client.get_me(), timeout=5)
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
                except asyncio.TimeoutError:
                    return {"account": session_name, "status": "slow", "valid": True, "detail": "连接慢但可用"}
                except (UserDeactivatedError, UserDeactivatedBanError):
                    return {"account": session_name, "status": "banned", "valid": False}
                except Exception:
                    pass
            
            # 4. 如果未连接，尝试连接（可能较慢）
            try:
                await asyncio.wait_for(mgr.ensure_connected(), timeout=8)
            except asyncio.TimeoutError:
                return {"account": session_name, "status": "connect_slow", "valid": False, "detail": "连接超时"}
            except Exception as e:
                return {"account": session_name, "status": "connect_failed", "valid": False, "detail": str(e)[:50]}
            
            # 5. 检查授权
            try:
                authorized = await asyncio.wait_for(mgr.client.is_user_authorized(), timeout=5)
                if not authorized:
                    return {"account": session_name, "status": "unauthorized", "valid": False}
            except Exception as e:
                return {"account": session_name, "status": "auth_error", "valid": False, "detail": str(e)[:50]}
            
            # 6. 获取用户信息
            try:
                me = await asyncio.wait_for(mgr.client.get_me(), timeout=5)
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
            except Exception:
                pass
            
            return {"account": session_name, "status": "unknown", "valid": False}
            
        except Exception as e:
            return {"account": session_name, "status": "error", "valid": False, "detail": str(e)[:50]}

    def clear_cache(self):
        """清除缓存"""
        self._cache.clear()

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
        # 清除缓存
        if session_name in self._cache:
            del self._cache[session_name]
        return deleted

account_service = AccountService()
