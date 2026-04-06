"""
Central config loader. Every setting comes from here — nowhere else.

WHY: If you scatter os.getenv() across 10 files, you'll never know
what env vars your app needs. One file = one source of truth.
"""
import os
from dotenv import load_dotenv

load_dotenv()  # Reads .env file into environment variables


class Settings:
    ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "")
    OPENAI_API_KEY: str = os.getenv("OPENAI_API_KEY", "")
    GMAIL_CREDENTIALS_PATH: str = os.getenv("GMAIL_CREDENTIALS_PATH", "config/credentials.json")
    
    # Fail fast if critical keys are missing
    @classmethod
    def validate(cls):
        missing = []
        if not cls.OPENAI_API_KEY:
            missing.append("OPENAI_API_KEY")
        if missing:
            raise EnvironmentError(
                f"Missing required env vars: {', '.join(missing)}. "
                f"Copy .env.example to .env and fill in your keys."
            )


settings = Settings()
