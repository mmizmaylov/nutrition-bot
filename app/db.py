from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Dict, Optional, Tuple
from zoneinfo import ZoneInfo

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, create_engine, func, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship, sessionmaker

ENGINE = create_engine("sqlite:///nutrition.db", echo=False, future=True)
SessionLocal = sessionmaker(bind=ENGINE, autoflush=False, autocommit=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    telegram_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    calorie_target: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    timezone: Mapped[str] = mapped_column(String(64), default="Europe/Moscow", nullable=False)

    meals: Mapped[list[Meal]] = relationship("Meal", back_populates="user", cascade="all, delete-orphan")


class Meal(Base):
    __tablename__ = "meals"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.telegram_id"), nullable=False, index=True)
    created_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    dish: Mapped[str] = mapped_column(String(512), nullable=False)
    portion: Mapped[str] = mapped_column(String(256), nullable=True)
    calories: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    raw_model_json: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    user: Mapped[User] = relationship("User", back_populates="meals")


@contextmanager
def get_session() -> Session:
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def init_db() -> None:
    Base.metadata.create_all(ENGINE)


def get_or_create_user(session: Session, telegram_id: int, default_tz: str) -> User:
    user = session.get(User, telegram_id)
    if user is None:
        user = User(telegram_id=telegram_id, timezone=default_tz)
        session.add(user)
    return user


def set_user_calorie_target(session: Session, telegram_id: int, target: int) -> None:
    user = get_or_create_user(session, telegram_id, "Europe/Moscow")
    user.calorie_target = target


def set_user_timezone(session: Session, telegram_id: int, tz: str) -> None:
    user = get_or_create_user(session, telegram_id, tz)
    user.timezone = tz


def _day_bounds_local(now_local: datetime) -> tuple[datetime, datetime]:
    day_start = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start.replace(hour=23, minute=59, second=59, microsecond=999999)
    return day_start, day_end


def get_today_totals(session: Session, telegram_id: int) -> Tuple[Optional[User], Dict[str, int]]:
    user = session.get(User, telegram_id)
    if user is None:
        return None, {"cal_today": 0}
    tz = ZoneInfo(user.timezone)
    now_local = datetime.now(tz)
    day_start_local, day_end_local = _day_bounds_local(now_local)

    # Convert local bounds to naive UTC by subtracting tz offset
    start_utc = day_start_local.astimezone(ZoneInfo("UTC"))
    end_utc = day_end_local.astimezone(ZoneInfo("UTC"))

    q = (
        select(func.coalesce(func.sum(Meal.calories), 0))
        .where(Meal.user_id == telegram_id)
        .where(Meal.created_at_utc >= start_utc)
        .where(Meal.created_at_utc <= end_utc)
    )
    cal_today = session.execute(q).scalar_one()
    return user, {"cal_today": int(cal_today or 0)}


def add_meal(
    session: Session,
    user_id: int,
    created_at_utc: datetime,
    dish: str,
    portion: Optional[str],
    calories: Optional[int],
    raw_model_json: Optional[dict[str, Any]] = None,
) -> Meal:
    raw_json_str = None
    if raw_model_json is not None:
        try:
            import json

            raw_json_str = json.dumps(raw_model_json, ensure_ascii=False)
        except Exception:
            raw_json_str = None

    meal = Meal(
        user_id=user_id,
        created_at_utc=created_at_utc,
        dish=dish,
        portion=portion or "",
        calories=calories,
        raw_model_json=raw_json_str,
    )
    session.add(meal)
    return meal 