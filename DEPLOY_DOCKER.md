# Docker Compose 部署

在 `router-stats-sqlite` 目录执行：

```powershell
docker compose up -d --build
```

查看状态：

```powershell
docker compose ps
docker compose logs -f
```

访问地址：

```text
http://127.0.0.1:8088
```

停止服务：

```powershell
docker compose down
```

说明：

- SQLite 数据文件落在宿主机 `./data/stats.db`，容器重建后仍会保留。
- 关键环境变量在 `docker-compose.yml` 中：
  - `HOST=0.0.0.0`
  - `PORT=8088`
  - `DB_PATH=/app/data/stats.db`
