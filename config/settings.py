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
    SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL")
    
    # Fail fast if critical keys are missing
    @classmethod
    def validate(cls,require: list[str]) -> None:
        """
        Check that the specified env vars are set.
        Each module passes only the keys it actually needs.
        """
        missing = [key for key in require if not getattr(cls, key, None)]
        if missing:
            raise EnvironmentError(
                f"Missing required env vars: {', '.join(missing)}. "
                f"Copy .env.example to .env and fill in your keys."
            )


settings = Settings()
