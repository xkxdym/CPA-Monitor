# CPA-Monitor

## Git + Docker Compose 部署

完整部署与更新说明见 [DEPLOY_DOCKER.md](DEPLOY_DOCKER.md)。

快速部署：

```bash
git clone https://github.com/xkxdym/CPA-Monitor.git CPA-Monitor
cd CPA-Monitor
docker compose up -d --build
```

更新：

```bash
cd CPA-Monitor
git pull
docker compose up -d --build
```

访问地址：

```text
http://服务器IP:18088
```

本地项目（Python 标准库 + SQLite）：
- 多账号 `profile` 管理（新增/编辑/删除/切换 active）
- 手动拉取、后端定时拉取
- 模型统计（含 `source` 账号维度）
- 趋势图与拉取日志
- 数据保留天数自动清理 + 手动清理

## 1. 启动

```powershell
python server.py
```

访问：

```text
http://127.0.0.1:8088
```

页面：
- 监控页：`/`（CPA监控）
- 配置页：`/config.html`

## 2. 核心概念

- `profile`：一套 Router 管理 API 凭据与拉取参数
  - `name / base_url / token / endpoint_mode / queue_count`
- `active_profile`：当前生效账号，统计与定时拉取都基于它
- 全局配置：
  - `refresh_interval_sec`
  - `auto_refresh_enabled`
  - `lookback_hours`
  - `retention_days`

## 3. SQLite 表

数据库：`stats.db`

- `app_config`：全局配置 + active profile
- `profiles`：多账号配置
- `usage_records`：拉取后的聚合记录（带 `profile_id/profile_name`）
- `pull_snapshots`：每次拉取总览（用于趋势）
- `pull_logs`：每次拉取日志和 HTTP trace

## 4. API

### 读取
- `GET /api/health`
- `GET /api/config`
- `GET /api/profiles`
- `GET /api/stats?hours=24&keyword=&profile_id=1`
- `GET /api/trend?hours=24&limit=200&profile_id=1`
- `GET /api/logs?limit=20&profile_id=1`
- `GET /api/records?hours=24&limit=300&keyword=&profile_id=1`

### 写入
- `POST /api/config`
- `POST /api/profiles/upsert`
- `POST /api/profiles/select`
- `POST /api/profiles/delete`
- `POST /api/refresh`
- `POST /api/cache/prune`
- `POST /api/cache/clear`

## 5. 注意

- `usage-queue` 是出队语义，读取后可能消费记录。
- `token` 仅保存在本地 SQLite；前端编辑 profile 时 `token` 留空表示“不改原 token”。

## 6. Docker Compose 部署

在项目根目录执行：

```powershell
docker compose up -d --build
```

查看状态与日志：

```powershell
docker compose ps
docker compose logs -f
```

访问地址：

```text
http://127.0.0.1:18088
```

停止服务：

```powershell
docker compose down
```

说明：

- SQLite 数据文件映射到宿主机 `./data/stats.db`，容器重建后仍会保留。
- 关键配置来自 `docker-compose.yml`：
  - `HOST=0.0.0.0`
  - `PORT=8088`
  - `DB_PATH=/app/data/stats.db`
  - 端口映射 `18088:8088`
