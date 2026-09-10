# Telegram 群发控制面板

使用 FastAPI + Telethon 构建的 Telegram 多账号群发控制后台，提供简单 Web 面板：选择群/频道、编辑消息、设定解析与间隔、后台异步发送与结果日志。

## 功能特性

- 多账号管理：通过 `.env` 配置多个账号，面板中切换使用
- 群组/频道列表：可搜索、全选/取消、显示成员数与类型标记
- 消息发送：支持 `plain`/`markdown`/`html` 三种解析；可关闭链接预览
- 速率控制：每条可设置 `delay_ms`；内置短窗口去重与请求节流
- 登录流程：在面板内发送验证码并确认登录；也提供 CLI 脚本
- 异步发送：前端轮询任务状态；结果写入数据库并展示最近日志
- Docker 部署：一键 `docker compose up -d`；可选 Caddy 反向代理启用 HTTPS

## 技术架构

- 后端：`Starlette`/`FastAPI 风格` 路由（入口 `main.py`），`Telethon` 负责 Telegram 交互
- 前端：Vue 3 管理面板（`templates/vue_index.html`、`static/vue-admin.js`、`static/vue-admin.css`）
- 数据库：`SQLAlchemy` + SQLite（默认 `sqlite:///./data.db`），模型 `SendLog`
- 会话：Telethon `.session` 文件位于 `SESSION_DIR`
- 容器化：`Dockerfile` + `docker-compose.yml`；可选 `Caddyfile` 进行反向代理

## 目录结构（关键项）

- `main.py`：Web 入口与所有 API 路由
- `app/telegram_client.py`：Telegram 客户端与多账号管理
- `app/services/*.py`：群列表与发送逻辑
- `app/models.py`、`app/database.py`：数据库模型与连接
- `templates/vue_index.html`、`static/vue-admin.*`：当前管理面板页面与交互脚本
- `Dockerfile`、`docker-compose.yml`、`Caddyfile`：容器与反向代理

## 快速开始（本地运行）

要求：Python 3.10+

```bash
git clone <repo_url>
cd project_root
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # 若无示例，可参考下方配置说明
# 编辑 .env（见下文），至少设置 ADMIN_TOKEN 与 TG_API_ID/TG_API_HASH/TG_SESSION_NAME
uvicorn main:app --host 0.0.0.0 --port 8000
```

首次运行如账号未授权，面板会提示“未授权”，请在顶部登录区域完成验证码登录。

## 配置说明（.env）

最小配置（单账号）：

```env
# 后台管理员令牌（访问 API/面板时需要在 Header/页面输入）
ADMIN_TOKEN=your_admin_token_here

# Telegram 单账号配置
TG_API_ID=123456
TG_API_HASH=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
TG_SESSION_NAME=acc1

# 服务器监听（可选）
HOST=0.0.0.0
PORT=8000

# SQLite 数据库（本地默认写到项目根的 data.db）
DB_URL=sqlite:///./data.db

# Telethon 会话目录（默认当前目录），本地建议 `./sessions`
SESSION_DIR=./sessions
```

多账号配置（示例）：

```env
ADMIN_TOKEN=your_admin_token_here

# 声明账号列表（逗号分隔）
TG_ACCOUNTS=acc1_vps,acc2,acc3

# 每个账号提供独立的 API_ID/API_HASH/SESSION_NAME
TG_acc1_vps_API_ID=111111
TG_acc1_vps_API_HASH=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
TG_acc1_vps_SESSION_NAME=acc1_vps

TG_acc2_API_ID=222222
TG_acc2_API_HASH=bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
TG_acc2_SESSION_NAME=acc2

TG_acc3_API_ID=333333
TG_acc3_API_HASH=cccccccccccccccccccccccccccccccc
TG_acc3_SESSION_NAME=acc3

# 数据库与会话目录（容器与本地可不同）
DB_URL=sqlite:///./data.db
SESSION_DIR=./sessions
```

Docker 部署时如需持久化数据库到挂载目录，建议设置：

```env
# 在 docker-compose 中默认挂载了 /app/data
DB_URL=sqlite:////app/data/data.db
SESSION_DIR=/sessions
```

## 登录与会话

- 面板登录：在首页顶部输入手机号，点击“发送验证码”，随后输入验证码与（如有）二次密码，点击“确认登录”。
- CLI 登录（可选）：

```bash
python login.py \
  --api_id 123456 \
  --api_hash xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx \
  --session acc1 \
  --phone +8613812345678 
# 可加：--send_code_only 1 仅发送验证码；随后再次调用传入 --code 12345
```

