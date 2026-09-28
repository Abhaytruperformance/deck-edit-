from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    supabase_url: str = ""
    supabase_key: str = ""
    openrouter_api_key: str = ""
    openrouter_model: str = "anthropic/claude-opus-5"
    session_secret: str = "change-me"

    class Config:
        env_file = ".env"


settings = Settings()
