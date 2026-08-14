from collections.abc import Callable
from datetime import datetime, timezone


class BudgetExceeded(RuntimeError):
    """A stable, non-sensitive task budget failure."""

    def __init__(self, dimension: str) -> None:
        self.dimension = dimension
        super().__init__(f"task budget exceeded: {dimension}")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _require_aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("budget timestamps must be timezone-aware")
    return value.astimezone(timezone.utc)


class TaskBudget:
    def __init__(
        self,
        max_calls: int,
        max_input_tokens: int,
        max_output_tokens: int,
        max_steps: int,
        deadline: datetime,
        *,
        calls: int = 0,
        input_tokens: int = 0,
        output_tokens: int = 0,
        steps: int = 0,
        now: Callable[[], datetime] = utcnow,
    ) -> None:
        limits = (max_calls, max_input_tokens, max_output_tokens, max_steps)
        usage = (calls, input_tokens, output_tokens, steps)
        if any(value < 0 for value in (*limits, *usage)):
            raise ValueError("budget limits and usage must be non-negative")
        self.max_calls = max_calls
        self.max_input_tokens = max_input_tokens
        self.max_output_tokens = max_output_tokens
        self.max_steps = max_steps
        self.deadline = _require_aware(deadline)
        self.calls = calls
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.steps = steps
        self.now = now

    def check_deadline(self) -> None:
        if _require_aware(self.now()) >= self.deadline:
            raise BudgetExceeded("deadline")

    def check_model_call(self) -> None:
        if self.calls >= self.max_calls:
            raise BudgetExceeded("model_calls")
        if self.input_tokens >= self.max_input_tokens:
            raise BudgetExceeded("input_tokens")
        if self.output_tokens >= self.max_output_tokens:
            raise BudgetExceeded("output_tokens")

    def check_usage(self) -> None:
        for used, maximum, dimension in (
            (self.calls, self.max_calls, "model_calls"),
            (self.input_tokens, self.max_input_tokens, "input_tokens"),
            (self.output_tokens, self.max_output_tokens, "output_tokens"),
            (self.steps, self.max_steps, "steps"),
        ):
            if used > maximum:
                raise BudgetExceeded(dimension)

    def consume_call(self) -> None:
        self.check_model_call()
        self.calls += 1

    def consume_tokens(self, input_tokens: int, output_tokens: int) -> None:
        if input_tokens < 0 or output_tokens < 0:
            raise ValueError("token usage must be non-negative")
        if self.input_tokens + input_tokens > self.max_input_tokens:
            raise BudgetExceeded("input_tokens")
        if self.output_tokens + output_tokens > self.max_output_tokens:
            raise BudgetExceeded("output_tokens")
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens

    def consume_step(self) -> None:
        if self.steps + 1 > self.max_steps:
            raise BudgetExceeded("steps")
        self.steps += 1
