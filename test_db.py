"""Manual database connectivity check.

Only runs when executed directly:  python test_db.py
Safe for pytest to import (nothing executes at import time).
"""

from sqlalchemy import text

from app.core.database import engine


def main() -> None:
    with engine.connect() as conn:
        result = conn.execute(text("SELECT 1"))
    print(result.scalar())


if __name__ == "__main__":
    main()
