from sqlalchemy import Column, Integer, String, DateTime, Text
from sqlalchemy.sql import func
from app.database import Base


class SendLog(Base):
    __tablename__ = "send_logs"

    id = Column(Integer, primary_key=True, index=True)
    account_name = Column(String(64), index=True)
    group_id = Column(Integer, index=True)
    group_title = Column(String(255))
    message_preview = Column(Text)
    status = Column(String(32))
    error = Column(Text, nullable=True)
    message_id = Column(Integer, nullable=True)
    parse_mode = Column(String(16), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class GroupCache(Base):
    __tablename__ = "group_caches"

    id = Column(Integer, primary_key=True, index=True)
    account_name = Column(String(64), index=True)
    only_groups = Column(Integer)
    data_json = Column(Text)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class Task(Base):
    __tablename__ = "tasks"

    id = Column(String(64), primary_key=True, index=True)
    status = Column(String(32), index=True)
    total = Column(Integer)
    success = Column(Integer)
    failed = Column(Integer)
    account_name = Column(String(64), index=True)
    message = Column(Text)
    parse_mode = Column(String(16))
    disable_web_page_preview = Column(Integer)
    delay_ms = Column(Integer)
    current_index = Column(Integer)
    group_ids_json = Column(Text)
    request_id = Column(String(128), nullable=True)
    paused = Column(Integer, default=0)
    stop_requested = Column(Integer, default=0)
    started_at = Column(DateTime(timezone=True), server_default=func.now())
    finished_at = Column(DateTime(timezone=True), nullable=True)
    heartbeat_at = Column(DateTime(timezone=True), nullable=True)
    rounds = Column(Integer, default=1)
    current_round = Column(Integer, default=1)
    round_interval_s = Column(Integer, default=0)
    next_round_at = Column(DateTime(timezone=True), nullable=True)


class TaskEvent(Base):
    __tablename__ = "task_events"

    id = Column(Integer, primary_key=True, index=True)
    task_id = Column(String(64), index=True)
    ts = Column(DateTime(timezone=True), server_default=func.now())
    event = Column(String(32))
    detail = Column(Text)
    meta_json = Column(Text)


class AccountHealth(Base):
    """账号健康状态跟踪"""
    __tablename__ = "account_health"

    id = Column(Integer, primary_key=True, index=True)
    account_name = Column(String(64), unique=True, index=True)
    status = Column(String(32), default="pending")  # ok, error, pending, banned
    last_login_at = Column(DateTime(timezone=True), nullable=True)
    last_join_at = Column(DateTime(timezone=True), nullable=True)
    last_error = Column(Text, nullable=True)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class SystemKV(Base):
    __tablename__ = "system_kv"

    id = Column(Integer, primary_key=True, index=True)
    k = Column(String(64), unique=True, index=True)
    v = Column(Text)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
