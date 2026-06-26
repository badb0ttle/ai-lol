# AI Task Backend
> 把一次 AI 请求做成可异步执行、可查询状态、可处理失败、可本地复现的后端系统。
---

## 项目目标

**把 AI 调用当作有状态任务来管理**
传统 `POST /chat → LLM → Response` 的问题：

任务做成RabbitMQ 持久化 + 手动 ACK 
每一步（Step + ToolCall）持久化到 PostgreSQL 
Worker 、 消息自动回队，幂等 key 防止重复执行 
Redis SET NX 前置 + PostgreSQL UNIQUE 后置 
`GET /tasks/{id}` 返回完整 steps → tool_calls 链路 
`docker compose up -d` 一条命令 

---
## 技术选型

| 组件 | 技术 | 理由 |
|------|------|------|
| **接入层** | FastAPI | 异步原生，`StreamingResponse` 支持 SSE 流式输出，Pydantic 自动校验，Swagger 文档自动生成 |
| **消息队列** | RabbitMQ | 持久化 + 手动 ACK + 原生死信队列，任务不能丢 |
| **消费端** | 独立 Worker 进程 | 生命周期与 API 解耦，故障隔离，可独立扩缩容 |
| **Agent 引擎** | 自实现 ReAct 循环 | 透明可控，每一步可记录可复盘 |
| **持久存储** | PostgreSQL + SQLAlchemy 2.0 async | 关系型数据天然适合关联查询；JSONB 存半结构化工具调用 |
| **迁移工具** | Alembic | `--autogenerate` 从模型自动生成迁移，版本化管理 |
| **缓存 + 幂等** | Redis | `SET NX` 原子去重，任务状态缓存减轻 DB 轮询压力 |
| **基础设施** | Docker Compose | 一条命令启动全部服务，环境完全一致 |
---

## 系统架构

```
┌──────────────────────────────────────────────────────────┐
│                    Docker Compose                        │
│                                                          │
│  ┌──────────┐   ┌──────────┐   ┌──────────┐            │
│  │PostgreSQL│   │  Redis   │   │ RabbitMQ │            │
│  │   :5432  │   │  :6379   │   │  :5672   │            │
│  │          │   │          │   │  :15672  │ (管理UI)   │
│  └────┬─────┘   └────┬─────┘   └────┬─────┘            │
│       │              │              │                   │
│  ┌────┴──────────────┴──────────────┴─────┐            │
│  │              FastAPI :8000              │            │
│  │  POST /chat          (同步对话)         │            │
│  │  POST /chat/stream   (SSE 流式)        │            │
│  │  POST /tasks         (提交异步任务)     │            │
│  │  GET  /tasks/{id}    (查询任务状态)     │            │
│  │  GET  /sessions/{id} (会话历史)         │            │
│  └────────────────┬───────────────────────┘            │
│                   │ 发布消息                             │
│  ┌────────────────▼───────────────────────┐            │
│  │           Worker (独立进程)             │            │
│  │  ┌─────────────────────────────────┐   │            │
│  │  │         Agent (ReAct 循环)       │   │            │
│  │  │  调 LLM → 解析 tool_call        │   │            │
│  │  │  → 执行工具 → 记录 → 循环       │   │            │
│  │  └─────────────────────────────────┘   │            │
│  │  ┌─────────────────────────────────┐   │            │
│  │  │  工具层                          │   │            │
│  │  │  file_reader / web_fetcher       │   │            │
│  │  │  calculator / system_info        │   │            │
│  │  └─────────────────────────────────┘   │            │
│  └────────────────────────────────────────┘            │
└──────────────────────────────────────────────────────────┘
```

**三条请求路线**：

| 路线 | 接口 | 延迟 | 失败处理 | 适用场景 |
|------|------|------|---------|---------|
| 同步对话 | `POST /chat` | 1-5s | 返回错误，用户重发 | 简单问答、翻译 |
| SSE 流式 | `POST /chat/stream` | 流式输出 | `CancelledError` 释放资源 | 长文生成、代码生成 |
| 异步任务 | `POST /tasks → Worker` | 30-120s | 全链路持久化，可复盘 | 多步工具调用、批量任务 |

---

## 启动方式

```bash
# 1. 克隆项目
git clone <repo-url> && cd hjh-ai-lol

# 2. 配置环境变量
cp .env.example .env.dev
# 编辑 .env，填入 LLM API Key

# 3. 一条命令启动
docker compose up -d

# 4. 运行数据库迁移
docker compose exec api alembic upgrade head

# 5. 验证
curl http://localhost:8000/health
# → {"status": "ok", "timestamp": "..."}
```

**服务端口**：

| 服务 | 端口 | 用途 |
|------|------|------|
| FastAPI | 8000 | API 服务 |
| PostgreSQL | 5432 | 数据库 |
| Redis | 6379 | 缓存 |
| RabbitMQ | 5672 | AMQP 消息 |
| RabbitMQ 管理界面 | 15672 | 查看队列/消息/确认率 |

