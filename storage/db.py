"""
Engine + session setup.

Defaults to a local SQLite file so the project runs with zero external
dependencies. Set DATABASE_URL to a Postgres connection string (e.g. a
Render/Neon/Supabase free-tier instance) to swap backends with no code
changes elsewhere — every other module only imports `SessionLocal`.

Why SQLite is fine here, and when it stops being fine
------------------------------------------------------
SQLite handles our write pattern comfortably: we write ~1 row per route
per poll (so maybe 30-100 rows/minute across all monitored routes), and
SQLite's single-writer model only becomes a bottleneck well above that.
The moment this becomes a problem is concurrent writers — e.g. if you
horizontally scale the ingest job — at which point Postgres is one
environment variable away.
"""

import os

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from storage.models import Base

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "sqlite:///./transit_anomalies.db",
)

# check_same_thread=False is required for SQLite when used from both the
# APScheduler background thread and FastAPI's request threads.
connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}

engine = create_engine(DATABASE_URL, connect_args=connect_args, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


def init_db() -> None:
    Base.metadata.create_all(bind=engine)
