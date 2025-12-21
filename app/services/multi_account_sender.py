"""
多账号并发发送服务

功能:
1. 将群组分配给多个账号并发发送
2. 每个账号之间有随机延迟,防止风控
3. 智能负载均衡和错误处理
4. 账号级别的速率限制
"""
import asyncio
import random
import time
from typing import List, Dict, Optional, Callable
from dataclasses import dataclass, field
from app.config import CONFIG
from app.telegram_client import MultiTelegramManager
from app.models import SendLog, Task, TaskEvent
from sqlalchemy.orm import Session
import json


@dataclass
class AccountState:
    """账号状态追踪"""
    name: str
    authorized: bool = False
    last_send_time: float = 0.0
    success_count: int = 0
    fail_count: int = 0
    flood_wait_until: float = 0.0  # FloodWait 结束时间
    is_busy: bool = False
    consecutive_errors: int = 0


@dataclass
class SendResult:
    """发送结果"""
    account: str
    group_id: int
    success: bool
    error: Optional[str] = None
    message_id: Optional[int] = None


class MultiAccountSender:
    """多账号并发发送器"""
    
    def __init__(self, manager: MultiTelegramManager):
        self.manager = manager
        self.accounts: Dict[str, AccountState] = {}
        self._semaphore: Optional[asyncio.Semaphore] = None
        self._global_lock = asyncio.Lock()
        
    async def initialize_accounts(self, account_names: List[str]) -> List[str]:
        """
        初始化账号列表,检查授权状态
        返回: 已授权的账号列表
        """
        authorized_accounts = []
        
        for name in account_names:
            try:
                is_auth = await self.manager.is_authorized(name)
                self.accounts[name] = AccountState(name=name, authorized=is_auth)
                if is_auth:
                    authorized_accounts.append(name)
            except Exception as e:
                print(f"[WARN] Failed to check account {name}: {e}")
                self.accounts[name] = AccountState(name=name, authorized=False)
        
        return authorized_accounts

    def _get_available_account(self) -> Optional[str]:
        """获取一个可用的账号 (未被占用且没有 FloodWait)"""
        now = time.monotonic()
        candidates = []
        
        for name, state in self.accounts.items():
            if not state.authorized:
                continue
            if state.is_busy:
                continue
            if state.flood_wait_until > now:
                continue
            # 优先选择错误次数少的账号
            candidates.append((name, state.consecutive_errors, state.last_send_time))
        
        if not candidates:
            return None
        
        # 按错误次数升序,上次发送时间升序排序
        candidates.sort(key=lambda x: (x[1], x[2]))
        return candidates[0][0]

    def _distribute_groups(self, group_ids: List[int], accounts: List[str]) -> Dict[str, List[int]]:
        """
        将群组分配给多个账号
        使用随机打乱 + 轮询分配,确保均匀分布且不可预测
        """
        if not accounts:
            return {}
        
        # 随机打乱群组顺序
        shuffled = list(group_ids)
        random.shuffle(shuffled)
        
        distribution: Dict[str, List[int]] = {acc: [] for acc in accounts}
        
        for i, gid in enumerate(shuffled):
            acc = accounts[i % len(accounts)]
            distribution[acc].append(gid)
        
        return distribution

    async def send_with_account(
        self,
        account: str,
        group_id: int,
        message: str,
        parse_mode: str,
        disable_web_page_preview: bool,
        retry_max: int = 2,
        retry_delay_ms: int = 1500,
    ) -> SendResult:
        """使用指定账号发送消息到群组"""
        state = self.accounts.get(account)
        if not state:
            return SendResult(account=account, group_id=group_id, success=False, error="account_not_found")
        
        state.is_busy = True
        try:
            attempt = 0
            last_error = None
            
            while attempt <= retry_max:
                try:
                    ok, err, msg_id = await self.manager.send_message_to_group(
                        account,
                        group_id=group_id,
                        text=message,
                        parse_mode=parse_mode,
                        disable_web_page_preview=disable_web_page_preview,
                    )
                    
                    if ok:
                        state.success_count += 1
                        state.consecutive_errors = 0
                        state.last_send_time = time.monotonic()
                        return SendResult(
                            account=account,
                            group_id=group_id,
                            success=True,
                            message_id=msg_id,
                        )
                    
                    # 检查是否是 FloodWait
                    if err and "FloodWait" in str(err):
                        # 解析等待时间
                        try:
                            wait_seconds = int(''.join(filter(str.isdigit, str(err)[:20]))) or 60
                        except:
                            wait_seconds = 60
                        state.flood_wait_until = time.monotonic() + wait_seconds
                        return SendResult(
                            account=account,
                            group_id=group_id,
                            success=False,
                            error=f"FloodWait:{wait_seconds}s",
                        )
                    
                    last_error = err
                    
                except Exception as e:
                    last_error = str(e)
                
                attempt += 1
                if attempt <= retry_max:
                    await asyncio.sleep(retry_delay_ms / 1000.0)
            
            # 所有重试都失败
            state.fail_count += 1
            state.consecutive_errors += 1
            state.last_send_time = time.monotonic()
            
            return SendResult(
                account=account,
                group_id=group_id,
                success=False,
                error=last_error or "send_failed",
            )
            
        finally:
            state.is_busy = False

    async def send_to_groups_multi_account(
        self,
        db: Session,
        accounts: List[str],
        group_ids: List[int],
        message: str,
        parse_mode: str,
        disable_web_page_preview: bool,
        delay_ms: int,
        retry_max: int = 2,
        retry_delay_ms: int = 1500,
        task_id: Optional[str] = None,
        on_progress: Optional[Callable[[int, int, int], None]] = None,
    ) -> Dict:
        """
        使用多个账号并发发送消息
        
        Args:
            db: 数据库会话
            accounts: 账号列表
            group_ids: 群组 ID 列表
            message: 消息内容
            parse_mode: 解析模式
            disable_web_page_preview: 是否禁用预览
            delay_ms: 每条消息之间的延迟
            retry_max: 最大重试次数
            retry_delay_ms: 重试延迟
            task_id: 任务 ID (可选,用于更新任务状态)
            on_progress: 进度回调
        
        Returns:
            {"total": int, "success": int, "failed": int, "by_account": {...}}
        """
        # 初始化账号
        authorized = await self.initialize_accounts(accounts)
        if not authorized:
            return {"total": len(group_ids), "success": 0, "failed": len(group_ids), "error": "no_authorized_accounts"}
        
        # 分配群组
        distribution = self._distribute_groups(group_ids, authorized)
        
        # 配置
        max_concurrent = min(CONFIG.MULTI_ACCOUNT_MAX_CONCURRENT, len(authorized))
        stagger_ms = CONFIG.MULTI_ACCOUNT_STAGGER_MS
        base_delay = max(delay_ms, CONFIG.SEND_MIN_DELAY_MS)
        jitter_pct = CONFIG.SEND_JITTER_PCT
        
        # 结果统计
        total = len(group_ids)
        success = 0
        failed = 0
        by_account: Dict[str, Dict] = {acc: {"success": 0, "failed": 0} for acc in authorized}
        
        # 并发控制
        sem = asyncio.Semaphore(max_concurrent)
        # 数据库操作锁 - SQLAlchemy Session 不支持并发访问
        db_lock = asyncio.Lock()
        processed = 0
        stop_flag = False  # 任务停止标志
        
        async def process_account_batch(account: str, gids: List[int]):
            nonlocal success, failed, processed, stop_flag
            
            for gid in gids:
                # 检查停止标志 (无需锁,只读)
                if stop_flag:
                    return
                
                async with sem:
                    # 检查任务是否被停止 (需要锁保护数据库访问)
                    if task_id:
                        async with db_lock:
                            t = db.query(Task).filter(Task.id == task_id).first()
                            if t and t.stop_requested:
                                stop_flag = True
                                return
                    
                    # 发送消息 (不需要锁,这是 Telegram API 调用)
                    result = await self.send_with_account(
                        account=account,
                        group_id=gid,
                        message=message,
                        parse_mode=parse_mode,
                        disable_web_page_preview=disable_web_page_preview,
                        retry_max=retry_max,
                        retry_delay_ms=retry_delay_ms,
                    )
                    
                    # 获取群组标题 (不需要锁,这是 Telegram API 调用)
                    title = str(gid)
                    try:
                        ent = await self.manager.get(account).client.get_entity(gid)
                        title = getattr(ent, 'title', None) or str(gid)
                    except:
                        pass
                    
                    # 数据库操作 (需要锁保护)
                    async with db_lock:
                        log = SendLog(
                            account_name=account,
                            group_id=gid,
                            group_title=title,
                            message_preview=message[:200],
                            status="success" if result.success else "failed",
                            error=result.error,
                            message_id=result.message_id,
                            parse_mode=parse_mode,
                        )
                        db.add(log)
                        
                        if result.success:
                            success += 1
                            by_account[account]["success"] += 1
                        else:
                            failed += 1
                            by_account[account]["failed"] += 1
                        
                        processed += 1
                        
                        # 更新任务进度
                        if task_id:
                            t = db.query(Task).filter(Task.id == task_id).first()
                            if t:
                                t.success = success
                                t.failed = failed
                                t.current_index = processed
                                t.heartbeat_at = CONFIG.now()
                        
                        db.commit()
                    
                    if on_progress:
                        on_progress(processed, success, failed)
                    
                    # 延迟 (带随机抖动,不需要锁)
                    if base_delay > 0:
                        jitter = random.uniform(-jitter_pct, jitter_pct) * base_delay
                        wait_ms = max(0, base_delay + jitter)
                        await asyncio.sleep(wait_ms / 1000.0)
        
        # 启动各账号的发送任务,账号之间有错开延迟
        tasks = []
        for i, (account, gids) in enumerate(distribution.items()):
            if gids:
                # 错开启动时间
                if i > 0 and stagger_ms > 0:
                    await asyncio.sleep(stagger_ms / 1000.0)
                task = asyncio.create_task(process_account_batch(account, gids))
                tasks.append(task)
        
        # 等待所有任务完成
        await asyncio.gather(*tasks, return_exceptions=True)
        
        return {
            "total": total,
            "success": success,
            "failed": failed,
            "by_account": by_account,
        }


# 全局实例
multi_sender: Optional[MultiAccountSender] = None


def get_multi_sender(manager: MultiTelegramManager) -> MultiAccountSender:
    global multi_sender
    if multi_sender is None:
        multi_sender = MultiAccountSender(manager)
    return multi_sender

