from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base
from app.config import CONFIG

engine = create_engine(
    CONFIG.DB_URL,
    connect_args={"check_same_thread": False} if CONFIG.DB_URL.startswith("sqlite") else {},
    pool_size=20,
    max_overflow=40,
    pool_timeout=60,
    pool_recycle=3600,
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()