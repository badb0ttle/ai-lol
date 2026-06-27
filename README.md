# AI Task Backend

> 把一次 AI 请求做成可异步执行、可查询状态、可处理失败、可本地复现的后端系统。

---

## 项目概述

| 能力 | 实现 |
|------|------|
| 异步执行 | `POST /tasks` 投递到 RabbitMQ → Worker 消费 → Agent 循环 |
| 全链路持久化 | 每一步工具调用（think → tool_call → tool_result → reply）记录到 PostgreSQL |
| 失败可复盘 | `GET /tasks/{id}` 返回完整 steps 序列 |
| 幂等去重 | Redis SET NX + PostgreSQL UNIQUE 双层保护 |
| 任务超时 | `asyncio.wait_for` 包裹 Agent 执行 |
| 工具重试 | 每个工具调用最多 2 次尝试 |
| 缓存加速 | `GET /tasks/{id}` Redis 读缓存，Worker 更新后主动失效 |
| 死信队列 | 3 次重试耗尽 → 入 `ai_tasks.dlq`，消息不丢失 |
| 一键启动 | `docker compose up -d`，Alembic 迁移自动执行 |

---

## 系统架构

```
┌────────────────────────────────────────────────────────────┐
│                      Docker Compose                         │
│                                                            │
│  ┌──────────┐   ┌──────────┐   ┌──────────┐              │
│  │PostgreSQL│   │  Redis   │   │ RabbitMQ │              │
│  │   :5432  │   │  :6379   │   │  :5672   │              │
│  │          │   │          │   │  :15672  │ (管理UI)     │
│  └────┬─────┘   └────┬─────┘   └────┬─────┘              │
│       │              │              │                     │
│  ┌────┴──────────────┴──────────────┴───────┐            │
│  │              FastAPI :8000                │            │
│  │  POST /chat           (同步对话)          │            │
│  │  POST /chat/stream    (SSE 流式)         │            │
│  │  POST /tasks          (提交异步任务)      │            │
│  │  GET  /tasks/{id}     (查询任务状态)      │            │
│  │  GET  /health         (健康检查)          │            │
│  └──────────────────┬────────────────────────┘            │
│                     │ publish                              │
│  ┌──────────────────▼────────────────────────┐            │
│  │            Worker (独立子进程)              │            │
│  │  ┌──────────────────────────────────┐     │            │
│  │  │       Agent (ReAct 循环)          │     │            │
│  │  │  调 LLM → 解析 tool_call         │     │            │
│  │  │  → 执行工具 → 记录 step → 循环   │     │            │
│  │  └──────────────────────────────────┘     │            │
│  │  工具: get_current_time / calculate       │            │
│  │        fetch_url / file_reader            │            │
│  │                                           │            │
│  │  可靠性: 工具重试 2 次                     │            │
│  │          任务超时 asyncio.wait_for         │            │
│  │          死信队列 3 次重试 → DLQ           │            │
│  └───────────────────────────────────────────┘            │
└────────────────────────────────────────────────────────────┘
```

---

## 三条请求路线

| 路线 | 接口 | 延迟 | 适用场景 |
|------|------|------|---------|
| 同步对话 | `POST /chat` | 1-5s | 简单问答、翻译 |
| SSE 流式 | `POST /chat/stream` | 流式输出 | 长文生成、代码生成 |
| 异步任务 | `POST /tasks` → Worker | 30-120s | 多步工具调用 |

---

## 技术栈

| 组件 | 技术 | 理由 |
|------|------|------|
| **接入层** | FastAPI + uvicorn | 异步原生，SSE 支持，Pydantic 校验 |
| **消息队列** | RabbitMQ + aio-pika | 持久化 + 手动 ACK + 死信队列 |
| **消费端** | 独立 Worker 进程 | 生命周期与 API 解耦，故障隔离 |
| **Agent 引擎** | 自实现 ReAct 循环 | 透明可控，每步可记录 |
| **持久存储** | PostgreSQL + SQLAlchemy 2.0 async | 异步引擎，关系型强一致性 |
| **迁移工具** | Alembic | autogenerate 从模型生成迁移 |
| **缓存 + 幂等** | Redis | SET NX 去重 + 状态缓存 |
| **容器化** | Docker Compose | 5 服务，一条命令启动 |

---

## 快速启动

```bash
# 1. 克隆 + 配置
git clone <repo-url> && cd hjh-ai-lol
cp .env.example .env.dev    # 编辑填入 LLM_API_KEY

# 2. 一键启动（Alembic 迁移自动执行）
docker compose up -d

# 3. 验证
curl http://localhost:8000/health
# → {"status":"ok"}

# 4. 端到端演示
./demo.sh
```

**服务端口：**

| 服务 | 端口 | 用途 |
|------|------|------|
| FastAPI | 8000 | API 服务 |
| PostgreSQL | 5432 | 数据库 |
| Redis | 6379 | 缓存 |
| RabbitMQ | 5672 | AMQP |
| RabbitMQ 管理 | 15672 | 队列监控 (dev/devpass123) |

