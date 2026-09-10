import os
import asyncio
import json
import time
import urllib.request
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

# #region debug-point C:health-check-server-report
def _report_health_check_debug(hypothesis_id: str, location: str, msg: str, data: dict | None = None):
    if os.getenv("HEALTH_CHECK_DEBUG", "").lower() not in ("1", "true", "yes"):
        return
    try:
        debug_url = "http://127.0.0.1:7777/event"
        session_id = "health-check-stall"
        try:
            with open(".dbg/health-check-stall.env", "r", encoding="utf-8") as fh:
                content = fh.read().splitlines()
            for line in content:
                if line.startswith("DEBUG_SERVER_URL="):
                    debug_url = line.split("=", 1)[1].strip() or debug_url
                elif line.startswith("DEBUG_SESSION_ID="):
                    session_id = line.split("=", 1)[1].strip() or session_id
        except Exception:
            pass
        payload = json.dumps({
            "sessionId": session_id,
            "runId": "post-fix",
            "hypothesisId": hypothesis_id,
            "location": location,
            "msg": f"[DEBUG] {msg}",
            "data": data or {},
            "ts": int(time.time() * 1000),
        }).encode("utf-8")
        urllib.request.urlopen(
            urllib.request.Request(
                debug_url,
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            ),
            timeout=1,
        ).read()
    except Exception:
        pass
# #endregion

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

    async def check_account(self, session_name: str, use_cache: bool = True, include_group_send_check: bool = False):
        """检查账号状态，支持缓存"""
        # #region debug-point C:check-account-start
        started_at = time.time()
        _report_health_check_debug("C", "app/services/account_service.py:check_account:start", "check_account.start", {
            "account": session_name,
            "use_cache": bool(use_cache),
            "include_group_send_check": bool(include_group_send_check),
        })
        # #endregion
        cache_key = (session_name, bool(include_group_send_check))
        # 检查缓存
        if use_cache and cache_key in self._cache:
            cached_time, cached_result = self._cache[cache_key]
            if time.time() - cached_time < self._cache_ttl:
                # #region debug-point C:check-account-cache-hit
                _report_health_check_debug("C", "app/services/account_service.py:check_account:cache-hit", "check_account.cache-hit", {
                    "account": session_name,
                    "elapsedMs": int((time.time() - started_at) * 1000),
                    "status": cached_result.get("status"),
                })
                # #endregion
                return cached_result
        
        async with self.semaphore:
            timeout_s = max(self.check_timeout, 20) if include_group_send_check else self.check_timeout
            try:
                result = await asyncio.wait_for(
                    self._do_check(session_name, include_group_send_check=include_group_send_check),
                    timeout=timeout_s
                )
            except asyncio.TimeoutError:
                result = {"account": session_name, "status": "timeout", "valid": False, "detail": f"连接超时 ({timeout_s}s)"}
            
            # 缓存结果
            self._cache[cache_key] = (time.time(), result)
            # #region debug-point C:check-account-finish
            _report_health_check_debug("C", "app/services/account_service.py:check_account:finish", "check_account.finish", {
                "account": session_name,
                "elapsedMs": int((time.time() - started_at) * 1000),
                "status": result.get("status"),
                "valid": result.get("valid"),
                "detail": result.get("detail"),
            })
            # #endregion
            return result

    async def _do_check(self, session_name: str, include_group_send_check: bool = False):
        """实际的检查逻辑 - 优化版"""
        session_base = os.path.join(CONFIG.SESSION_DIR, session_name)
        last_error = None
        
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
                        result = {
                            "account": session_name,
                            "status": "ok",
                            "valid": True,
                            "phone": me.phone,
                            "id": me.id,
                            "username": getattr(me, 'username', None),
                            "first_name": getattr(me, 'first_name', None),
                        }
                        if include_group_send_check:
                            result.update(await self._inspect_group_send_capability(session_name))
                            if not result.get("can_send_in_groups", True):
                                result["status"] = "cannot_send"
                        return result
                except asyncio.TimeoutError:
                    return {"account": session_name, "status": "slow", "valid": True, "detail": "连接慢但可用"}
                except (UserDeactivatedError, UserDeactivatedBanError):
                    return {"account": session_name, "status": "banned", "valid": False}
                except Exception as e:
                    last_error = e
            
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
                    result = {
                        "account": session_name,
                        "status": "ok",
                        "valid": True,
                        "phone": me.phone,
                        "id": me.id,
                        "username": getattr(me, 'username', None),
                        "first_name": getattr(me, 'first_name', None),
                    }
                    if include_group_send_check:
                        result.update(await self._inspect_group_send_capability(session_name))
                        if not result.get("can_send_in_groups", True):
                            result["status"] = "cannot_send"
                    return result
            except (UserDeactivatedError, UserDeactivatedBanError):
                return {"account": session_name, "status": "banned", "valid": False}
            except Exception as e:
                last_error = e
            
            if last_error is not None:
                return {
                    "account": session_name,
                    "status": "error",
                    "valid": False,
                    "detail": str(last_error)[:120],
                }
            return {"account": session_name, "status": "unknown", "valid": False, "detail": "无法读取账号资料"}
            
        except Exception as e:
            return {"account": session_name, "status": "error", "valid": False, "detail": str(e)[:50]}

    def clear_cache(self):
        """清除缓存"""
        self._cache.clear()

    async def _inspect_group_send_capability(self, session_name: str):
        if not self._manager:
            return {
                "can_send_in_groups": False,
                "checked_groups": 0,
                "sendable_groups": 0,
                "detail": "管理器未初始化，无法检测群发言权限",
            }
        try:
            # #region debug-point D:group-send-check-start
            started_at = time.time()
            _report_health_check_debug("D", "app/services/account_service.py:_inspect_group_send_capability:start", "group_send_check.start", {
                "account": session_name,
            })
            # #endregion
            result = await asyncio.wait_for(
                self._manager.inspect_group_send_capability(session_name),
                timeout=max(self.check_timeout, 15),
            )
            # #region debug-point D:group-send-check-finish
            _report_health_check_debug("D", "app/services/account_service.py:_inspect_group_send_capability:finish", "group_send_check.finish", {
                "account": session_name,
                "elapsedMs": int((time.time() - started_at) * 1000),
                "checked_groups": result.get("checked_groups"),
                "can_send_in_groups": result.get("can_send_in_groups"),
            })
            # #endregion
            return result
        except asyncio.TimeoutError:
            # #region debug-point D:group-send-check-timeout
            _report_health_check_debug("D", "app/services/account_service.py:_inspect_group_send_capability:timeout", "group_send_check.timeout", {
                "account": session_name,
                "timeoutS": max(self.check_timeout, 15),
            })
            # #endregion
            return {
                "can_send_in_groups": False,
                "checked_groups": 0,
                "sendable_groups": 0,
                "detail": f"群发言权限检查超时 ({self.check_timeout}s)",
            }
        except Exception as exc:
            # #region debug-point D:group-send-check-error
            _report_health_check_debug("D", "app/services/account_service.py:_inspect_group_send_capability:error", "group_send_check.error", {
                "account": session_name,
                "error": str(exc)[:120],
            })
            # #endregion
            return {
                "can_send_in_groups": False,
                "checked_groups": 0,
                "sendable_groups": 0,
                "detail": f"群发言权限检查失败: {str(exc)[:80]}",
            }

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
        # 清除缓存
        for key in list(self._cache.keys()):
            if isinstance(key, tuple):
                if key[0] == session_name:
                    del self._cache[key]
            elif key == session_name:
                del self._cache[key]
        return deleted

account_service = AccountService()