会话文件位于 `SESSION_DIR` 下（如 `./sessions/acc1.session`）。

## Docker 部署

```bash
# 构建并启动（后台运行）
docker compose up -d

# 查看容器状态
docker ps

# 查看日志
docker logs -f tg-bulk-web
docker logs -f tg-bulk-caddy
```

`docker-compose.yml` 要点：

- `web` 服务映射端口 `8000:8000`，并设置 `SESSION_DIR=/sessions`；挂载 `./sessions:/sessions`、`./data:/app/data`
- `caddy` 反向代理到 `web:8000`，开放 `80/443`；域名在 `Caddyfile` 中配置

## 反向代理与 HTTPS（Caddy）

示例 `Caddyfile`（参考仓库中的文件）：

```
你的域名.com, www.你的域名.com {
    encode gzip
    header {
        Strict-Transport-Security "max-age=31536000; includeSubDomains; preload"
    }
    reverse_proxy web:8000
}
```

将域名解析到服务器 IP 并确保开放 `80/443`，即可自动申请证书并通过 HTTPS 访问。

## 使用指南（面板）

- 输入并保存管理员令牌（顶部“管理员令牌”）
- 选择账号（顶部下拉“账号”），必要时完成登录（验证码与二次密码）
- 左侧“群组快选”中勾选需要发送的群组；支持搜索、全选/清空、刷新
- 在“邀请任务”页编辑消息：选择解析模式 `plain/markdown/html`，设置间隔、轮数、轮次间隔，按需关闭链接预览
- 发送：
  - “发送到选中群组”创建单账号异步任务
  - “已授权账号批量群发”按当前可用账号批量创建任务
  - “测试发送”立即返回结果，不加入异步队列
- 总览页可查看任务摘要、最近日志；健康检查页可逐账号检查可用性与群内发言能力

## API 文档（需 Header：`X-Admin-Token: <ADMIN_TOKEN>`）

- `GET /api/accounts` → 返回可用账号列表
- `GET /api/groups?only_groups=true|false&account=<name>` → 返回群/频道列表
- `GET /api/account-status?account=<name>` → 返回 `{ account, authorized }`
- `POST /api/login/send-code` → `{"account","phone","force_sms"}`；返回 200 或 429（flood_wait）
- `POST /api/login/confirm` → `{"account","phone","code","password"}`；成功返回 `{ ok: true, user }`
- `POST /api/test-send` → 同步发送，返回 `{ total, success, failed }`
- `POST /api/send-async` → 异步任务，返回 `{ task_id }`
- `GET /api/task-status?task_id=...` → 返回任务进度 `{ total, success, failed, status }`
- `GET /api/logs?limit=50` → 返回最近发送记录

请求去重与节流：服务器在短窗口内对同一令牌做节流，并对重复 `request_id` 拦截（详见 `main.py:163`）。

## 数据与日志

- 数据库模型 `SendLog` 字段：`account_name`、`group_id`、`group_title`、`message_preview`、`status`、`error`、`created_at`
- 状态值：`success` / `failed` / `skipped`（短时间内重复内容会跳过，详见发送服务逻辑）
- 默认数据库为项目根的 `data.db`；生产环境建议将 `DB_URL` 指到容器挂载目录（如 `/app/data/data.db`）以持久化

## 常见问题与排查

- 401 Unauthorized：令牌错误或未设置，在页面顶部保存正确的 `ADMIN_TOKEN`
- 403 session_not_authorized：账号尚未登录，完成验证码/二次密码登录后重试
- 429 Too Many Requests：请求节流或重复 `request_id`；等待片刻再发
- 429 flood_wait（发送验证码）：Telethon 触发频率限制，按提示秒数等待
- 发送失败：可能目标不允许发言/账号受限/解析错误，详见最近日志中的 `error`
- 无法持久化数据：确认 `DB_URL` 指向挂载目录（Docker 推荐 `sqlite:////app/data/data.db`）
- 端口访问失败：在云防火墙中开放 `22/80/443/8000`

## 安全与合规

- 管理员令牌仅在本系统内部校验，请妥善保管并避免泄露
- 请遵守 Telegram 使用条款，勿用于垃圾信息或骚扰行为
- 对大量群发设置合理 `delay_ms`，避免账号被限制

---

如需进一步定制（多会话并行、分布式发送、任务队列、角色权限、域名与证书自动化等），可在现有架构基础上扩展。
