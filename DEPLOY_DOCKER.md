# Docker Compose 部署与更新

本文说明如何通过 Git 拉取代码，并使用 Docker Compose 部署、查看状态和更新 CPA-Monitor。

## 1. 环境要求

- 已安装 Git
- 已安装 Docker 和 Docker Compose v2
- 服务器可访问代码仓库

检查命令：

```bash
git --version
docker --version
docker compose version
```

## 2. 首次部署

选择部署目录并克隆仓库：

```bash
mkdir -p /opt
cd /opt
git clone https://github.com/xkxdym/CPA-Monitor.git CPA-Monitor
cd CPA-Monitor
```

启动服务：

```bash
docker compose up -d --build
```

查看容器状态和日志：

```bash
docker compose ps
docker compose logs -f
```

访问地址：

```text
http://服务器IP:18088
```

本机访问：

```text
http://127.0.0.1:18088
```

## 3. 数据持久化

`docker-compose.yml` 已将容器内数据库目录挂载到宿主机：

```yaml
volumes:
  - ./data:/app/data
```

SQLite 数据文件保存在：

```text
./data/stats.db
```

更新镜像或重建容器不会删除该数据文件。需要备份时，备份 `data` 目录即可。

## 4. 更新部署

进入部署目录：

```bash
cd /opt/CPA-Monitor
```

拉取最新代码：

```bash
git pull
```

重新构建并启动：

```bash
docker compose up -d --build
```

确认服务状态：

```bash
docker compose ps
docker compose logs --tail=100
```

## 5. 回滚到指定版本

查看提交记录：

```bash
git log --oneline -n 10
```

切换到指定提交：

```bash
git checkout <commit-id>
docker compose up -d --build
```

恢复到主分支最新版本：

```bash
git checkout main
git pull
docker compose up -d --build
```

如果你的默认分支不是 `main`，请替换为实际分支名。

## 6. 常用运维命令

停止服务：

```bash
docker compose down
```

重启服务：

```bash
docker compose restart
```

实时查看日志：

```bash
docker compose logs -f
```

查看最近日志：

```bash
docker compose logs --tail=200
```

清理旧镜像：

```bash
docker image prune -f
```

## 7. 端口与配置

默认端口映射：

```yaml
ports:
  - "18088:8088"
```

如需修改外部访问端口，例如改为 `28088`：

```yaml
ports:
  - "28088:8088"
```

修改后执行：

```bash
docker compose up -d --build
```

关键环境变量：

- `HOST=0.0.0.0`
- `PORT=8088`
- `DB_PATH=/app/data/stats.db`
