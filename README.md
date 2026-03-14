# ChatGPT 批量注册工具 · Docker 版

> 纯协议实现：DuckMail 临时邮箱 → 自动注册 → Team 邀请 → Codex OAuth 全流程

---

## 🚀 快速启动（Docker 一键）

### 1. 克隆 / 解压项目

```bash
unzip gpt2_docker.zip   # 或 git clone ...
cd gpt2_docker
```

### 2. 配置环境变量（可选）

```bash
cp .env.example .env
# 用编辑器填写 DUCKMAIL_BEARER / DUCKMAIL_DOMAIN 等
nano .env
```

> ✅ 也可以**不配置 .env**，直接启动后在 **Web 面板** 的左侧「设置」中填写并保存。

### 3. 一键启动

```bash
docker compose up -d
```

面板地址：**http://localhost:5000**

### 4. 查看日志

```bash
docker compose logs -f
```

### 5. 停止 / 重启

```bash
docker compose stop
docker compose restart
```

---

## 📂 目录结构

```
gpt2_docker/
├── Dockerfile
├── docker-compose.yml
├── .env.example          # 环境变量模板
├── requirements.txt
├── data/                 # 持久化数据目录（容器内 /data）
│   ├── config.json       # Web 面板配置
│   ├── registered_accounts.txt
│   ├── registered_accounts.csv
│   ├── ak.txt            # Access Token 列表
│   ├── rk.txt            # Refresh Token 列表
│   ├── invite_tracker.json
│   └── codex_tokens/     # 每账号 JSON token
└── codex/
    ├── app.py            # Flask 后端
    ├── config_loader.py  # 注册核心逻辑
    ├── static/
    └── templates/
```

> `data/` 目录通过 Docker volume 挂载，**容器删除不影响数据**。

---

## ⚙️ 配置说明

所有配置均可通过以下方式设置（优先级从高到低）：

| 方式 | 说明 |
|------|------|
| **环境变量** | `.env` 文件或 `docker compose` 的 `environment` |
| **config.json** | Web 面板保存后写入 `data/config.json` |

### 必填项

| 字段 | 说明 |
|------|------|
| `DUCKMAIL_API_BASE` | Worker/Mailcow API 地址 |
| `DUCKMAIL_DOMAIN` | 邮箱域名 |
| `DUCKMAIL_BEARER` | Admin 密码 / Bearer Token |

### 可选项

| 字段 | 说明 |
|------|------|
| `PROXY` | HTTP/SOCKS5 代理，格式 `http://host:port` |
| `UPLOAD_API_URL` / `UPLOAD_API_TOKEN` | CPA 平台上传 |
| `SUB2API_URL` / `SUB2API_TOKEN` | SUB2API 推送 |
| `PORT` | Web 面板端口，默认 `5000` |

---

## 🛠 本地运行（不用 Docker）

```bash
pip install -r requirements.txt
cd codex
python app.py
```

---

## 📌 接口速查

| 接口 | 说明 |
|------|------|
| `GET /api/health` | 健康检查 |
| `GET /api/config` | 读取配置 |
| `POST /api/config` | 保存配置 |
| `POST /api/start` | 启动任务 `{count, workers, proxy}` |
| `POST /api/stop` | 停止任务 |
| `GET /api/status` | 任务状态 + 进度 |
| `GET /api/logs` | SSE 实时日志流 |
| `GET /api/accounts` | 账号列表 |
| `DELETE /api/accounts` | 删除账号 |
| `POST /api/export` | 导出 Token ZIP |
| `GET /api/datainfo` | 数据文件统计 |

---

## 🔄 升级

```bash
docker compose pull   # 拉取新镜像（若使用远程镜像）
# 或本地重新构建
docker compose build --no-cache
docker compose up -d
```

数据目录 `data/` 不受影响。