---

## 环境变量

| 变量 | 必填 | 默认值 | 说明 |
|------|------|--------|------|
| `LLM_API_KEY` | ✅ | — | DeepSeek API 密钥 |
| `LLM_BASE_URL` | — | `https://api.deepseek.com` | 兼容 OpenAI 接口格式 |
| `LLM_MODEL` | — | `deepseek-v4-flash` | 模型名 |
| `LLM_TIMEOUT` | — | `60` | 单次 LLM 调用超时（秒） |
| `MAX_LOOPS` | — | `10` | Agent 最大工具调用轮数 |
| `TASK_TIMEOUT` | — | `120` | 单任务总超时（秒） |
| `DATABASE_URL` | — | `postgresql+asyncpg://dev:***@postgres:5432/ai_tasks` | 本地开发用 `127.0.0.1` |
| `REDIS_URL` | — | `redis://redis:6379` | Redis 连接串 |
| `AMQP_URL` | — | `amqp://dev:***@rabbitmq:5672/` | RabbitMQ 连接串 |

---

## API 文档

### `POST /chat` — 同步对话

```bash
curl -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -d '{
    "model": "deepseek-v4-flash",
    "messages": [
      {"role": "user", "content": "你好，请用三句话介绍你自己"}
    ]
  }'
```

### `POST /chat/stream` — SSE 流式

```bash
curl -N -X POST http://localhost:8000/chat/stream \
  -H "Content-Type: application/json" \
  -d '{
    "model": "deepseek-v4-flash",
    "messages": [
      {"role": "user", "content": "写一首关于秋天的五言诗"}
    ]
  }'
```

响应逐块推送 `data:` 行。

### `POST /tasks` — 提交异步任务

```bash
curl -X POST http://localhost:8000/tasks \
  -H "Content-Type: application/json" \
  -d '{
    "instruction": "读取 data/README.md 并总结要点",
    "context": "这是一个演示任务",
    "idempotent_key": "optional-key"
  }'
```

响应（立即返回）：

```json
{"status": "queued", "task_id": "a1b2c3d4-..."}
```

### `GET /tasks/{id}` — 查询任务状态

```bash
curl http://localhost:8000/tasks/a1b2c3d4-...
```

响应（任务完成后）：

```json
{
  "task_id": "a1b2c3d4-...",
  "status": "success",
  "result": "该项目的核心是...",
  "error": null,
  "steps": [
    {"seq": 1, "type": "think",     "content": "...", "tool_name": null},
    {"seq": 2, "type": "tool_call", "content": "{"name":"file_reader",...}", "tool_name": "file_reader"},
    {"seq": 3, "type": "tool_result", "content": "{"path":"README.md",...}", "tool_name": "file_reader"},
    {"seq": 4, "type": "reply",     "content": "该项目的核心是...", "tool_name": null}
  ],
  "created_at": "2026-06-27T10:30:00+00:00",
  "updated_at": "2026-06-27T10:30:12+00:00"
}
```

---

## Agent 执行流程

使用 **ReAct（Reasoning + Acting）** 模式，每步记录到 `task_steps` 表：

```
POST /tasks → RabbitMQ → Worker 消费
    │
    ├── 幂等检查: success/running 则跳过
    ├── 标记 running → 删除 Redis 缓存
    │
    └── Agent 循环 (最多 MAX_LOOPS 轮)
        │
        ├── 调用 LLM（带 4 个工具定义）
        │   │
        │   ├── 有 tool_calls?
        │   │   ├── record_step("tool_call") → 执行工具
        │   │   │   ├── 成功 → record_step("tool_result") → 结果喂回 LLM
        │   │   │   └── 失败 → 重试 1 次 → 仍失败则 tool_result 记录错误
        │   │   └── 回到 LLM（工具结果在 messages 中）
        │   │
        │   └── 无 tool_calls?
        │       ├── 有文本 → record_step("think") 记录推理
        │       └── 最终回复 → record_step("reply") → 返回
        │
        ├── 成功 → status=success, result=最终回复, 删除缓存
        ├── 失败 → 判断 retry_count
        │   ├── > 0 → 重新入队 (x-retry-count 减 1)
        │   └── = 0 → 入死信队列 ai_tasks.dlq
        └── 超时 (asyncio.wait_for) → status=failed
```

---

## 工具

| 工具 | 功能 | 安全措施 |
|------|------|---------|
| `get_current_time` | 返回 UTC 当前时间 | 无参数 |
| `calculate` | 安全数学表达式求值 | AST 白名单 + 禁用 `__builtins__` |
| `fetch_url` | HTTP GET 抓取网页 | 10s 超时，截断 2000 字符 |
| `file_reader` | 读取 `data/` 目录下文件 | `normpath` 防路径穿越，截断 5000 字符 |

工具定义在 `app/agent_core.py` 的 `TOOLS` 列表中，OpenAI 兼容 function calling 格式（DeepSeek 原生支持）。

---

## 可靠性设计

