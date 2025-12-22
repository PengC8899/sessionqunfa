# 部署记录 (VPS Deployment Log)

本文档用于记录 `qunfa` 脚本的 VPS 部署信息，以便维护和调试。

## 1. 初始 VPS (Singapore)
- **IP 地址**: `47.130.222.65`
- **SSH 用户**: `ubuntu`
- **密钥文件**: `/Users/pclucky/qunfa/xinjiapo.pem`
- **部署路径**: `~/qunfa`
- **配置概览**:
  - `ACCOUNT_COUNT`: 100
- **状态**: 🟢 正常运行
- **最近维护**:
  - 修复前端登录输入框无法输入的问题
  - 修复令牌保存按钮逻辑
  - 优化数据库连接池配置 (解决 `QueuePool limit` 报错)
  - 更新 API ID/Hash
  - **更新**: 自动回复文案 (High-Volume Corporate Accounts)
  - **功能**: 实现随机群组发送顺序 (防止多账号同步并发)
  - **修复**: `/api/send-async-batch` 冷却时间计算的时区错误 (避免 500 / 卡死)
  - **稳定性**: 增加 `1G swap`（`/swapfile`）防止内存峰值导致 SSH 断连

## 2. 孟买 VPS (Mengmai)
- **IP 地址**: `13.203.174.210`
- **SSH 用户**: `ubuntu`
- **密钥文件**: `/Users/pclucky/qunfa/mengmai.pem`
- **部署路径**: `~/qunfa`
- **配置概览**:
  - `ADMIN_TOKEN`: `123456`
  - `ACCOUNT_COUNT`: 100
  - `TG_API_ID`: `24426543`
- **状态**: 🟢 正常运行
- **最近维护**:
  - 全新部署环境 (Docker + Docker Compose)
  - 修复环境变量不生效问题 (Recreated container)
  - 手动创建并授权 `sessions` 目录
  - 优化数据库并发
  - **更新**: 自动回复文案同步
  - **功能**: 随机群发顺序同步
  - **修复**: `/api/send-async-batch` 冷却时间计算的时区错误 (避免 500 / 卡死)
  - **稳定性**: 增加 `1G swap`（`/swapfile`）防止内存峰值导致 SSH 断连



## 5. 备用配置 (Backup Config)
### 备用 Telegram API Credentials
如果主 API (24426543) 失效或被封禁，可切换使用以下备用 API：

- **App api_id**: `32705926`
- **App api_hash**: `f481359244dc35766b10e76d3d76cc2f`

---

## 常用维护命令

### 连接服务器
```bash
# 连接初始 VPS (新加坡)
ssh -i xinjiapo.pem ubuntu@47.130.222.65

# 连接孟买 VPS
ssh -i mengmai.pem ubuntu@13.203.174.210


```

### 重启服务
```bash
cd qunfa
docker compose restart web
```

### 强制重建容器 (更新代码或配置后执行)
```bash
cd qunfa
docker compose up -d --build web
```

### 查看日志
```bash
cd qunfa
docker compose logs -f web --tail 100
```

### 停止所有发送任务 (数据库级)
```bash
docker compose exec -T web python - << 'PY'
from app.database import SessionLocal
from app.models import Task
from datetime import datetime, timezone

session = SessionLocal()
try:
    q = session.query(Task).filter(Task.status == 'running')
    count = 0
    for t in q.all():
        t.stop_requested = 1
        t.paused = 0
        t.status = 'stopped'
        t.finished_at = datetime.now(timezone.utc)
        count += 1
    session.commit()
    print('UPDATED_TASKS', count)
finally:
    session.close()
PY
```
