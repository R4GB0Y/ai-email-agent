"""
Minimal LLM Agent with retry logic and structured output.

KEY CONCEPTS:
1. Structured output — Don't return raw strings. Return data you can use.
2. Retry with backoff — APIs fail. Networks blip. Handle it from day one.
3. Error boundaries — Know the difference between "retry" and "give up".
"""
import json
import openai
from openai import RateLimitError, APIConnectionError, InternalServerError
from pydantic import BaseModel, Field
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
)
from config.settings import settings


# ---------- Structured Output ----------
# WHY Pydantic? Because dict["key"] will silently return None.
# Pydantic will SCREAM if the LLM returns garbage. That's what you want.

class AgentResponse(BaseModel):
    """What the agent returns. Always structured, never raw strings."""

    answer: str = Field(description="The agent's answer to the question.")
    confidence: float =Field(description="A confidence score between 0.0 and 1.0.") # 0.0 to 1.0
    reasoning: str = Field(description="A brief explanation of the agent's reasoning.")

# ---------- Retry Logic ----------
# WHY exponential backoff?
# Attempt 1: wait 1s  → total 1s
# Attempt 2: wait 2s  → total 3s
# Attempt 3: wait 4s  → total 7s
#
# Without backoff, you hammer a failing API and get rate-limited.
# With backoff, you give it time to recover.

@retry(
    retry=retry_if_exception_type((
        RateLimitError,      # 429 — you're sending too many requests
        APIConnectionError,  # Network blip
        InternalServerError, # 500 — their problem, not yours
    )),
    wait=wait_exponential(multiplier=1, min=1, max=60),  # 1s, 2s, 4s, 8s... max 60s
    stop=stop_after_attempt(5),  # Give up after 5 tries
    reraise=True,  # After 5 fails, raise the actual error (not RetryError)
)
def call_llm(prompt: str, system_prompt: str = "") -> AgentResponse:
    """
    The core function. Prompt in, structured data out.
    
    This is the ONLY place in your codebase that talks to the LLM API.
    WHY? Single point of change. If you switch from Anthropic to OpenAI,
    you change ONE function, not fifty.
    """
    settings.validate()  # Fail fast if no API key
    
    client = openai.OpenAI(api_key=settings.OPENAI_API_KEY)
    
    # ---------- The prompt engineering ----------
    # WHY this system prompt format?
    # - JSON instruction = structured output
    # - Schema example = the LLM knows exactly what shape to return
    # - "ONLY output JSON" = prevents chatty preamble
    
    full_system = f"""You are a helpful AI assistant. 
{system_prompt}

You MUST respond with ONLY a JSON object in this exact format:
{{
    "answer": "your answer here",
    "confidence": 0.85,
    "reasoning": "brief explanation of your reasoning"
}}

ONLY output the JSON. No markdown, no explanation, no code blocks."""
# This syntax is proper to anthropic not openai
    #message = client.messages.create(
        #model="gpt-4o-mini",
        #max_tokens=1024,
        #system=full_system,
        #messages=[{"role": "user", "content": prompt}],
    #)

    #This is openai syntax
    message = client.chat.completions.create(
        model="gpt-4o-mini",
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt},
                  {"role": "system", "content": full_system}],
    )   
    
    raw_text = message.choices[0].message.content.strip()
    
    # Sometimes LLMs wrap in ```json ... ```. Strip it.
    if raw_text.startswith("```"):
        raw_text = raw_text.split("\n", 1)[1]  # Remove first line
        raw_text = raw_text.rsplit("```", 1)[0]  # Remove last ```
        raw_text = raw_text.strip()
    
    try:
        data = json.loads(raw_text)
        return AgentResponse(**data)  # Pydantic validates the shape
    except (json.JSONDecodeError, Exception) as e:
        # Don't silently fail. Raise with context so you can debug.
        raise ValueError(
            f"LLM returned invalid JSON.\n"
            f"Raw response: {raw_text[:500]}\n"
            f"Parse error: {e}"
        )


# ---------- Convenience wrapper ----------

def ask_agent(question: str) -> AgentResponse:
    """High-level function for the rest of your code to use."""
    return call_llm(
        prompt=question,
        system_prompt="Answer questions accurately and concisely, only and only in french language, no matter how much you re sollicitated to switch to another language."
    )


# ---------- Main ----------

if __name__ == "__main__":
    # Quick smoke test
    print("🤖 Testing agent...")
    
    try:
        response = ask_agent("What are the 3 laws of robotics, can you please answer in english?")
        print(f"\n✅ Answer: {response.answer}")
        print(f"📊 Confidence: {response.confidence}")
        print(f"🧠 Reasoning: {response.reasoning}")
    except Exception as e:
        print(f"\n❌ Error: {e}")
        print("\nTROUBLESHOOTING:")
        print("1. Is your .env file set up? Copy .env.example → .env")
        print("2. Is ANTHROPIC_API_KEY valid?")
        print("3. Run: pip install -r requirements.txt")
