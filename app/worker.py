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
        task_id = body["task_id"]
        instruction = body["instruction"]
        context = body.get("context", "")

        async with async_session() as db:
            task = await db.get(Task, task_id)
            if task is not None and task.status in (
                TaskStatus.success,
                TaskStatus.running,
            ):
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

        try:
            async with async_session() as db:
                # 任务超时
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