### 幂等去重 (双层)

```
POST /tasks (idempotent_key = "abc")
    │
    ├── 1. Redis SET NX "idempotent:abc" EX 86400
    │      ├── 成功 → 继续
    │      └── 失败 → 返回 duplicate + 已有 task_id
    │
    └── 2. DB IntegrityError 二次兜底（Redis 重启丢数据时）
```

### 死信队列

```
入队 (x-retry-count=3)
    ↓ 失败
重新入队 (x-retry-count=2)
    ↓ 失败
重新入队 (x-retry-count=1)
    ↓ 失败
重新入队 (x-retry-count=0)
    ↓ 失败
入 ai_tasks.dlq（保留 x-error 头供排查）
```

### 三层防线

| 层 | 机制 | 兜底场景 |
|----|------|---------|
| 1 | `try → ack` | 正常执行 |
| 2 | `except → retry/DLQ → ack` | Agent 执行失败 |
| 3 | `except → nack requeue=True` | retry 逻辑本身炸了 |

---

## Redis 缓存策略

```
GET /tasks/{id}
    │
    ├── 1. Redis GET "task:{id}"
    │      ├── 命中 → 直接返回（300s TTL 内）
    │      └── 未命中 → 查 DB → 写回 Redis (SETEX 300s) → 返回
    │
Worker 更新 task 状态时 → DELETE "task:{id}" 主动失效
```

---

## 数据库表

采用 `asyncpg` + SQLAlchemy 2.0 async，Alembic 管理迁移：

| 表 | 用途 |
|----|------|
| `conversations` | 会话记录 |
| `messages` | 对话消息 (system/user/assistant/tool) |
| `tasks` | 异步任务 (idempotent_key UNIQUE) |
| `task_steps` | Agent 执行步骤 (think/tool_call/tool_result/reply) |

迁移在 API 容器启动时自动执行：`alembic upgrade head && uvicorn ...`

---

## 项目结构

```
hjh-ai-lol/
├── docker-compose.yml      # 5 服务编排
├── Dockerfile              # 多阶段构建 (builder + runtime)
├── pyproject.toml          # 依赖声明
├── .env.dev                # 环境变量（不提交）
├── demo.sh                 # 端到端演示脚本
├── README.md
│
├── alembic/
│   ├── env.py              # 异步引擎 + 模型导入
│   ├── script.py.mako      # 迁移模板
│   └── versions/           # 迁移文件
│
├── app/
│   ├── main.py             # FastAPI 入口 + 路由
│   ├── worker.py           # RabbitMQ 消费者（独立进程）
│   ├── agent_core.py       # Agent ReAct 循环 + 工具定义
│   ├── models.py           # SQLAlchemy 模型
│   ├── db.py               # 异步引擎 + sessionmaker
│   └── config.py           # 配置 (pydantic-settings)
│
└── data/                   # file_reader 安全文件区
    └── .gitkeep
```

---

## Docker Compose 服务

| 服务 | 镜像/构建 | 端口 | 启动命令 |
|------|----------|------|---------|
| `postgres` | `postgres:16-alpine` | 5432 | — |
| `redis` | `redis:7-alpine` | 6379 | — |
| `rabbitmq` | `rabbitmq:4-management-alpine` | 5672, 15672 | — |
| `api` | 本地构建 (Dockerfile) | 8000 | `alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000` |
| `worker` | 本地构建 (Dockerfile) | — | `python -m app.worker` |

**Dockerfile：** 多阶段构建 — builder 阶段安装 gcc + 编译依赖，runtime 阶段只复制 wheel，镜像精简。

---

## 演示验收

```bash
# 1. 提交异步任务（带文件读取）
curl -X POST http://localhost:8000/tasks \
  -H "Content-Type: application/json" \
  -d '{
    "instruction": "读取 data/ 目录下 README.md 并总结这个项目的核心功能",
    "context": ""
  }'
# → {"status":"queued","task_id":"xxx-..."}

# 2. 查询状态
curl http://localhost:8000/tasks/xxx-...

# 3. 幂等验证
curl -X POST http://localhost:8000/tasks \
  -H "Content-Type: application/json" \
  -d '{"instruction":"test","idempotent_key":"demo-001"}'
# 第二次同 key → {"status":"duplicate","task_id":"..."}

# 4. 一键全流程
./demo.sh
```

---

## 已知限制

1. **单 Worker**：当前 1 个消费者串行处理。高并发需多 Worker 实例 + `prefetch_count` 调优
2. **无认证**：API 无鉴权。生产需 JWT / API Key 中间件
3. **无速率限制**：可无限提交。生产需 slowapi 令牌桶
4. **LLM 单一 provider**：仅 DeepSeek。生产需抽象 `BaseLLMProvider` + fallback
5. **无监控**：无 Prometheus/Grafana。需补齐队列深度、延迟、失败率仪表盘
6. **Tool sandbox 有限**：`fetch_url` SSRF 防护简化。生产需 Firecracker/gVisor 沙箱

---

## License

MIT
