# AI Task Backend - 项目说明

## 核心功能
- 异步任务投递（POST /tasks）
- Agent 引擎自主编排工具调用
- 全链路持久化（每一步 think/tool_call/tool_result/reply）
- 幂等去重、死信队列、超时控制

## 技术栈
FastAPI + RabbitMQ + PostgreSQL + Redis + Docker Compose
