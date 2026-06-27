from fastapi import FastAPI,APIRouter,Request,HTTPException
from fastapi.responses import StreamingResponse
from contextlib import asynccontextmanager
from pydantic import BaseModel
import json
import asyncio
from datetime import datetime
import redis.asyncio as aioredis

from app.db import async_session, engine, Base
from app.models import Conversation, Message, MessageRole, Task
import httpx
from app.config import settings
import aio_pika
from uuid import uuid4
from sqlalchemy import select as sa_select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload


# 生命周期管理
@asynccontextmanager
async def lifespan(app: FastAPI):
    # 创建引擎、建表、连接 RabbitMQ
    # 1. 建表
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    # 2. 连接 Redis（编号顺延）
    redis = aioredis.from_url(settings.redis_url, decode_responses=True)
    app.state.redis = redis
    # 3. 连接 RabbitMQ
    rmq_conn = await aio_pika.connect_robust(settings.amqp_url)
    rmq_channel = await rmq_conn.channel()
    await rmq_channel.declare_queue("ai_tasks", durable=True)

    # 4. 挂到 app.state 上，端点里用
    app.state.rmq_channel = rmq_channel
    app.state.rmq_conn = rmq_conn
    yield
    # 断开连接
    await rmq_conn.close()
    await redis.aclose()
    await engine.dispose()


app = FastAPI(lifespan=lifespan)


# 请求模型
class ChatRequest(BaseModel):
    model: str = settings.llm_model
    messages: list[dict]  # 对话历史
    conversation_id: str | None = None  # 续接已有会话


class TaskRequest(BaseModel):
    instruction: str  # 任务指令
    context: str = ""  # 补充上下文
    idempotent_key: str | None = None


# 路由
router = APIRouter()
app.include_router(router)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/")
async def root():
    return {
        "service": "AI Task Backend",
        "endpoints": {
            "health": "GET /health",
            "chat": "POST /chat",
            "chat_stream": "POST /chat/stream",
            "submit_task": "POST /tasks",
            "get_task": "GET /tasks/{task_id}",
        },
        "docs": "FastAPI.openapi.json (OpenAPI 3.0 规范)",
    }


@app.post("/chat")
async def chat(req: ChatRequest):
    async with async_session() as db:
        # 会话管理：传了 conversation_id 就续接，否则新建
        if req.conversation_id:
            conv = await db.get(Conversation, req.conversation_id)
            if not conv:
                raise HTTPException(404, "conversation not found")
        else:
            conv = Conversation()
            db.add(conv)
            await db.flush()

        # 保存用户消息
        user_msg = req.messages[-1]
        db.add(Message(
            conversation_id=conv.id,
            role=MessageRole.user,
            content=user_msg["content"],
        ))
        await db.commit()

        # 调用 LLM
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                f"{settings.llm_base_url}/v1/chat/completions",
                headers={"Authorization": f"Bearer {settings.llm_api_key}"},
                json={"model": req.model, "messages": req.messages},
            )
            resp.raise_for_status()
            result = resp.json()

        # 保存助手回复
        assistant_content = result["choices"][0]["message"]["content"]
        db.add(Message(
            conversation_id=conv.id,
            role=MessageRole.assistant,
            content=assistant_content,
        ))
        await db.commit()

        return {"conversation_id": conv.id, **result}


