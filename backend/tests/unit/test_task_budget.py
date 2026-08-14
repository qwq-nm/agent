from datetime import datetime, timedelta, timezone

import pytest

from secagent.agents.budget import BudgetExceeded, TaskBudget


def test_budget_rejects_ninth_model_call_without_incrementing_usage() -> None:
    budget = TaskBudget(
        max_calls=8,
        max_input_tokens=120_000,
        max_output_tokens=24_000,
        max_steps=20,
        deadline=datetime.now(timezone.utc) + timedelta(minutes=5),
    )

    for _ in range(8):
        budget.consume_call()

    with pytest.raises(BudgetExceeded, match="model_calls") as raised:
        budget.consume_call()

    assert raised.value.dimension == "model_calls"
    assert budget.calls == 8


def test_budget_consumes_token_usage_atomically() -> None:
    budget = TaskBudget(
        max_calls=8,
        max_input_tokens=10,
        max_output_tokens=5,
        max_steps=20,
        deadline=datetime.now(timezone.utc) + timedelta(minutes=5),
    )

    budget.consume_tokens(6, 3)
    with pytest.raises(BudgetExceeded, match="output_tokens"):
        budget.consume_tokens(1, 3)

    assert budget.input_tokens == 6
    assert budget.output_tokens == 3


def test_budget_deadline_uses_injected_aware_clock() -> None:
    current = datetime(2026, 1, 1, tzinfo=timezone.utc)
    budget = TaskBudget(
        max_calls=1,
        max_input_tokens=1,
        max_output_tokens=1,
        max_steps=1,
        deadline=current + timedelta(seconds=1),
        now=lambda: current + timedelta(seconds=2),
    )

    with pytest.raises(BudgetExceeded, match="deadline"):
        budget.check_deadline()


def test_budget_rejects_naive_deadline_and_clock() -> None:
    aware = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with pytest.raises(ValueError, match="timezone-aware"):
        TaskBudget(1, 1, 1, 1, aware.replace(tzinfo=None))

    budget = TaskBudget(1, 1, 1, 1, aware, now=lambda: aware.replace(tzinfo=None))
    with pytest.raises(ValueError, match="timezone-aware"):
        budget.check_deadline()


def test_budget_rehydrates_persisted_usage() -> None:
    budget = TaskBudget(
        max_calls=8,
        max_input_tokens=10,
        max_output_tokens=10,
        max_steps=3,
        deadline=datetime.now(timezone.utc) + timedelta(minutes=5),
        calls=7,
        input_tokens=9,
        output_tokens=8,
        steps=2,
    )

    budget.consume_call()
    budget.consume_tokens(1, 2)
    budget.consume_step()

    assert (budget.calls, budget.input_tokens, budget.output_tokens, budget.steps) == (
        8,
        10,
        10,
        3,
    )


@pytest.mark.parametrize(
    ("usage", "dimension"),
    [
        ({"input_tokens": 10}, "input_tokens"),
        ({"output_tokens": 5}, "output_tokens"),
    ],
)
def test_model_call_preflight_rejects_fully_consumed_token_dimension(
    usage, dimension
) -> None:
    budget = TaskBudget(
        max_calls=8,
        max_input_tokens=10,
        max_output_tokens=5,
        max_steps=20,
        deadline=datetime.now(timezone.utc) + timedelta(minutes=5),
        **usage,
    )

    with pytest.raises(BudgetExceeded, match=dimension) as raised:
        budget.check_model_call()

    assert raised.value.dimension == dimension
