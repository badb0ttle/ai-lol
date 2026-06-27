from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # LLM
    llm_api_key: str
    llm_base_url: str = "https://api.deepseek.com"
    llm_model: str = "deepseek-v4-flash"

    # Database
    database_url: str = "postgresql+asyncpg://dev:devpass123@localhost:5432/ai_tasks" # postgresql+asyncpg://user:password@hostname:port/dbname

    # Redis
    redis_url: str = "redis://localhost:6379/0" # redis://:password@hostname:port/db_number

    # RabbitMQ
    amqp_url: str = "amqp://dev:devpass123@localhost:5672/"

    # Agent
    max_loops: int = 10
    task_timeout: int = 120
    llm_timeout: int = 60
    max_retries: int = 3

    model_config = {"env_file": ".env.dev", "env_file_encoding": "utf-8"}


settings = Settings()