---

## 环境变量

| 变量 | 必填 | 默认值 | 说明 |
|------|------|--------|------|
| `LLM_API_KEY` | ✅ | — | DeepSeek API 的密钥（[获取地址](https://platform.deepseek.com/api_keys)） |
| `LLM_BASE_URL` | ❌ | `https://api.deepseek.com` | LLM API 地址（DeepSeek 兼容 OpenAI 接口格式） |
| `LLM_MODEL` | ❌ | `deepseek-chat` | 模型名称（`deepseek-chat`=V3，`deepseek-reasoner`=R1） |
| `DATABASE_URL` | ❌ | `postgresql+asyncpg://dev:devpass123@postgres:5432/ai_tasks` | PostgreSQL 异步连接串 |
| `REDIS_URL` | ❌ | `redis://redis:6379/0` | Redis 连接串 |
| `AMQP_URL` | ❌ | `amqp://dev:***@rabbitmq:5672/` | RabbitMQ 连接串 |
| `AMQP_URL` | ❌ | `amqp://dev:devpass123@rabbitmq:5672/` | RabbitMQ 连接串 |
| `MAX_LOOPS` | ❌ | `10` | Agent 最大工具调用轮数 |
| `TASK_TIMEOUT` | ❌ | `120` | 单任务总超时（秒） |
| `LLM_TIMEOUT` | ❌ | `60` | 单次 LLM 调用超时（秒） |
| `MAX_RETRIES` | ❌ | `3` | 任务整体最大重试次数 |

---

## API 使用示例

### 1. 普通对话

```bash
curl -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "你好，请用三句话介绍你自己"}'
```

响应：

```json
{
  "session_id": "a1b2c3d4",
  "message": "你好！我是一个 AI 助手..."
}
```

### 2. SSE 流式对话

```bash
curl -N -X POST http://localhost:8000/chat/stream \
  -H "Content-Type: application/json" \
  -d '{"message": "写一首关于秋天的五言诗"}'
```

响应（逐块推送）：

```
event: start
data: {"session_id":"a1b2c3d4"}

event: chunk
data: {"content":"秋风"}

event: chunk
data: {"content":"送爽"}

event: done
data: {}
```

### 3. 提交异步任务（带工具调用）

```bash
curl -X POST http://localhost:8000/tasks \
  -H "Content-Type: application/json" \
  -d '{
    "message": "读取 data/meeting.txt 的内容，并总结要点",
    "tools": ["file_reader"],
    "idempotent_key": "my-unique-key-001"
  }'
```

响应（立即返回，任务在后台执行）：

```json
{
  "task_id": "e5f6g7h8",
  "session_id": "a1b2c3d4",
  "status": "pending"
}
```

### 4. 查询任务状态

```bash
curl http://localhost:8000/tasks/e5f6g7h8
```

响应（任务完成后）：

```json
{
  "task_id": "e5f6g7h8",
  "session_id": "a1b2c3d4",
  "status": "success",
  "input_message": "读取 data/meeting.txt 的内容，并总结要点",
  "output_message": "会议主要讨论了以下三个议题：1. ...",
  "retry_count": 0,
  "steps": [
    {
      "sequence": 1,
      "type": "llm_call",
      "status": "success",
      "input": {"messages": [...]},
      "output": {"tool_calls": [{"function": {"name": "file_reader", "arguments": "{\"path\":\"meeting.txt\"}"}}]},
      "tool_calls": []
    },
    {
      "sequence": 2,
      "type": "tool_call",
      "status": "success",
      "tool_calls": [
        {
          "tool_name": "file_reader",
          "input_params": {"path": "meeting.txt"},
          "output_result": {"content": "...(文件内容)..."},
          "status": "success"
        }
      ]
    },
    {
      "sequence": 3,
      "type": "llm_call",
      "status": "success",
      "input": {"messages": [...]},
      "output": {"content": "会议主要讨论了..."},
      "tool_calls": []
    }
  ],
  "created_at": "2026-06-25T10:30:00Z",
  "started_at": "2026-06-25T10:30:01Z",
  "completed_at": "2026-06-25T10:30:12Z"
}
```

### 5. 重复提交（幂等保护）

```bash
# 第一次提交
curl -X POST http://localhost:8000/tasks \
  -H "Content-Type: application/json" \
  -d '{"message": "读取 data/meeting.txt", "idempotent_key": "key-001"}'
# → 201 {"task_id": "xxx", "status": "pending"}

# 立即再次提交（相同的 idempotent_key）
curl -X POST http://localhost:8000/tasks \
  -H "Content-Type: application/json" \
  -d '{"message": "读取 data/meeting.txt", "idempotent_key": "key-001"}'
# → 200 {"task_id": "xxx", "status": "duplicate"}  ← 返回已有的 task_id，不创建新任务
```

### 6. 查询会话历史

```bash
curl http://localhost:8000/sessions/a1b2c3d4
curl http://localhost:8000/sessions/a1b2c3d4/messages
```

---

## 数据库表设计

**ER 关系**：

```
sessions 1──N messages     （一条会话包含多条消息）
sessions 1──N tasks         （一条会话可以触发多个异步任务）
tasks    1──N steps         （一个任务包含多个执行步骤）
steps    1──N tool_calls    （一个步骤可以包含多个工具调用）
```

### 表结构

```sql
-- 会话
sessions (
    id          VARCHAR(64) PRIMARY KEY,
    title       VARCHAR(255),
    metadata    JSONB DEFAULT '{}',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 消息（所有路线共用）
messages (
    id           BIGSERIAL PRIMARY KEY,
    session_id   VARCHAR(64) NOT NULL REFERENCES sessions(id),
    role         VARCHAR(20) NOT NULL,      -- user / assistant / tool
    content      TEXT,
    tool_calls   JSONB,                     -- OpenAI 兼容 tool_calls 格式（DeepSeek 原生支持）
    tool_call_id VARCHAR(64),               -- 关联 tool 消息
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_messages_session ON messages(session_id, created_at);

-- 异步任务
tasks (
    id              VARCHAR(64) PRIMARY KEY,
    session_id      VARCHAR(64) NOT NULL REFERENCES sessions(id),
    idempotent_key  VARCHAR(128) UNIQUE NOT NULL,   -- 幂等去重
    status          VARCHAR(20) NOT NULL DEFAULT 'pending',  -- pending/running/success/failed
    input_message   TEXT NOT NULL,
    output_message  TEXT,
    error_type      VARCHAR(100),
    error_message   TEXT,
    retry_count     INT NOT NULL DEFAULT 0,
    max_retries     INT NOT NULL DEFAULT 3,
    max_loops       INT NOT NULL DEFAULT 10,
    timeout_seconds INT NOT NULL DEFAULT 120,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at      TIMESTAMPTZ,
    completed_at    TIMESTAMPTZ
);
CREATE INDEX idx_tasks_status ON tasks(status, created_at);
CREATE INDEX idx_tasks_session ON tasks(session_id);

-- 执行步骤
steps (
    id           BIGSERIAL PRIMARY KEY,
    task_id      VARCHAR(64) NOT NULL REFERENCES tasks(id),
    sequence     INT NOT NULL,              -- 第几步（1-based）
    type         VARCHAR(20) NOT NULL,       -- llm_call / tool_call
    input        JSONB,                      -- LLM: messages / Tool: params
    output       JSONB,                      -- LLM: response / Tool: result
    model        VARCHAR(100),               -- 使用的 LLM 模型
    status       VARCHAR(20) NOT NULL DEFAULT 'pending',
    error_type   VARCHAR(100),
    error_message TEXT,
    started_at   TIMESTAMPTZ,
    completed_at TIMESTAMPTZ
);
CREATE UNIQUE INDEX idx_steps_task_seq ON steps(task_id, sequence);

-- 工具调用记录
tool_calls (
    id             BIGSERIAL PRIMARY KEY,
    step_id        BIGINT NOT NULL REFERENCES steps(id),
    tool_name      VARCHAR(100) NOT NULL,
    input_params   JSONB NOT NULL,
    output_result  JSONB,
    status         VARCHAR(20) NOT NULL DEFAULT 'pending',
    error_type     VARCHAR(100),
    error_message  TEXT,
    started_at     TIMESTAMPTZ,
    completed_at   TIMESTAMPTZ
);
CREATE INDEX idx_tool_calls_step ON tool_calls(step_id);
```

### 复盘查询

```sql
-- 查看一个任务从头到尾发生了什么
SELECT
    t.status AS task_status,
    t.error_message AS task_error,
    s.sequence,
    s.type AS step_type,
    s.status AS step_status,
    s.error_message AS step_error,
    tc.tool_name,
    tc.status AS tool_status,
    tc.error_message AS tool_error,
    tc.input_params,
    tc.output_result
FROM tasks t
LEFT JOIN steps s ON s.task_id = t.id
LEFT JOIN tool_calls tc ON tc.step_id = s.id
WHERE t.id = 'your-task-id'
ORDER BY s.sequence, tc.id;
```

---

## Redis 状态设计

| Key Pattern | 类型 | TTL | 用途 | 写入时机 |
|---|---|---|---|---|
| `task:{task_id}:status` | String | 10s | 缓存任务状态，避免轮询打 DB | Worker 每次更新状态时 SET |
| `idempotent:{key}` | String | 24h | 幂等 key 去重 | POST `/tasks` 时 SET NX |

### 幂等去重流程

```
POST /tasks (idempotent_key = "abc")
  │
  ├─ 1. Redis SET NX "idempotent:abc" "task-uuid" EX 86400
  │     ├─ 成功 → 继续步骤 2
  │     └─ 失败 → key 已存在 → GET 取 task_id → 返回已存在的任务
  │
  ├─ 2. PostgreSQL INSERT task (idempotent_key = "abc")
  │     ├─ 成功 → 继续步骤 3
  │     └─ UNIQUE 冲突 → Redis 漏了但 DB 兜底 → 查 DB 返回已有 task_id
  │
  └─ 3. 发布 RabbitMQ 消息
```

**为什么两层？** Redis 是前置快速检查（"这个请求我见过吗？"）。PostgreSQL UNIQUE 是最终防线（Redis 重启丢数据时兜底）。

### 状态查询流程

```
GET /tasks/{id}
  │
  ├─ 1. Redis GET "task:{id}:status"
  │     ├─ 命中 → 返回（10s 内刚被 Worker 更新过）
  │     └─ 未命中 → 继续步骤 2
  │
  ├─ 2. PostgreSQL SELECT task + steps + tool_calls
  │     └─ 组装完整响应
  │
  └─ 3. 回写 Redis SET "task:{id}:status" EX 10
        （下次轮询直接命中缓存）
```

Redis 不是权威数据源。Redis 不可用时，降级为直接查 PostgreSQL，功能不受影响。

---

## RabbitMQ 队列和消费设计

### 拓扑

```
Exchange: ai_task_exchange (type=direct, durable=True)
    │
    ├── Queue: task_queue (durable=True)
    │   ├── routing_key: "task.execute"
    │   ├── x-dead-letter-exchange: ai_task_dlx
    │   └── x-message-ttl: 300000  (5 分钟，可选)
    │
    └── Queue: task_dlq (durable=True)
        └── routing_key: "task.dead"
```

### 消息体

```json
{
    "task_id": "e5f6g7h8",
    "session_id": "a1b2c3d4",
    "messages": [
        {"role": "user", "content": "读取 data/meeting.txt 并总结"}
    ],
    "tools": ["file_reader"],
    "idempotent_key": "my-unique-key-001"
}
```

### 发布端（API）

```python
# POST /tasks 中
task = await create_task_in_db(session_id, message, idempotent_key)
await channel.default_exchange.publish(
    aio_pika.Message(
        body=json.dumps(payload).encode(),
        delivery_mode=aio_pika.DeliveryMode.PERSISTENT,  # 持久化到磁盘
    ),
    routing_key="task.execute",
)
# 返回 task_id，HTTP 请求结束
```

### 消费端（Worker）

```python
async def on_message(message: aio_pika.IncomingMessage):
    async with message.process():
        data = json.loads(message.body)

        # 1. 幂等检查
        task = await get_task(data["idempotent_key"])
        if task.status in ("success", "running"):
            return  # ACK，跳过

        # 2. 执行 Agent
        try:
            await agent.run(task)
        except MaxRetriesExceeded:
            await message.reject(requeue=False)  # 进 DLQ
        except TransientError:
            await message.reject(requeue=True)   # 回队重试
        except Exception as e:
            await save_error(task, e)
            if task.retry_count < task.max_retries:
                await message.reject(requeue=True)
                await increment_retry(task)
            else:
                await message.reject(requeue=False)
```

### 消息可靠性保证

| 保证 | 机制 |
|------|------|
| 消息不丢失 | `delivery_mode=PERSISTENT` + durable queue |
| Worker 崩溃不丢任务 | 手动 ACK，未 ACK 的消息自动回队 |
| 不重复执行 | 消费时检查 DB 状态，已处理的跳过 |
| 失败可追溯 | 进 DLQ 的消息保留，人工检查 |

---

## Agent / Workflow 执行流程

使用 **ReAct（Reasoning + Acting）** 模式：

```
┌─────────────────────────────────┐
│     Agent.run(session, tools)   │
└───────────────┬─────────────────┘
                │
   ┌────────────▼────────────┐
   │ 构建消息上下文            │
   │ system_prompt            │
   │ + 会话历史                │
   │ + 工具定义 (function_call)│
   │ + 当前用户消息            │
   └────────────┬────────────┘
                │
   ┌────────────▼────────────┐
   │   Agent Loop (最多 10 轮)│
   │                          │
   │  ┌─────────────────────┐ │
   │  │ Step N: 调用 LLM    │ │
   │  │ → 记录 Step(llm_call)│ │
   │  │ → 记录 DB            │ │
   │  └─────────┬───────────┘ │
   │            │              │
   │    ┌───────▼────────┐    │
   │    │ 有 tool_calls? │    │
   │    └───┬────────┬───┘    │
   │     NO │        │ YES    │
   │        │        │        │
   │        │  ┌─────▼──────┐ │
   │        │  │ 执行工具    │ │
   │        │  │ → 记录      │ │
   │        │  │   ToolCall  │ │
   │        │  │ → 结果追加  │ │
   │        │  │   到消息列表│ │
   │        │  │ → 回到 LLM  │ │
   │        │  └─────────────┘ │
   │        │                  │
   │  ┌─────▼──────┐          │
   │  │ 最终回复    │          │
   │  │ Task→success│          │
   │  └─────────────┘          │
   └────────────────────────────┘
```

### 每步都记录到数据库

```python
# 记录 LLM 调用
step = Step(task_id=task.id, sequence=n, type="llm_call",
            input={"messages": messages}, model=model)
step.start()
try:
    response = await llm.chat(messages, tools)
    step.output = response
    step.complete()
except Exception as e:
    step.fail(error=e)

# 记录工具调用
tool_call = ToolCall(step_id=step.id, tool_name=name,
                     input_params=params)
tool_call.start()
try:
    result = await execute_tool(name, params)
    tool_call.output_result = result
    tool_call.complete()
except Exception as e:
    tool_call.fail(error=e)
```

### 工具调用失败时，Agent 不直接中止

工具失败 → 转为 tool message 喂回 LLM：
```json
{
    "role": "tool",
    "tool_call_id": "call_xxx",
    "content": "错误: 文件不存在 (path=data/nonexistent.txt)"
}
```

LLM 看到这个错误后，可以决定：重试 / 换参数 / 换工具 / 告诉用户做不到。**Agent 的"智能"体现在它能自己处理工具层的失败，而不是把异常直接抛给用户。**

---

## 工具注册和调用设计

### 已实现的工具

| 工具 | 功能 | 安全边界 |
|------|------|---------|
| `file_reader` | 读取 `./data/` 目录下的文件 | `normpath` 防止 `../` 穿越，限定 `./data/` 子树 |
| `web_fetcher` | HTTP GET 提取网页文本 | 内网 IP 过滤（防 SSRF），内容截断 5000 字符 |
| `calculator` | 安全算术表达式求值 | `ast.literal_eval` + 禁用 `__builtins__` |
| `system_info` | 容器 CPU/内存/磁盘使用率 | 只读，不接任何参数，最小权限 |

### 工具定义格式（OpenAI 兼容 function calling，DeepSeek 原生支持）

```python
TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "file_reader",
            "description": "读取本地文件内容。路径必须在 ./data/ 目录下",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "相对于 ./data/ 的文件路径，如 'meeting.txt'"
                    }
                },
                "required": ["path"]
            }
        }
    },
    # ... 其他工具
]
```

### 工具执行

```python
# tools/registry.py

TOOL_MAP = {
    "file_reader": file_reader.execute,
    "web_fetcher": web_fetcher.execute,
    "calculator": calculator.execute,
    "system_info": system_info.execute,
}

async def execute_tool(name: str, params: dict) -> dict:
    """
    统一入口，返回 {"success": bool, "result": ..., "error": str | None}
    调用方负责记录 ToolCall 到数据库
    """
    tool_fn = TOOL_MAP.get(name)
    if not tool_fn:
        return {"success": False, "error": f"未知工具: {name}"}
    try:
        result = await tool_fn(**params)
        return {"success": True, "result": result}
    except Exception as e:
        return {"success": False, "error": str(e)}
```

### 如何添加新工具

1. 在 `tools/` 下新建文件，实现 `async def execute(**params) -> dict`
2. 在 `tools/registry.py` 的 `TOOL_MAP` 中注册
3. 在 `tools/registry.py` 的 `TOOL_DEFINITIONS` 中添加 OpenAI 格式定义
4. 重启 Worker

工具层不依赖 FastAPI、不依赖数据库、不依赖 RabbitMQ。**纯函数，可独立测试。**

---

## 幂等、重试、超时和失败降级策略

### 幂等

| 场景 | 机制 | 层级 |
|------|------|------|
| 用户重复提交 | Redis `SET NX` + PostgreSQL `UNIQUE(idempotent_key)` | HTTP 层 |
| Worker 重复消费 | 消费开始前 SELECT 检查任务状态 | 消费层 |

幂等 key 生成规则：
- 客户端提供 `idempotent_key` header → 使用客户端值
- 未提供 → 自动生成 `md5(session_id + message + sorted(tools))`

### 重试

| 层级 | 机制 | 最大次数 | 失败后 |
|------|------|---------|--------|
| 工具调用 | Worker 内重试同一工具 | 2 | 转为 tool error 消息喂回 LLM |
| 任务整体 | RabbitMQ `reject(requeue=True)` | 3 (`MAX_RETRIES`) | 拒绝不重投 → 进 DLQ |
| HTTP 幂等 | 返回已有 task_id | — | 用户拿到已有结果 |

```python
# 工具重试
for attempt in range(2):
    result = await execute_tool(name, params)
    if result["success"]:
        break
    if attempt == 1:  # 最后一次也失败
        return tool_error_message(result["error"])
```

### 超时

| 层级 | 超时时间 | 机制 | 超时后 |
|------|---------|------|--------|
| 单次 LLM 调用 | 60s (`LLM_TIMEOUT`) | `httpx.Timeout(60)` | Step → failed，Task → failed |
| 任务整体 | 120s (`TASK_TIMEOUT`) | `asyncio.wait_for(agent.run(), timeout)` | Task → failed，保存已完成步骤 |
| SSE 流 | 无全局超时 | `request.is_disconnected()` 逐 chunk 检查 | 客户端断开时释放资源 |

### 失败降级

```
失败类型                  系统行为
─────────────────────────────────────────────
LLM 超时              → 记录 Step 失败 + 原始错误
                         Task → failed
                         保留所有已完成步骤供复盘

LLM 返回格式异常       → 记录 Step(output=raw_response)
                         Task → failed
                         原始返回保存在 DB 中，人工查看

工具调用失败           → 转为 tool error 消息喂回 LLM
                         LLM 决定：重试/换参数/换工具/告知用户

工具重试 2 次全失败     → LLM 收到 "工具 X 重试 2 次均失败: {error}"
                         LLM 可以告知用户或尝试替代方案

max_loops 耗尽         → Task → failed
                         error_message = "达到最大循环次数 (10)"

Worker 进程崩溃         → RabbitMQ 消息自动回队
                         下次消费时幂等检查跳过

Redis 不可用            → 降级到 PostgreSQL 直接查询
                         功能可用，速度降低

PostgreSQL 不可用       → 系统不可用（唯一权威数据源）
                         但 RabbitMQ 中的消息保留，恢复后可继续
```

---

## 流式输出实现方式

使用 **SSE（Server-Sent Events）**，HTTP 协议原生支持。

```python
# app/api/chat.py

from fastapi.responses import StreamingResponse

async def event_stream(request, session_id, message):
    try:
        # 1. 通知客户端开始
        yield format_sse("start", {"session_id": session_id})

        # 2. 流式调用 LLM
        async for chunk in llm_client.stream_chat(messages):
            # 客户端断开时停止生成（节省 token）
            if await request.is_disconnected():
                break
            yield format_sse("chunk", {"content": chunk})

        # 3. 通知客户端完成
        yield format_sse("done", {})

    except asyncio.CancelledError:
        # 服务端主动取消（如客户端断开），释放资源
        yield format_sse("error", {"message": "连接已断开"})
        raise   # 必须 re-raise，否则 uvicorn 不释放资源


def format_sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@router.post("/chat/stream")
async def chat_stream(request: Request, body: ChatRequest):
    return StreamingResponse(
        event_stream(request, body.session_id, body.message),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",   # 禁用 nginx 缓冲
        },
    )
```

**关键设计点**：

1. **`request.is_disconnected()`** — 每个 chunk 前检查。客户端断开了还继续生成就是浪费 API 费用
2. **`CancelledError` 必须 re-raise** — 否则 uvicorn 不知道请求已结束，连接和资源不会释放
3. **`X-Accel-Buffering: no`** — 如果前面有 nginx 反向代理，禁用缓冲否则 chunk 会被攒成一块发出
4. **不需要 WebSocket** — SSE 是单向流，语义恰好匹配 LLM streaming

---

## Docker Compose 启动方式

### docker-compose.yml 服务清单

| 服务 | 镜像 | 端口 | 环境变量注入 |
|------|------|------|------------|
| `postgres` | `postgres:16-alpine` | 5432 | `POSTGRES_DB/USER/PASSWORD` |
| `redis` | `redis:7-alpine` | 6379 | — |
| `rabbitmq` | `rabbitmq:4-management-alpine` | 5672, 15672 | `RABBITMQ_DEFAULT_USER/PASS` |
| `api` | 本地构建 (Dockerfile) | 8000 | 全部通过 `.env` 文件 |
| `worker` | 本地构建 (Dockerfile) | — | 全部通过 `.env` 文件 |

### Dockerfile（API 和 Worker 共用，Python 3.13 + pyproject.toml）

```dockerfile
FROM python:3.13-slim
WORKDIR /app
COPY pyproject.toml .
RUN pip install --no-cache-dir .
COPY . .
```

### 启动命令

```bash
# 启动所有服务（后台运行）
docker compose up -d

# 查看日志
docker compose logs -f api
docker compose logs -f worker

# 运行数据库迁移
docker compose exec api alembic upgrade head

# 重启单个服务
docker compose restart worker

# 停止
docker compose down

# 停止并删除数据卷（重置数据库）
docker compose down -v
```

### 开发时的便利工具

```bash
# RabbitMQ 管理界面
open http://localhost:15672   # 账号: dev / devpass123

# 直接连接 PostgreSQL
docker compose exec postgres psql -U dev -d ai_tasks

# 直接连接 Redis
docker compose exec redis redis-cli
```

---

## 演示验收流程

### 1. 普通回复

```bash
curl -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "1+1等于几？"}'
```

### 2. 流式回复

```bash
curl -N -X POST http://localhost:8000/chat/stream \
  -H "Content-Type: application/json" \
  -d '{"message": "用中文写一段100字的自我介绍"}'
```

### 3. 成功工具调用

```bash
# 先创建测试文件
echo "今天的会议讨论了三个议题：1. Q3预算 2. 新员工培训 3. 产品路线图" > data/meeting.txt

# 提交带工具的任务
curl -X POST http://localhost:8000/tasks \
  -H "Content-Type: application/json" \
  -d '{"message": "读取 data/meeting.txt，用列表格式列出会议议题", "tools": ["file_reader"]}'
# → {"task_id": "xxx", "status": "pending"}

# 等待几秒后查询
curl http://localhost:8000/tasks/xxx
# → status: "success", steps 中有 file_reader 的 tool_call
```

### 4. 失败工具调用

```bash
curl -X POST http://localhost:8000/tasks \
  -H "Content-Type: application/json" \
  -d '{"message": "读取 data/nonexistent.txt", "tools": ["file_reader"]}'
# 查询后 → LLM 回复 "文件不存在"
```

### 5. 任务投递 → Worker 消费 → 状态查询

```bash
# 提交
TASK=$(curl -s -X POST http://localhost:8000/tasks \
  -H "Content-Type: application/json" \
  -d '{"message": "服务器状态怎么样？", "tools": ["system_info"]}' | jq -r .task_id)

# 轮询
watch -n 1 "curl -s http://localhost:8000/tasks/$TASK | jq .status"
```

### 6. 重复提交保护

```bash
# 同样参数发两次
curl -X POST http://localhost:8000/tasks -H "Content-Type: application/json" \
  -d '{"message": "test", "idempotent_key": "demo-key-001"}'
# → 201

curl -X POST http://localhost:8000/tasks -H "Content-Type: application/json" \
  -d '{"message": "test", "idempotent_key": "demo-key-001"}'
# → 200 {"status": "duplicate"}  不会创建第二个任务
```

### 7. 查看执行痕迹

```bash
curl http://localhost:8000/tasks/$TASK | jq .
# 查看 steps → tool_calls 的完整链路
```

---

## 已知限制和失败 case

### 已知限制

1. **单 Worker 单消费者**：当前 Worker 进程只启动一个消费者。高并发时多个任务串行执行。生产环境应启动多个 Worker 实例或增加 consumer 并发数
2. **无认证和授权**：API 完全开放，无 API Key 校验。生产需要 JWT 或 API Key 中间件
3. **无速率限制**：恶意用户可无限提交任务。生产需 slowapi 或令牌桶限流
4. **工具安全边界有限**：`web_fetcher` 的 SSRF 防护基于 IP 黑名单，不是网络层隔离。真正的生产环境需要用 gVisor/Firecracker 沙箱执行每个工具
5. **LLM 单一 provider**：当前使用 DeepSeek API（OpenAI 兼容格式），不支持多 provider 切换或 fallback。生产需要抽象 `BaseLLMProvider`
6. **无监控和告警**：没有 Prometheus 指标，没有 Grafana 仪表盘。队列深度、任务延迟、失败率只能手动查看
7. **Redis 单点**：Redis 不可用会降级到 DB 查询（变慢），但没有 Redis Sentinel/Cluster 高可用
8. **消息无优先级**：所有任务共用一个队列。紧急任务和生产批量任务无法区分优先级
9. **`script_runner` 工具未包含**：代码执行需要沙箱隔离，当前 Docker 网络栈共享不满足安全要求。生产环境应使用 gVisor 或独立容器执行
10. **幂等 key 基于内容哈希**：同一用户发相同消息会被去重（即使用户意图是重新执行）。生产可引入客户端提供的 `Idempotency-Key` header，或在 key 中加入时间窗口

### 需要留意的失败 case

| case | 现象 | 排查方法 |
|------|------|---------|
| LLM API 欠费 | 所有 LLM 调用返回 401/429 | 查看 Worker 日志 + tasks 表的 error_message |
| RabbitMQ 磁盘满 | 消息发布阻塞，API 返回 500 | RabbitMQ 管理界面 → 磁盘水位告警 |
| PostgreSQL 连接池耗尽 | API 请求排队/超时 | `SELECT count(*) FROM pg_stat_activity` |
| Worker OOM（工具读到超大文件） | Worker 容器重启，任务失败 | Docker logs + tasks 表 error_message |
| 迁移未执行 | API 启动成功但数据库缺表 | `alembic current` 检查 |
| 时区不一致 | `created_at` 时间不对 | 所有容器统一 UTC |

### 生产环境中下一步补齐

| 补齐项 | 优先级 | 方案 |
|--------|--------|------|
| API 认证 | 🔴 高 | JWT middleware + API Key |
| 监控告警 | 🔴 高 | Prometheus + Grafana（队列深度、任务延迟、错误率） |
| Worker 水平扩展 | 🟡 中 | 多 Worker 实例 + prefetch_count 调优 |
| LLM provider 抽象 | 🟡 中 | `BaseLLMProvider` + 多 provider fallback |
| 工具沙箱 | 🟡 中 | gVisor/Firecracker 隔离执行 |
| 消息优先级 | 🟢 低 | 3 层队列（high/normal/low） |
| Redis 高可用 | 🟢 低 | Sentinel 或 Cluster |
| 日志聚合 | 🟢 低 | ELK 或 Loki + 结构化 JSON 日志 |

---

## 项目结构

```
hjh-ai-lol/
├── docker-compose.yml
├── Dockerfile
├── pyproject.toml
├── .env.example
├── README.md
│
├── alembic/
│   ├── env.py
│   ├── script.py.mako
│   └── versions/
│       └── 001_initial_schema.py
│
├── app/
│   ├── __init__.py
│   ├── main.py              # FastAPI app 入口
│   ├── config.py            # 配置 (pydantic-settings)
│   │
│   ├── api/
│   │   ├── chat.py          # /chat, /chat/stream
│   │   ├── tasks.py         # POST /tasks, GET /tasks/{id}
│   │   └── sessions.py      # GET /sessions/{id}
│   │
│   ├── models/
│   │   ├── db.py            # 异步引擎 + sessionmaker
│   │   ├── session.py       # Session, Message
│   │   ├── task.py          # Task, Step, ToolCall
│   │   └── schemas.py       # Pydantic request/response
│   │
│   ├── services/
│   │   ├── llm.py           # LLM 客户端 (httpx)
│   │   ├── agent.py         # Agent ReAct 循环
│   │   └── dedup.py         # 幂等 key 生成和检查
│   │
│   ├── worker/
│   │   └── consumer.py      # RabbitMQ 消费者
│   │
│   ├── broker/
│   │   └── rabbitmq.py      # RabbitMQ 连接管理
│   │
│   └── redis_client.py      # Redis 连接 + 辅助函数
│
├── tools/
│   ├── file_reader.py
│   ├── web_fetcher.py
│   ├── calculator.py
│   ├── system_info.py
│   └── registry.py          # 工具注册表
│
├── data/                    # 工具可访问的文件目录
│   └── .gitkeep
│
└── tests/
    ├── test_chat.py
    ├── test_tasks.py
    ├── test_tools.py
    └── test_worker.py
```

---

## 学习路径

本项目是 **7 天全栈学习计划** 的实战交付物，每天学习一个核心技术模块并动手验证：

| 天数 | 模块 | 掌握要点 | 学习笔记 |
|------|------|---------|---------|
| Day 1 | FastAPI + SSE | 异步路由、`StreamingResponse`、`CancelledError` 释放资源、Pydantic v2 校验 | [fastapi.md](https://blog.hjhai.xyz/Backend/fastapi.html) |
| Day 2 | RabbitMQ + aio-pika | 持久化消息、手动 ACK、死信队列、连接池管理 | [RabbitMQ.md](https://blog.hjhai.xyz/Backend/RabbitMQ.html) |
| Day 3 | PostgreSQL + SQLAlchemy 2.0 async | 异步引擎、sessionmaker、Alembic 迁移、JSONB 查询 | [SQLAlchemy异步操作.md](https://blog.hjhai.xyz/Backend/SQLAlchemy%E5%BC%82%E6%AD%A5%E6%93%8D%E4%BD%9C.html) |
| Day 4 | Redis | `SET NX` 幂等去重、状态缓存、降级策略 | [Redis.md](https://blog.hjhai.xyz/Backend/Redis.html) |
| Day 5 | Agent ReAct 循环 | 工具注册/调用/错误回喂、循环终止条件、全链路记录 | [Agent.md](https://blog.hjhai.xyz/Backend/Agent.html) |
| Day 6 | Docker Compose | 多阶段构建、5 服务编排、健康检查、一键启动 | [Docker.md](https://blog.hjhai.xyz/Backend/Docker.html) |
| Day 7 | — | **整合联调，交付 `docker compose up` 一键启动** | — |

> 每天的学习笔记都包含可运行的 Python 验证脚本（`learn/dayN/`），边学边写代码验证，而非只看文档。笔记记录踩坑过程和错误原因，方便复盘。

---

## License

MIT
