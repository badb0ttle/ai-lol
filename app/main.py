from fastapi import FastAPI
from contextlib import asynccontextmanager
from fastapi.responses import StreamingResponse
from fastapi import APIRouter
from pydantic import BaseModel
from fastapi import Request
from fastapi import HTTPException
import json
from datetime import datetime
import redis.asyncio as aioredis

from app.db import async_session, engine, Base
from app.models import Task
import httpx
from app.config import settings
import aio_pika
from uuid import uuid4
from sqlalchemy import IntegrityError, select as sa_select


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


@app.post("/chat")
async def chat(req: ChatRequest):
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            f"{settings.llm_base_url}/v1/chat/completions",
            headers={"Authorization": f"Bearer {settings.llm_api_key}"},
            json=req.model_dump(),
        )
        resp.raise_for_status()
        return resp.json()


@router.post("/chat/stream")
async def chat_stream(req: ChatRequest):
    async def event_generator():
        async with httpx.AsyncClient() as client:
            async with client.stream(
                "POST",
                f"{settings.llm_base_url}/v1/chat/completions",
                json=req.model_dump(),
                headers={"Authorization": f"Bearer {settings.llm_api_key}"},
            ) as resp:
                async for line in resp.aiter_lines():
                    if line.startswith("data: "):
                        yield f"data: {line[6:]}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@router.post("/tasks")
async def submit_task(req: TaskRequest, request: Request):
    task_id = str(uuid4())
    now = datetime.utcnow().isoformat()
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
            ).encode()
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
        task = await db.get(Task, task_id)
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
