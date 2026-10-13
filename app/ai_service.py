import os
import logging

import httpx
from dotenv import load_dotenv
from google import genai
from google.genai import types
from google.genai.errors import APIError
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential_jitter,
    retry_if_exception,
    before_log,
    after_log,
)

load_dotenv()
logger = logging.getLogger(__name__)

# --- 1. Fail fast: crash at startup if key is missing,
#         instead of failing mysteriously on every request ---
API_KEY = os.getenv("GEMINI_API_KEY")
if not API_KEY:
    raise RuntimeError("GEMINI_API_KEY is not set. Add it to your .env file.")

# --- 2. One shared client (SDK default timeouts already work;
#         forcing http_options(timeout=...) broke the SDK here) ---
client = genai.Client(api_key=API_KEY)

# --- 3. Typed errors: Layer 2 will map each to the right HTTP status code ---
class AIServiceError(Exception):
    """Base for every AI-layer failure."""

class AITimeoutError(AIServiceError):
    """Gemini took too long, or the connection failed."""

class AIRateLimitError(AIServiceError):
    """Quota exceeded (HTTP 429)."""

class AIProviderError(AIServiceError):
    """Any other provider failure (5xx / unexpected APIError)."""

class AIContentBlockedError(AIServiceError):
    """Safety filter blocked it, or the model returned no text."""

# --- 4. Retry ONLY transient errors: 429, 5xx, timeouts, connection issues ---
def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, (TimeoutError, ConnectionError,
                        httpx.TimeoutException, httpx.ConnectError)):
        return True
    code = getattr(exc, "code", None)
    return code == 429 or (isinstance(code, int) and code >= 500)

@retry(
    retry=retry_if_exception(_is_retryable),
    stop=stop_after_attempt(3),           # max 3 tries
    wait=wait_exponential_jitter(initial=1, max=8),  # 1s, 2s, 4s... capped 8s + jitter
    before=before_log(logger, logging.INFO),
    after=after_log(logger, logging.WARNING),
    reraise=True,                          # after 3 fails, raise the real error
)
def _generate(question: str) -> types.GenerateContentResponse:
    return client.models.generate_content(
        model="gemini-3.5-flash-lite",
        contents=question,
        config=types.GenerateContentConfig(temperature=0.2),
    )

# --- 5. Public function: same name/signature as before,
#         so main.py and test_gemini.py keep working unchanged ---
def generate_support_response(question: str) -> str:
    try:
        response = _generate(question)
    except (TimeoutError, ConnectionError,
            httpx.TimeoutException, httpx.ConnectError) as e:
        raise AITimeoutError("AI provider timed out or connection failed") from e
    except httpx.HTTPError as e:
        raise AIProviderError("AI provider network error") from e
    except APIError as e:
        code = getattr(e, "code", None)
        if code == 429:
            raise AIRateLimitError("AI provider rate limit reached") from e
        raise AIProviderError(f"AI provider error (status={code})") from e

    if not response.candidates:
        raise AIContentBlockedError("Response blocked by safety filters")
    try:
        text = (response.text or "").strip()
    except ValueError as e:
        raise AIContentBlockedError("Response contained no readable text") from e
    if not text:
        raise AIContentBlockedError("Model returned an empty answer")
    return text
