from secagent.dag_domain import JobKind


class CeleryJobQueue:
    def enqueue(self, task_id: str, command_id: str) -> str:
        from secagent.worker import run_task

        result = run_task.apply_async(
            args=[task_id, command_id], task_id=command_id
        )
        return result.id

    def enqueue_dag_job(self, kind: JobKind, ref_id: str, command_id: str) -> str:
        from secagent.worker import (
            run_subtask_execute,
            run_turn_decompose,
            run_turn_synthesize,
        )

        tasks = {
            JobKind.TURN_DECOMPOSE: run_turn_decompose,
            JobKind.SUBTASK_EXECUTE: run_subtask_execute,
            JobKind.TURN_SYNTHESIZE: run_turn_synthesize,
        }
        result = tasks[kind].apply_async(args=[ref_id, command_id], task_id=command_id)
        return result.id
