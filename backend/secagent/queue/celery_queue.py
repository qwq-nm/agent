class CeleryJobQueue:
    def enqueue(self, task_id: str, command_id: str) -> str:
        from secagent.worker import run_task

        result = run_task.apply_async(
            args=[task_id, command_id], task_id=command_id
        )
        return result.id
