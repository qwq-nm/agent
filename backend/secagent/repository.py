from sqlalchemy import select
from sqlalchemy.orm import Session

from secagent.db_models import TaskRow
from secagent.domain import TaskCreate, TaskRead, TaskStatus


class TaskRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def create_task(self, payload: TaskCreate) -> TaskRead:
        row = TaskRow(**payload.model_dump(mode="json"))
        self.session.add(row)
        self.session.commit()
        return self._read(row)

    def get_task(self, task_id: str) -> TaskRead | None:
        row = self.session.get(TaskRow, task_id)
        return self._read(row) if row else None

    def list_tasks(self) -> list[TaskRead]:
        rows = self.session.scalars(
            select(TaskRow).order_by(TaskRow.created_at.desc())
        ).all()
        return [self._read(row) for row in rows]

    def set_task_status(
        self,
        task_id: str,
        status: TaskStatus,
        *,
        is_demo: bool | None = None,
    ) -> TaskRead:
        row = self.session.get(TaskRow, task_id)
        if row is None:
            raise KeyError(task_id)
        row.status = status.value
        if is_demo is not None:
            row.is_demo = is_demo
        self.session.commit()
        return self._read(row)

    @staticmethod
    def _read(row: TaskRow) -> TaskRead:
        return TaskRead.model_validate(row, from_attributes=True)
