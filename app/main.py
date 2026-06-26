from fastapi import FastAPI
from contextlib import asynccontextmanager
from fastapi.responses import StreamingResponse
from fastapi import APIRouter
from pydantic import BaseModel
from fastapi import Request
import httpx
from app.config import settings
import aio_pika
from uuid import uuid4  
from app.db import engine, Base

# 生命周期管理
@asynccontextmanager
async def lifespan(app: FastAPI):
    # 创建引擎、建表、连接 RabbitMQ
    # 1. 建表
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # 2. 连接 RabbitMQ
    rmq_conn = await aio_pika.connect_robust(settings.amqp_url)
    rmq_channel = await rmq_conn.channel()
    await rmq_channel.declare_queue("ai_tasks", durable=True)

    # 3. 挂到 app.state 上，端点里用
    app.state.rmq_channel = rmq_channel
    app.state.rmq_conn = rmq_conn
    yield
    # 断开连接
    await rmq_conn.close()
    await engine.dispose()

app = FastAPI(lifespan=lifespan)



# 请求模型
class ChatRequest(BaseModel):
    model: str = settings.llm_model
    messages: list[dict] # 对话历史

    
class TaskRequest(BaseModel):
    instruction: str # 任务指令
    context: str = "" # 补充上下文


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
    channel = request.app.state.rmq_channel
    await channel.default_exchange.publish(
        aio_pika.Message(body=req.model_dump_json().encode()),
        routing_key="ai_tasks",
    )
    return {"status": "queued", "task_id": task_id}



