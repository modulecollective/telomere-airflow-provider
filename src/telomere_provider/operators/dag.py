"""
Telomere operators for DAG-level lifecycle tracking.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from airflow.models import BaseOperator, Variable
from airflow.utils.context import Context
from airflow.utils.state import State

from telomere_provider.hooks.telomere import TelomereHook


class TelomereDAGStartOperator(BaseOperator):
    """
    Marks the start of a DAG run in Telomere.

    This operator should be placed at the beginning of your DAG to start
    tracking the entire DAG execution as a Telomere lifecycle. For scheduled
    DAGs, it also manages a separate schedule lifecycle to monitor that runs
    start on time.

    :param lifecycle_name: Name for the lifecycle (default: dag_id)
    :param timeout_seconds: Timeout for the entire DAG run (auto-calculated for scheduled DAGs)
    :param tags: Additional tags for the run
    :param telomere_conn_id: Connection ID for Telomere
    :param fail_on_telomere_error: Whether to fail the task if Telomere operations fail
    """

    template_fields = ["lifecycle_name", "tags", "timeout_seconds"]
    template_fields_renderers = {"tags": "json"}

    def __init__(
        self,
        lifecycle_name: Optional[str] = None,
        timeout_seconds: Optional[int] = None,
        tags: Optional[Dict[str, str]] = None,
        telomere_conn_id: str = TelomereHook.default_conn_name,
        fail_on_telomere_error: bool = False,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.lifecycle_name = lifecycle_name
        self.timeout_seconds = timeout_seconds
        self.tags = tags
        self.telomere_conn_id = telomere_conn_id
        self.fail_on_telomere_error = fail_on_telomere_error

    def execute(self, context: Context) -> Any:
        """Start tracking the DAG run."""
        try:
            hook = TelomereHook(self.telomere_conn_id)

            # Get lifecycle name - always namespace with DAG ID and add .dag suffix
            dag_id = context["dag"].dag_id
            if self.lifecycle_name:
                lifecycle_name = self.lifecycle_name
                # Add DAG namespace if not already present
                if not lifecycle_name.startswith(f"{dag_id}."):
                    lifecycle_name = f"{dag_id}.{lifecycle_name}"
                # Always add .dag suffix for DAG execution lifecycle
                if not lifecycle_name.endswith(".dag"):
                    lifecycle_name = f"{lifecycle_name}.dag"
            else:
                lifecycle_name = f"{dag_id}.dag"

            # Get timeout
            timeout_seconds = self.timeout_seconds
            if timeout_seconds is None:
                # Try to estimate from DAG schedule
                dag = context["dag"]
                dag_run = context.get("dag_run")

                if dag_run and hasattr(dag_run, 'data_interval_start') and hasattr(dag_run, 'data_interval_end'):
                    # Calculate interval from data interval
                    try:
                        timeout_seconds = int((dag_run.data_interval_end - dag_run.data_interval_start).total_seconds())
                    except:
                        timeout_seconds = 3600  # Default 1 hour
                else:
                    timeout_seconds = 3600  # Default 1 hour for manual/unscheduled DAGs

            # Build tags
            tags = {
                "dag_id": context["dag"].dag_id,
                "run_id": context["run_id"],
                "execution_date": context["ds"],
            }
            if self.tags:
                tags.update(self.tags)

            # Get URL
            url = None
            try:
                from airflow.configuration import conf
                webserver_base_url = conf.get("webserver", "base_url", fallback=None)
                if webserver_base_url:
                    url = f"{webserver_base_url}/dags/{context['dag'].dag_id}/grid?dag_run_id={context['run_id']}"
            except:
                pass

            # Ensure lifecycle exists for the execution
            hook.ensure_lifecycle(
                name=lifecycle_name,
                default_timeout_seconds=timeout_seconds,
                description=f"Airflow DAG execution: {context['dag'].dag_id}",
            )

            # Start execution run
            run = hook.start_run(
                lifecycle_name=lifecycle_name,
                timeout_seconds=timeout_seconds,
                tags=tags,
                url=url,
            )

            # For scheduled DAGs, also manage the schedule lifecycle
            if dag.schedule_interval:
                # Remove .dag suffix and add .schedule
                base_name = lifecycle_name[:-4] if lifecycle_name.endswith(".dag") else lifecycle_name
                schedule_lifecycle_name = f"{base_name}.schedule"

                # Ensure schedule lifecycle exists
                hook.ensure_lifecycle(
                    name=schedule_lifecycle_name,
                    default_timeout_seconds=timeout_seconds,
                    description=f"Airflow DAG schedule monitor: {context['dag'].dag_id}",
                )

                # Calculate timeout for next run
                schedule_timeout = timeout_seconds + 300  # Add 5 minute grace period

                # For scheduled DAGs with data intervals, we can be more precise
                if dag_run and hasattr(dag_run, 'data_interval_end'):
                    try:
                        from datetime import datetime
                        now = datetime.utcnow()

                        # If we have a future data_interval_end, that's when the next run should happen
                        if dag_run.data_interval_end > now:
                            time_until_next = int((dag_run.data_interval_end - now).total_seconds())
                            # Add grace period
                            schedule_timeout = time_until_next + 300
                    except:
                        pass  # Use default schedule_timeout

                # Respawn the schedule lifecycle to monitor the next run
                schedule_tags = tags.copy()
                schedule_tags["type"] = "schedule"
                schedule_response = hook.respawn(
                    lifecycle_name=schedule_lifecycle_name,
                    timeout_seconds=schedule_timeout,
                    tags=schedule_tags,
                    url=url,
                    previous_run_resolution="complete",
                )

                self.log.info(f"Respawned schedule lifecycle {schedule_lifecycle_name} for next run")

            # Store run ID for the end operator
            variable_key = f"telomere_dag_run_{context['dag'].dag_id}_{context['run_id']}"
            Variable.set(variable_key, run["id"])

            self.log.info(f"Started Telomere run {run['id']} for DAG lifecycle {lifecycle_name}")

        except Exception as e:
            self.log.error(f"Failed to start Telomere DAG run: {e}")
            if self.fail_on_telomere_error:
                raise


class TelomereDAGEndOperator(BaseOperator):
    """
    Marks the successful completion of a DAG run in Telomere.

    This operator should be placed at the end of your DAG to mark
    the successful completion of the entire DAG execution.

    :param telomere_conn_id: Connection ID for Telomere
    :param fail_on_telomere_error: Whether to fail the task if Telomere operations fail
    :param trigger_rule: Trigger rule for this task (default: none_failed_or_skipped)
    """

    def __init__(
        self,
        telomere_conn_id: str = TelomereHook.default_conn_name,
        fail_on_telomere_error: bool = False,
        trigger_rule: str = "none_failed_or_skipped",
        **kwargs,
    ) -> None:
        # Override trigger rule default to be more appropriate for end operator
        kwargs["trigger_rule"] = trigger_rule
        super().__init__(**kwargs)
        self.telomere_conn_id = telomere_conn_id
        self.fail_on_telomere_error = fail_on_telomere_error

    def execute(self, context: Context) -> Any:
        """End tracking the DAG run."""
        try:
            hook = TelomereHook(self.telomere_conn_id)

            # Get run ID from Variable
            variable_key = f"telomere_dag_run_{context['dag'].dag_id}_{context['run_id']}"
            run_id = Variable.get(variable_key, default_var=None)

            if not run_id:
                self.log.warning("No Telomere run ID found, skipping end operation")
                return

            # End the run
            # Note: For scheduled DAGs using respawn, the next respawn call will
            # automatically complete this run, but we explicitly end it here for
            # clarity and to handle edge cases (e.g., last run before schedule change)
            hook.end_run(run_id, message="DAG completed successfully")

            # Clean up Variable
            Variable.delete(variable_key)

            self.log.info(f"Ended Telomere DAG run {run_id} as completed")

        except Exception as e:
            self.log.error(f"Failed to end Telomere DAG run: {e}")
            if self.fail_on_telomere_error:
                raise


class TelomereDAGFailOperator(BaseOperator):
    """
    Marks a DAG run as failed in Telomere.

    This operator can be used in failure callbacks or as part of
    error handling flows to mark the DAG as failed in Telomere.

    :param telomere_conn_id: Connection ID for Telomere
    :param fail_on_telomere_error: Whether to fail the task if Telomere operations fail
    :param trigger_rule: Trigger rule for this task (default: one_failed)
    """

    def __init__(
        self,
        telomere_conn_id: str = TelomereHook.default_conn_name,
        fail_on_telomere_error: bool = False,
        trigger_rule: str = "one_failed",
        **kwargs,
    ) -> None:
        # Override trigger rule default to trigger on failures
        kwargs["trigger_rule"] = trigger_rule
        super().__init__(**kwargs)
        self.telomere_conn_id = telomere_conn_id
        self.fail_on_telomere_error = fail_on_telomere_error

    def execute(self, context: Context) -> Any:
        """Mark the DAG run as failed."""
        try:
            hook = TelomereHook(self.telomere_conn_id)

            # Get run ID from Variable
            variable_key = f"telomere_dag_run_{context['dag'].dag_id}_{context['run_id']}"
            run_id = Variable.get(variable_key, default_var=None)

            if not run_id:
                self.log.warning("No Telomere run ID found, skipping fail operation")
                return

            # Collect failure information
            dag_run = context["dag_run"]
            failed_tasks = []

            for ti in dag_run.get_task_instances():
                if ti.state == State.FAILED:
                    failed_tasks.append(ti.task_id)

            message = f"DAG failed. Failed tasks: {', '.join(failed_tasks)}" if failed_tasks else "DAG failed"

            # Fail the run
            hook.fail_run(run_id, message=message)

            # Clean up Variable
            Variable.delete(variable_key)

            self.log.info(f"Marked Telomere DAG run {run_id} as failed")

        except Exception as e:
            self.log.error(f"Failed to mark Telomere DAG run as failed: {e}")
            if self.fail_on_telomere_error:
                raise