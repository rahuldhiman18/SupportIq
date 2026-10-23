import os
from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, DeclarativeBase

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")

#check condition (must run BEFORE create_engine, or we get a cryptic error instead)
if not DATABASE_URL:
    raise RuntimeError("Database Url is not set,Add it to your .env file")

#for database engine
# Connection pool tuning (Phase 1.4). Applied only for non-SQLite dialects
# (PostgreSQL in prod) since SQLite's SingletonThreadPool doesn't support
# these args and tests run against SQLite.
_POOL_KWARGS = {}
if DATABASE_URL and not DATABASE_URL.startswith("sqlite"):
    _POOL_KWARGS = {
        "pool_size": 20,        # persistent connections kept ready
        "max_overflow": 40,     # extra connections under burst traffic
        "pool_timeout": 30,     # seconds to wait for a free conn before 503
        "pool_recycle": 1800,   # recycle after 30 min so idle conns don't go stale
        "pool_pre_ping": True,  # test conn before use; avoids "server closed
                                # connection" after Postgres restarts/idle timeouts
    }

engine = create_engine(
    DATABASE_URL,
    echo=False,
    **_POOL_KWARGS,
)

SessionLocal = sessionmaker(bind=engine,autoflush=False)
#bind=engine says session made by factory to talk his database
#autoflush=False stops SQLAlchemy from quietly sending pending changes to the database before every query

class Base(DeclarativeBase):
    pass