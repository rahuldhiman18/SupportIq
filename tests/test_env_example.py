"""`.env.example` must document every variable the code actually reads.

Root cause this guards against: ADMIN_API_KEY was missing from .env (and
undocumented anywhere), so every admin endpoint fail-closed with 401 and
the runbook existed only in one person's head.
"""
from pathlib import Path

# Every os.getenv(...) the app relies on for startup/behavior.
REQUIRED_VARS = {
    "DATABASE_URL",      # database.py fail-fast
    "GEMINI_API_KEY",    # ai_service.py fail-fast
    "JWT_SECRET_KEY",    # login (503 when unset outside tests)
    "ADMIN_API_KEY",     # admin endpoints (fail-closed 401 when unset)
    "QUERY_MODE",        # inline | queued
}

_ROOT = Path(__file__).resolve().parents[1]


def test_env_example_documents_every_required_var():
    example = _ROOT / ".env.example"
    assert example.exists(), ".env.example missing -- new devs cannot configure the server"
    keys = {
        line.split("=", 1)[0].strip()
        for line in example.read_text(encoding="utf-8-sig").splitlines()
        if "=" in line and not line.strip().startswith("#")
    }
    missing = REQUIRED_VARS - keys
    assert not missing, f".env.example is missing: {sorted(missing)}"


def test_env_example_values_are_placeholders_not_secrets():
    # Placeholder discipline: example values must look fake, never real.
    text = (_ROOT / ".env.example").read_text(encoding="utf-8-sig")
    for line in text.splitlines():
        if line.startswith("GEMINI_API_KEY="):
            value = line.split("=", 1)[1]
            assert value.startswith("your-") or value.startswith("AIza"), \
                "GEMINI_API_KEY example does not look like a placeholder"
        if line.startswith("ADMIN_API_KEY="):
            value = line.split("=", 1)[1]
            assert "random" in value, "ADMIN_API_KEY example must say it is random"