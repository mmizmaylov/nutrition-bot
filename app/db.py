from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, date
from typing import Any, Dict, Optional, Tuple
from zoneinfo import ZoneInfo

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, create_engine, func, select, UniqueConstraint
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


class DailySummary(Base):
    __tablename__ = "daily_summaries"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.telegram_id"), nullable=False, index=True)
    # День в локальной зоне пользователя в формате YYYY-MM-DD
    day_local: Mapped[str] = mapped_column(String(10), nullable=False)
    sent_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint("user_id", "day_local", name="uq_daily_summary_user_day"),
    )


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


# New helpers for daily summaries

def get_all_users(session: Session) -> list[User]:
    q = select(User).order_by(User.telegram_id.asc())
    return list(session.execute(q).scalars().all())


def get_meals_for_local_day(session: Session, telegram_id: int, day: date, tzid: str) -> list[Meal]:
    tz = ZoneInfo(tzid)
    start_local = datetime(day.year, day.month, day.day, 0, 0, 0, tzinfo=tz)
    end_local = start_local.replace(hour=23, minute=59, second=59, microsecond=999999)

    start_utc = start_local.astimezone(ZoneInfo("UTC"))
    end_utc = end_local.astimezone(ZoneInfo("UTC"))

    q = (
        select(Meal)
        .where(Meal.user_id == telegram_id)
        .where(Meal.created_at_utc >= start_utc)
        .where(Meal.created_at_utc <= end_utc)
        .order_by(Meal.created_at_utc.asc())
    )
    return list(session.execute(q).scalars().all())


def get_day_total_calories(session: Session, telegram_id: int, day: date, tzid: str) -> int:
    tz = ZoneInfo(tzid)
    start_local = datetime(day.year, day.month, day.day, 0, 0, 0, tzinfo=tz)
    end_local = start_local.replace(hour=23, minute=59, second=59, microsecond=999999)
    start_utc = start_local.astimezone(ZoneInfo("UTC"))
    end_utc = end_local.astimezone(ZoneInfo("UTC"))
    q = (
        select(func.coalesce(func.sum(Meal.calories), 0))
        .where(Meal.user_id == telegram_id)
        .where(Meal.created_at_utc >= start_utc)
        .where(Meal.created_at_utc <= end_utc)
    )
    total = session.execute(q).scalar_one()
    return int(total or 0)


def has_summary_sent(session: Session, telegram_id: int, day_local_str: str) -> bool:
    q = (
        select(func.count(DailySummary.id))
        .where(DailySummary.user_id == telegram_id)
        .where(DailySummary.day_local == day_local_str)
    )
    return (session.execute(q).scalar_one() or 0) > 0


def mark_summary_sent(session: Session, telegram_id: int, day_local_str: str) -> None:
    rec = DailySummary(user_id=telegram_id, day_local=day_local_str, sent_at_utc=datetime.now(ZoneInfo("UTC")))
    session.add(rec) 