@router.post("/chat/stream")
async def chat_stream(req: ChatRequest, request: Request):
    # 会话管理（先建好会话，再返回流）
    async with async_session() as db:
        if req.conversation_id:
            conv = await db.get(Conversation, req.conversation_id)
            if not conv:
                raise HTTPException(404, "conversation not found")
        else:
            conv = Conversation()
            db.add(conv)
            await db.flush()

        user_msg = req.messages[-1]
        db.add(Message(
            conversation_id=conv.id,
            role=MessageRole.user,
            content=user_msg["content"],
        ))
        await db.commit()
        conv_id = conv.id  # 闭包捕获，db session 关闭后仍可用

    async def event_generator():
        full_content = ""
        try:
            async with httpx.AsyncClient() as client:
                async with client.stream(
                    "POST",
                    f"{settings.llm_base_url}/v1/chat/completions",
                    json={"model": req.model, "messages": req.messages, "stream": True},
                    headers={"Authorization": f"Bearer {settings.llm_api_key}"},
                ) as resp:
                    async for line in resp.aiter_lines():
                        # 客户端断连 → 停止消费上游
                        if await request.is_disconnected():
                            break
                        if line.startswith("data: "):
                            payload = line[6:]
                            if payload == "[DONE]":
                                break
                            yield f"data: {payload}\n\n"
                            # 累积文本内容
                            try:
                                chunk = json.loads(payload)
                                delta = (
                                    chunk.get("choices", [{}])[0]
                                    .get("delta", {})
                                )
                                if delta.get("content"):
                                    full_content += delta["content"]
                            except (json.JSONDecodeError, KeyError, IndexError):
                                pass
        except asyncio.CancelledError:
            # 客户端断开时 asyncio 会取消生成器
            pass
        finally:
            # 无论如何，保存已收到的内容（包括断开时的半截回复）
            if full_content:
                async with async_session() as db:
                    db.add(Message(
                        conversation_id=conv_id,
                        role=MessageRole.assistant,
                        content=full_content,
                    ))
                    await db.commit()

    return StreamingResponse(
        event_generator(), media_type="text/event-stream"
    )


@router.post("/tasks")
async def submit_task(req: TaskRequest, request: Request):
    task_id = str(uuid4())
    now = datetime.now().isoformat()
    async with async_session() as db:
        if req.idempotent_key:
            redis_client = request.app.state.redis
            existing = await redis_client.set(
                f"idempotent:{req.idempotent_key}",
                "1",
                nx=True,
                ex=86400,
            )
            if not existing:
                result = await db.execute(
                    sa_select(Task).where(Task.idempotent_key == req.idempotent_key)
                )
                dup_task = result.scalar_one_or_none()
                if dup_task:
                    return {"status": "duplicate", "task_id": dup_task.id}

        try:
            task = Task(
                id=task_id,
                instruction=req.instruction,
                context=req.context,
                idempotent_key=req.idempotent_key,
            )
            db.add(task)
            await db.commit()
        except IntegrityError:
            await db.rollback()
            if req.idempotent_key:
                result = await db.execute(
                    sa_select(Task).where(Task.idempotent_key == req.idempotent_key)
                )
                dup_task = result.scalar_one_or_none()
                if dup_task:
                    return {"status": "duplicate", "task_id": dup_task.id}
            raise

    channel = request.app.state.rmq_channel
    await channel.default_exchange.publish(
        aio_pika.Message(
            body=json.dumps(
                {
                    "task_id": task_id,
                    "instruction": req.instruction,
                    "context": req.context,
                },
                ensure_ascii=False,
            ).encode(),
            headers={"x-retry-count": 3},# 重试次数
        ),
        routing_key="ai_tasks",
    )
    await request.app.state.redis.setex(
        f"task:{task_id}",
        3600,
        json.dumps(
            {
                "task_id": task_id,
                "status": "queued",
                "result": None,
                "error": None,
                "steps": [],
                "created_at": now,
                "updated_at": now,
            },
            ensure_ascii=False,
        ),
    )
    return {"status": "queued", "task_id": task_id}


@router.get("/tasks/{task_id}")
async def get_task(task_id: str, request: Request):
    redis_client = request.app.state.redis
    cached = await redis_client.get(f"task:{task_id}")
    if cached:
        try:
            return json.loads(cached)
        except json.JSONDecodeError:
            await redis_client.delete(f"task:{task_id}")
    async with async_session() as db:
        stmt = sa_select(Task).options(selectinload(Task.steps)).where(Task.id == task_id)
        result = await db.execute(stmt)
        task = result.scalar_one_or_none()
        if not task:
            raise HTTPException(status_code=404, detail="task not found")
        result = {
            "task_id": task.id,
            "status": task.status.value,
            "result": task.result,
            "error": task.error,
            "steps": [
                {
                    "seq": s.seq,
                    "type": s.type,
                    "content": s.content,
                    "tool_name": s.tool_name,
                }
                for s in task.steps
            ],
            "created_at": task.created_at.isoformat(),
            "updated_at": task.updated_at.isoformat(),
        }
        await redis_client.setex(
            f"task:{task_id}", 300, json.dumps(result, ensure_ascii=False)
        )
        return result
