from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import Boolean, DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from secagent.db import Base


class TaskRow(Base):
    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid4()))
    goal: Mapped[str] = mapped_column(Text)
    authorization_scope: Mapped[str] = mapped_column(Text)
    route_mode: Mapped[str] = mapped_column(String)
    preferred_model: Mapped[str | None] = mapped_column(String, nullable=True)
    scene_hint: Mapped[str | None] = mapped_column(String, nullable=True)
    target_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    scene: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, default="created")
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc)
    )
