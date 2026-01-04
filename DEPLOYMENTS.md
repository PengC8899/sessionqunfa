# 部署记录 (VPS Deployment Log)

本文档用于记录 `qunfa` 脚本的 VPS 部署信息，以便维护和调试。

## VPS 映射
- 名称：mengmai
- 公网 IP：15.207.200.154
- SSH 密钥路径：/Users/pclucky/sessionqunfa/mengmai.pem
- 远程目录：/home/ubuntu/sessionqunfa
- 备注：使用环境变量覆盖部署脚本参数进行更新

## 域名映射
- 主域名：7575.pro
- 通配符：*.7575.pro（仅解析；证书默认签发 7575.pro 与 www.7575.pro）
- 解析到：15.207.200.154
- 反向代理：Caddy 自动申请证书并反代到 web:8000
- 注意：
  - 自动 HTTPS 将为 7575.pro / www.7575.pro 申请证书；通配符需 DNS-01（不在当前配置范围）
  - 首次签发需等待 DNS 生效；80/443 端口需开放

## 5. 备用配置 (Backup Config)
### 备用 Telegram API Credentials
如果主 API (24426543) 失效或被封禁，可切换使用以下备用 API：

- **App api_id**: `32705926`
- **App api_hash**: `f481359244dc35766b10e76d3d76cc2f`

---

## 常用维护命令
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
