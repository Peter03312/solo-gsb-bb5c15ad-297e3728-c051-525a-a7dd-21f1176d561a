"""数据库连接与会话管理。"""
from __future__ import annotations

import os

from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql+psycopg://cnc:cnc_password@db:5432/cnc_audit",
)

connect_args: dict = {}
if DATABASE_URL.startswith("sqlite"):
    connect_args = {"check_same_thread": False}

try:
    engine = create_engine(DATABASE_URL, pool_pre_ping=True, future=True,
                           connect_args=connect_args)
except ModuleNotFoundError as exc:  # 本地未安装 psycopg 时给出明确提示
    if "psycopg" not in str(exc):
        raise
    raise RuntimeError(
        "PostgreSQL 驱动 psycopg 未安装；请执行 pip install -r requirements.txt，"
        "或设置 DATABASE_URL=sqlite:///./cnc_audit.db 用于本地调试。"
    ) from exc
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False,
                            future=True)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
