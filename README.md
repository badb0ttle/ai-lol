# AI Task Backend

> 把一次 AI 请求做成可异步执行、可查询状态、可处理失败、可本地复现的后端系统。

---

## 零依赖启动

**唯一前提：安装了 Docker Desktop。**

```bash
git clone <repo-url> && cd hjh-ai-lol
cp .env.example .env.dev    # 填入 LLM_API_KEY
docker compose up -d         # 启动 5 个容器
```

一行命令拉起全部服务——不需要装 Python、PostgreSQL、Redis、RabbitMQ、Apifox 或任何其他工具。所有基础设施都在容器里。

```bash
curl http://localhost:8000/health   # → {"status":"ok"}
```

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
│  │  GET  /               (API 总览)          │            │
│  │  POST /chat           (同步对话)          │            │
│  │  POST /chat/stream    (SSE 流式)         │            │
│  │  POST /tasks          (提交异步任务)      │            │
│  │  GET  /tasks/{id}     (查询任务+步骤)     │            │
│  │  GET  /health         (健康检查)          │            │
│  └──────────────────┬────────────────────────┘            │
│                     │ publish                              │
│  ┌──────────────────▼────────────────────────┐            │
│  │            Worker (独立进程)                │            │
│  │  ┌──────────────────────────────────┐     │            │
│  │  │       Agent (ReAct 循环)          │     │            │
│  │  │  调 LLM → 解析 tool_call         │     │            │
│  │  │  → 执行工具 → 记录 step → 循环   │     │            │
│  │  └──────────────────────────────────┘     │            │
│  │                                           │            │
│  │  工具: get_current_time / calculate       │            │
│  │        fetch_url / run_command            │            │
│  │                                           │            │
│  │  可靠性: 工具重试 + 任务超时               │            │
│  │          死信队列 3 次重试 → DLQ           │            │
│  └───────────────────────────────────────────┘            │
└────────────────────────────────────────────────────────────┘
```

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

## 三条请求路线

| 路线 | 接口 | 延迟 | 适用场景 |
|------|------|------|---------|
| 同步对话 | `POST /chat` | 1-5s | 简单问答、翻译 |
| SSE 流式 | `POST /chat/stream` | 流式输出 | 长文生成、代码生成 |
| 异步任务 | `POST /tasks` → Worker | 30-120s | 多步工具调用（核心） |

---

## 工具

| 工具 | 功能 | 安全措施 |
|------|------|---------|
| `get_current_time` | 返回 UTC 当前时间 | 无参数 |
| `calculate` | 安全数学表达式求值 | AST 白名单 + 禁用 `__builtins__` |
| `fetch_url` | HTTP GET 抓取网页 | 10s 超时，截断 2000 字符 |
| `run_command` | 在 `/app/data/` 下执行白名单命令 | 禁止绝对路径和 `..`、禁止管道重定向、命令白名单、10s 超时 |

工具定义在 `app/agent_core.py` 的 `TOOLS` 列表中，OpenAI 兼容 function calling 格式（DeepSeek 原生支持）。

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

## API 文档

请求/响应的完整 Schema 见 `FastAPI.openapi.json`（OpenAPI 3.0 规范）。可导入 Swagger Editor、Apifox、或任意 OpenAPI 查看器。

### `POST /tasks` — 提交异步任务（核心接口）

```bash
curl -X POST http://localhost:8000/tasks \
  -H "Content-Type: application/json" \
  -d '{
    "instruction": "用 run_command 读取 data/readme.txt 并总结内容",
    "context": "演示任务",
    "idempotent_key": "optional-key"
  }'
```

响应（立即返回）：

```json
{"status": "queued", "task_id": "a1b2c3d4-..."}
```

### `GET /tasks/{id}` — 查询任务状态与执行步骤

```bash
curl http://localhost:8000/tasks/a1b2c3d4-...
```

响应（任务完成后）：

```json
{
  "task_id": "a1b2c3d4-...",
  "status": "success",
  "result": "该项目的核心功能是...",
  "error": null,
  "steps": [
    {"seq": 1, "type": "think",       "tool_name": null,          "content": "我先读取文件..."},
    {"seq": 2, "type": "tool_call",   "tool_name": "run_command", "content": "{\"command\":\"cat data/readme.txt\"}"},
    {"seq": 3, "type": "tool_result", "tool_name": "run_command", "content": "{\"output\":\"# AI Task Backend...\"}"},
    {"seq": 4, "type": "think",       "tool_name": null,          "content": "文件读取成功，内容包含..."},
    {"seq": 5, "type": "reply",       "tool_name": null,          "content": "该项目的核心功能是..."}
  ],
  "created_at": "2026-06-27T10:30:00+00:00",
  "updated_at": "2026-06-27T10:30:12+00:00"
}
```

### 其他接口速查

| 接口 | 方法 | 说明 |
|------|------|------|
| `/` | GET | API 概览（返回所有可用端点） |
| `/health` | GET | 健康检查 |
| `/chat` | POST | 同步对话（持久化到 conversations/messages 表） |
| `/chat/stream` | POST | SSE 流式对话 |
| `/tasks` | POST | 提交异步任务 |
| `/tasks/{task_id}` | GET | 查询任务（含完整步骤链） |

---

## 全链路审计

### 通过 API 查询

```bash
# 查看单个任务的完整执行链（think → tool_call → tool_result → reply）
curl http://localhost:8000/tasks/{task_id}
```

### 通过数据库查询（直连容器内 PostgreSQL）

无需安装 psql，直接用 `docker exec` 进入容器：

```bash
# 连接数据库
docker exec -it ai-lol-postgres-1 psql -U dev -d ai_tasks

