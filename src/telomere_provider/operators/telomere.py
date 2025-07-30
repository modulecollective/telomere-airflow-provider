"""
Telomere operator for task-level lifecycle tracking.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional, Union

from airflow.models import BaseOperator
from airflow.utils.context import Context

from telomere_provider.hooks.telomere import TelomereHook


class TelomereLifecycleOperator(BaseOperator):
    """
    Tracks task execution as a Telomere lifecycle.

    This operator wraps task execution with Telomere lifecycle tracking,
    automatically starting a run when the task begins and ending it when
    the task completes or fails. Perfect for monitoring critical tasks
    that must complete within specific timeframes.

    :param lifecycle_name: Name for the lifecycle (default: {dag_id}.{task_id})
    :param timeout_seconds: Override timeout (default: task timeout or 3600)
    :param tags: Additional tags for the run
    :param telomere_conn_id: Connection ID for Telomere
    :param fail_on_telomere_error: Whether to fail the task if Telomere operations fail
    :param python_callable: The callable to execute (for PythonOperator compatibility)
    :param op_args: Arguments for python_callable
    :param op_kwargs: Keyword arguments for python_callable

    Example:
        critical_task = TelomereLifecycleOperator(
            task_id="process_payments",
            python_callable=process_payment_batch,
            lifecycle_name="payment_processing",
            timeout_seconds=300,  # 5 minutes
            tags={"priority": "critical"},
            dag=dag
        )
    """

    template_fields = ["lifecycle_name", "tags", "timeout_seconds"]
    template_fields_renderers = {"tags": "json"}

    def __init__(
        self,
        lifecycle_name: Optional[Union[str, Callable]] = None,
        timeout_seconds: Optional[Union[int, Callable]] = None,
        tags: Optional[Union[Dict[str, str], Callable]] = None,
        telomere_conn_id: str = TelomereHook.default_conn_name,
        fail_on_telomere_error: bool = False,
        python_callable: Optional[Callable] = None,
        op_args: Optional[list] = None,
        op_kwargs: Optional[dict] = None,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.lifecycle_name = lifecycle_name
        self.timeout_seconds = timeout_seconds
        self.tags = tags
        self.telomere_conn_id = telomere_conn_id
        self.fail_on_telomere_error = fail_on_telomere_error
        self.python_callable = python_callable
        self.op_args = op_args or []
        self.op_kwargs = op_kwargs or {}
        self._run_id: Optional[str] = None

    def _get_lifecycle_name(self, context: Context) -> str:
        """Get the lifecycle name, evaluating callable if needed."""
        dag_id = context['dag'].dag_id

        if self.lifecycle_name is None:
            # Default to dag_id.task_id
            return f"{dag_id}.{context['task'].task_id}"
        elif callable(self.lifecycle_name):
            name = self.lifecycle_name(**context)
        else:
            name = str(self.lifecycle_name)

        # Always namespace with DAG ID if not already present
        if not name.startswith(f"{dag_id}."):
            name = f"{dag_id}.{name}"

        return name

    def _get_timeout_seconds(self, context: Context) -> int:
        """Get timeout in seconds, evaluating callable if needed."""
        if self.timeout_seconds is not None:
            if callable(self.timeout_seconds):
                return int(self.timeout_seconds(**context))
            else:
                return int(self.timeout_seconds)

        # Try to get from task configuration
        task = context["task"]
        if hasattr(task, "execution_timeout") and task.execution_timeout:
            return int(task.execution_timeout.total_seconds())

        # Default to 1 hour
        return 3600

    def _get_tags(self, context: Context) -> Dict[str, str]:
        """Get tags, evaluating callable if needed."""
        base_tags = {
            "dag_id": context["dag"].dag_id,
            "task_id": context["task"].task_id,
            "run_id": context["run_id"],
            "try_number": str(context["ti"].try_number),
        }

        if self.tags:
            if callable(self.tags):
                additional_tags = self.tags(**context)
            else:
                additional_tags = self.tags
            base_tags.update(additional_tags)

        return base_tags

    def _get_url(self, context: Context) -> Optional[str]:
        """Get URL for the Airflow task instance."""
        try:
            # Try to construct Airflow UI URL
            from airflow.configuration import conf
            webserver_base_url = conf.get("webserver", "base_url", fallback=None)
            if webserver_base_url:
                ti = context["ti"]
                return (
                    f"{webserver_base_url}/dags/{ti.dag_id}/grid"
                    f"?dag_run_id={ti.run_id}&task_id={ti.task_id}"
                )
        except:
            pass
        return None

    def _start_telomere_run(self, context: Context) -> None:
        """Start Telomere run for this task."""
        try:
            hook = TelomereHook(self.telomere_conn_id)

            lifecycle_name = self._get_lifecycle_name(context)
            timeout_seconds = self._get_timeout_seconds(context)
            tags = self._get_tags(context)
            url = self._get_url(context)

            # Ensure lifecycle exists
            hook.ensure_lifecycle(
                name=lifecycle_name,
                default_timeout_seconds=timeout_seconds,
                description=f"Airflow task: {context['dag'].dag_id}.{context['task'].task_id}",
            )

            # Start run
            run = hook.start_run(
                lifecycle_name=lifecycle_name,
                timeout_seconds=timeout_seconds,
                tags=tags,
                url=url,
            )

            self._run_id = run["id"]
            self.log.info(f"Started Telomere run {self._run_id} for lifecycle {lifecycle_name}")

        except Exception as e:
            self.log.error(f"Failed to start Telomere run: {e}")
            if self.fail_on_telomere_error:
                raise

    def _end_telomere_run(self, context: Context, success: bool, error: Optional[Exception] = None) -> None:
        """End Telomere run for this task."""
        if not self._run_id:
            return

        try:
            hook = TelomereHook(self.telomere_conn_id)

            if success:
                hook.end_run(self._run_id, message="Task completed successfully")
                self.log.info(f"Ended Telomere run {self._run_id} as completed")
            else:
                message = f"Task failed: {str(error)}" if error else "Task failed"
                # Truncate message to reasonable length
                if len(message) > 1000:
                    message = message[:997] + "..."
                hook.fail_run(self._run_id, message=message)
                self.log.info(f"Ended Telomere run {self._run_id} as failed")

        except Exception as e:
            self.log.error(f"Failed to end Telomere run: {e}")
            if self.fail_on_telomere_error:
                raise

    def execute(self, context: Context) -> Any:
        """Execute the task with Telomere lifecycle tracking."""
        # Start Telomere run
        self._start_telomere_run(context)

        try:
            # Execute the actual task
            if self.python_callable:
                # Check if callable expects kwargs (like Airflow's PythonOperator)
                import inspect
                sig = inspect.signature(self.python_callable)

                # If the function accepts **kwargs, pass the context
                if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()):
                    kwargs = self.op_kwargs.copy()
                    kwargs.update(context)
                    result = self.python_callable(*self.op_args, **kwargs)
                else:
                    # Otherwise just pass the provided kwargs
                    result = self.python_callable(*self.op_args, **self.op_kwargs)
            else:
                # Subclasses should override this
                result = self._execute(context)

            # Mark as successful
            self._end_telomere_run(context, success=True)
            return result

        except Exception as e:
            # Mark as failed
            self._end_telomere_run(context, success=False, error=e)
            raise

    def _execute(self, context: Context) -> Any:
        """Override this method in subclasses to provide task logic."""
        raise NotImplementedError("Subclasses must implement _execute method")