import asyncio
import json
import logging
import time

import aio_pika
import redis.asyncio as aioredis

from app.agent_core import run_agent
from app.config import settings
from app.db import async_session
from app.models import Task, TaskStatus

logger = logging.getLogger("worker")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(message)s",
    datefmt="%H:%M:%S",
)


async def process_message(
    message: aio_pika.IncomingMessage,
    redis_client: aioredis.Redis,
    channel: aio_pika.Channel,
) -> None:
    retry_count = 0
    t0 = time.monotonic()
    try:
        body = json.loads(message.body.decode())
        task_id = body["task_id"]
        instruction = body["instruction"]
        context = body.get("context", "")
        retry_count = int((message.headers or {}).get("x-retry-count", 3))

        logger.info("📥 收到任务 task=%s retry=%d | %s", task_id[:8], retry_count, instruction[:60])

        async with async_session() as db:
            task = await db.get(Task, task_id)
            if task is not None and task.status in (
                TaskStatus.success,
                TaskStatus.running,
            ):
                await message.ack()
                return
            if task is None:
                task = Task(
                    id=task_id,
                    instruction=instruction,
                    context=context,
                    status=TaskStatus.running,
                )
                db.add(task)
            else:
                task.instruction = instruction
                task.context = context
                task.status = TaskStatus.running
            await db.commit()
            await redis_client.delete(f"task:{task_id}")

        logger.info("▶️  开始执行 task=%s", task_id[:8])

        async with async_session() as db:
            result = await asyncio.wait_for(
                run_agent(instruction, context, db, task_id),
                timeout=settings.task_timeout,
            )

        async with async_session() as db:
            task = await db.get(Task, task_id)
            if task is None:
                raise RuntimeError(f"task not found: {task_id}")
            task.status = TaskStatus.success
            task.result = result
            await db.commit()
            await redis_client.delete(f"task:{task_id}")
        elapsed = time.monotonic() - t0
        logger.info("✅ 任务完成 task=%s 耗时=%.1fs | %s", task_id[:8], elapsed, (result or "")[:80])
        await message.ack()
    except Exception as exc:
        elapsed = time.monotonic() - t0
        logger.error("❌ 任务失败 task=%s 耗时=%.1fs | %s", task_id[:8] if task_id else "?", elapsed, exc)
        try:
            body = json.loads(message.body.decode())
            task_id = body.get("task_id")
        except Exception:
            task_id = None

        if task_id is not None:
            async with async_session() as db:
                task = await db.get(Task, task_id)
                if task is not None:
                    task.status = TaskStatus.failed
                    task.error = str(exc)
                    await db.commit()
                    await redis_client.delete(f"task:{task_id}")

            try:
                if retry_count > 0:
                    await channel.default_exchange.publish(
                        aio_pika.Message(
                            body=message.body,
                            headers={"x-retry-count": retry_count - 1},
                        ),
                        routing_key="ai_tasks",
                    )
                else:
                    await channel.default_exchange.publish(
                        aio_pika.Message(
                            body=message.body,
                            headers={"x-error": str(exc)},
                        ),
                        routing_key="ai_tasks.dlq",
                    )
                await message.ack()
                return
            except Exception:
                await message.nack(requeue=True)
                return

        await message.nack(requeue=True)


async def consume_message(
    message: aio_pika.IncomingMessage,
    redis_client: aioredis.Redis,
    channel: aio_pika.Channel,
) -> None:
    await process_message(message, redis_client, channel)


async def main() -> None:
    redis_client = aioredis.from_url(settings.redis_url, decode_responses=True)
    conn = await aio_pika.connect_robust(settings.amqp_url)
    channel = await conn.channel()
    await channel.set_qos(prefetch_count=1)
    await channel.declare_queue("ai_tasks.dlq", durable=True)
    queue = await channel.declare_queue("ai_tasks", durable=True)

    async def on_message(message: aio_pika.IncomingMessage) -> None:
        await consume_message(message, redis_client, channel)

    await queue.consume(on_message)

    try:
        await asyncio.Future()
    finally:
        await conn.close()
        await redis_client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
