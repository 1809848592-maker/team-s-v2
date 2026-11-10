from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """所有业务模型共用的 SQLAlchemy 2 声明式基类。"""
