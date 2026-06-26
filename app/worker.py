import asyncio
import json

import aio_pika

from app.agent_core import run_agent
from app.config import settings
from app.db import async_session
from app.models import Task, TaskStatus


async def process_message(message: aio_pika.IncomingMessage) -> None:
    async with message.process():
        body = json.loads(message.body.decode())
        instruction = body["instruction"]
        context = body.get("context", "")

        async with async_session() as db:
            task = Task(
                instruction=instruction,
                context=context,
                status=TaskStatus.running,
            )
            db.add(task)
            await db.commit()
            task_id = task.id

        try:
            async with async_session() as db:
                result = await run_agent(instruction, context, db, task_id)

            async with async_session() as db:
                task = await db.get(Task, task_id)
                if task is None:
                    raise RuntimeError(f"task not found: {task_id}")
                task.status = TaskStatus.success
                task.result = result
                await db.commit()
        except Exception as exc:
            async with async_session() as db:
                task = await db.get(Task, task_id)
                if task is not None:
                    task.status = TaskStatus.failed
                    task.error = str(exc)
                    await db.commit()


async def main() -> None:
    conn = await aio_pika.connect_robust(settings.amqp_url)
    channel = await conn.channel()
    await channel.set_qos(prefetch_count=1)
    queue = await channel.declare_queue("ai_tasks", durable=True)
    await queue.consume(process_message)

    try:
        await asyncio.Future()
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
