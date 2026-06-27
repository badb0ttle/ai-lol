import ast
import asyncio
import json
import os
import pathlib
import shlex
import subprocess
from datetime import datetime, timezone
from typing import Any

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import TaskStep

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_current_time",
            "description": "获取当前时间",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "calculate",
            "description": "执行数学计算",
            "parameters": {
                "type": "object",
                "properties": {
                    "expression": {
                        "type": "string",
                        "description": "数学表达式",
                    }
                },
                "required": ["expression"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_url",
            "description": "从指定 URL 抓取网页内容，返回文本摘要",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "目标网页 URL"}
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": (
                "在 data 目录下执行只读命令，支持：cat、head、tail、wc、grep、sort、"
                "uniq、cut、tr、find、ls、file、stat、echo、date、expr、test。"
                "命令超时 10 秒，输出上限 3000 字符。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "要执行的命令（不含管道和重定向）",
                    }
                },
                "required": ["command"],
            },
        },
    },
]


def _utc_now_text() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_calculate(expression: str) -> Any:
    allowed_nodes = (
        ast.Expression,
        ast.BinOp,
        ast.UnaryOp,
        ast.Add,
        ast.Sub,
        ast.Mult,
        ast.Div,
        ast.FloorDiv,
        ast.Mod,
        ast.Pow,
        ast.UAdd,
        ast.USub,
        ast.Constant,
        ast.Load,
    )

    tree = ast.parse(expression, mode="eval")
    for node in ast.walk(tree):
        if not isinstance(node, allowed_nodes):
            raise ValueError("expression contains unsupported operations")

    return eval(compile(tree, "<calculate>", "eval"), {"__builtins__": {}}, {})


async def _next_step_seq(db_session: AsyncSession, task_id: str) -> int:
    result = await db_session.execute(
        select(func.coalesce(func.max(TaskStep.seq), 0)).where(
            TaskStep.task_id == task_id
        )
    )
    return int(result.scalar_one()) + 1


async def record_step(
    db_session: AsyncSession,
    task_id: str,
    step_type: str,
    content: str,
    tool_name: str | None = None,
) -> None:
    seq = await _next_step_seq(db_session, task_id)
    db_session.add(
        TaskStep(
            task_id=task_id,
            seq=seq,
            type=step_type,
            content=content,
            tool_name=tool_name,
        )
    )
    await db_session.commit()


BASE_DIR = os.path.join(os.path.dirname(__file__), "..", "data")


async def execute_tool(name: str, args: dict[str, Any]) -> Any:
    # 安全沙箱：仅白名单命令，限制在 data 目录
    if name == "run_command":
        ALLOWED = {
            "cat", "head", "tail", "wc", "grep", "sort", "uniq",
            "cut", "tr", "find", "ls", "file", "stat", "echo", "date", "expr", "test",
        }
        raw = str(args.get("command", ""))
        tokens = shlex.split(raw)
        if not tokens:
            raise ValueError("命令不能为空")
        if tokens[0] not in ALLOWED:
            raise ValueError(f"命令不在白名单: {tokens[0]}")
        # 禁止管道、重定向和路径穿越
        for tok in tokens:
            if tok in {"|", ">", ">>", "<", "&&", "||", ";", "&"}:
                raise ValueError(f"禁止的操作符: {tok}")
        # 检查所有路径参数，防止越界
        base = pathlib.Path(BASE_DIR).resolve()
        for tok in tokens[1:]:
            # 跳过不以路径开头的参数（如 -l, -la, 数字）
            if tok.startswith("-") or tok.lstrip(".").isdigit():
                continue
            try:
                full = (base / tok).resolve()
                if not str(full).startswith(str(base) + os.sep) and full != base:
                    raise ValueError(f"路径越界: {tok}")
            except Exception:
                raise ValueError(f"无效路径: {tok}")
        proc = await asyncio.create_subprocess_exec(
            *tokens,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=BASE_DIR,
            limit=3000,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=10
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return {"error": "命令执行超时 (10s)"}
        return {
            "stdout": stdout.decode(errors="replace"),
            "stderr": stderr.decode(errors="replace"),
            "exit_code": proc.returncode,
        }

    if name == "fetch_url":
        url = str(args.get("url", ""))
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url, follow_redirects=True)
            text = resp.text[:2000]
        return {"url": url, "status": resp.status_code, "text_preview": text}

    if name == "get_current_time":
        return {"current_time": _utc_now_text()}

    if name == "calculate":
        expression = str(args.get("expression", ""))
        if not expression.strip():
            raise ValueError("expression is required")
        return {"expression": expression, "result": _safe_calculate(expression)}

    raise ValueError(f"unsupported tool: {name}")


async def run_agent(
    instruction: str,
    context: str,
    db_session: AsyncSession,
    task_id: str,
) -> str:
    messages: list[dict[str, Any]] = [
        {
            "role": "system",
            "content": (
                "你是一个会调用工具的 AI 任务执行器。"
                "在需要时使用工具，完成后给出简洁、明确的最终回复。"
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "instruction": instruction,
                    "context": context,
                },
                ensure_ascii=False,
            ),
        },
    ]

    async with httpx.AsyncClient(timeout=settings.llm_timeout) as http_client:
        for _ in range(settings.max_loops):
            response = await http_client.post(
                f"{settings.llm_base_url}/v1/chat/completions",
                headers={"Authorization": f"Bearer {settings.llm_api_key}"},
                json={
                    "model": settings.llm_model,
                    "messages": messages,
                    "tools": TOOLS,
                },
            )
            response.raise_for_status()
            data = response.json()
            choice = data["choices"][0]["message"]

            assistant_content = choice.get("content") or ""
            if assistant_content.strip():
                await record_step(db_session, task_id, "think", assistant_content)

            tool_calls = choice.get("tool_calls") or []
            if tool_calls:
                messages.append(
                    {
                        "role": "assistant",
                        "content": assistant_content or None,
                        "tool_calls": tool_calls,
                    }
                )

                for tool_call in tool_calls:
                    function_data = tool_call.get("function", {})
                    tool_name = function_data.get("name", "")
                    raw_arguments = function_data.get("arguments") or "{}"
                    try:
                        parsed_arguments = json.loads(raw_arguments)
                    except json.JSONDecodeError:
                        parsed_arguments = {}

                    await record_step(
                        db_session,
                        task_id,
                        "tool_call",
                        json.dumps(
                            {
                                "tool_call_id": tool_call.get("id"),
                                "name": tool_name,
                                "arguments": parsed_arguments,
                            },
                            ensure_ascii=False,
                        ),
                        tool_name=tool_name,
                    )
                    # 工具重试
                    for attempt in range(2):
                        try:
                            tool_result = await execute_tool(tool_name, parsed_arguments)
                            break
                        except Exception as e:
                            if attempt == 1:
                                tool_result = {"error": f"工具 {tool_name} 重试 2 次均失败: {str(e)}"}

                    
                    await record_step(
                        db_session,
                        task_id,
                        "tool_result",
                        json.dumps(tool_result, ensure_ascii=False),
                        tool_name=tool_name,
                    )

                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tool_call.get("id"),
                            "content": json.dumps(tool_result, ensure_ascii=False),
                        }
                    )
                continue

            final_reply = assistant_content.strip()
            await record_step(db_session, task_id, "reply", final_reply)
            return final_reply

    raise RuntimeError("agent exceeded max_loops without producing a final reply")
