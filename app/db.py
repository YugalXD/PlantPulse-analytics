from functools import lru_cache

from sqlalchemy import Engine, create_engine

from app.config import settings


@lru_cache
def engine() -> Engine:
    return create_engine(settings.database_url, pool_pre_ping=True)