# 1. 所有会话
SELECT id, title, created_at FROM conversations ORDER BY created_at DESC;

# 2. 某个会话的全部消息
SELECT role, content FROM messages WHERE conversation_id = 'xxx' ORDER BY created_at;

# 3. 所有任务（含状态和失败原因）
SELECT id, status,
       CASE WHEN error IS NOT NULL THEN SUBSTRING(error,1,80) END AS error_snippet,
       created_at
FROM tasks ORDER BY created_at DESC;

# 4. 某任务的执行步骤（工具调用入参出参）
SELECT seq, type, tool_name, SUBSTRING(content,1,100)
FROM task_steps WHERE task_id = 'xxx' ORDER BY seq;
```

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
| `messages` | 对话消息 (user/assistant/tool) |
| `tasks` | 异步任务 (idempotent_key UNIQUE) |
| `task_steps` | Agent 执行步骤 (think/tool_call/tool_result/reply) |

迁移在 API 容器启动时自动执行：`alembic upgrade head && uvicorn ...`

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
| `DATABASE_URL` | — | `postgresql+asyncpg://dev:devpass123@postgres:5432/ai_tasks` | 本地开发用 `127.0.0.1` |
| `REDIS_URL` | — | `redis://redis:6379` | Redis 连接串 |
| `AMQP_URL` | — | `amqp://dev:devpass123@rabbitmq:5672/` | RabbitMQ 连接串 |

---

## 项目结构

```
hjh-ai-lol/
├── docker-compose.yml      # 5 服务编排
├── Dockerfile              # 多阶段构建 (builder + runtime)
├── pyproject.toml          # 依赖声明
├── .env.dev                # 环境变量（不提交）
├── .env.example            # 环境变量模板
├── FastAPI.openapi.json    # OpenAPI 3.0 规范（可导入 Swagger/Apifox）
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
└── data/                   # run_command 安全文件区
    ├── readme.txt          # 演示用文件
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
# 1. 启动
docker compose up -d
curl http://localhost:8000/health   # 确认就绪

# 2. 提交异步任务
curl -X POST http://localhost:8000/tasks \
  -H "Content-Type: application/json" \
  -d '{
    "instruction": "用 run_command 读取 data/readme.txt，总结项目核心功能",
    "context": ""
  }'
# → {"status":"queued","task_id":"xxx-..."}

# 3. 查询状态 + 步骤链
curl http://localhost:8000/tasks/xxx-...

# 4. 查看 Worker 实时日志
docker-compose logs -f worker

# 5. 幂等验证
curl -X POST http://localhost:8000/tasks \
  -H "Content-Type: application/json" \
  -d '{"instruction":"test","idempotent_key":"demo-001"}'
# 第二次同 key → {"status":"duplicate","task_id":"..."}

# 6. 数据库全链路审计
docker exec ai-lol-postgres-1 psql -U dev -d ai_tasks \
  -c "SELECT seq, type, tool_name, SUBSTRING(content,1,100) FROM task_steps WHERE task_id='xxx' ORDER BY seq;"

---

## OpenAPI 规范文件

`FastAPI.openapi.json` —— 当前 API 的完整 OpenAPI 3.0 规范，包含所有接口的请求/响应 Schema、字段类型和示例。

**使用方式：**
- 导入 [Swagger Editor](https://editor.swagger.io) 在线预览（粘贴或拖入文件）
- 导入 Apifox / Postman 自动生成请求集合
- 用 `openapi-generator` 生成客户端 SDK
- 贴给 ChatGPT / Claude 让 AI 理解接口结构

---

## 已知限制

1. **单 Worker**：当前 1 个消费者串行处理。高并发需多 Worker 实例 + `prefetch_count` 调优
2. **无认证**：API 无鉴权。生产需 JWT / API Key 中间件
3. **无速率限制**：可无限提交。生产需 slowapi 令牌桶
4. **LLM 单一 provider**：仅 DeepSeek。生产需抽象 `BaseLLMProvider` + fallback
5. **无监控**：无 Prometheus/Grafana。需补齐队列深度、延迟、失败率仪表盘
6. **Tool sandbox 有限**：`run_command` 白名单 + 路径限制。生产需 Firecracker/gVisor 沙箱