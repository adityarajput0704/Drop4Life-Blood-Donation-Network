from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, DeclarativeBase
import os
from dotenv import load_dotenv
from backend.config import get_settings

# load_dotenv()
# DATABASE_URL = os.getenv("DATABASE_URL")

settings = get_settings()

engine = create_engine(
    settings.DATABASE_URL,
    pool_size=5,
    max_overflow=10,
    pool_timeout=30,
    pool_recycle=300,
    pool_pre_ping=True,
    echo=True,
)


SessionLocal = sessionmaker(
    bind=engine,
    autocommit=False,
    autoflush=False,
)

class Base(DeclarativeBase):
    pass

