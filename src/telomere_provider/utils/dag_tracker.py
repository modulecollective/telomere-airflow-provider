"""
Utility for automatic DAG tracking with Telomere.
"""

from __future__ import annotations

from typing import Dict, Optional

from airflow.models import DAG

from telomere_provider.hooks.telomere import TelomereHook
from telomere_provider.operators.dag import (
    TelomereDAGStartOperator,
    TelomereDAGEndOperator,
    TelomereDAGFailOperator,
)


def enable_telomere_tracking(
    dag: DAG,
    lifecycle_name: Optional[str] = None,
    track_schedule: bool = True,
    timeout_seconds: Optional[int] = None,
    tags: Optional[Dict[str, str]] = None,
    telomere_conn_id: str = TelomereHook.default_conn_name,
    fail_on_telomere_error: bool = False,
) -> None:
    """
    Enable automatic Telomere tracking for a DAG with one line of code.

    This function modifies the DAG by injecting Telomere lifecycle tracking
    operators to track DAG execution lifecycle. Perfect for adding monitoring
    to existing DAGs without rewriting them.

    :param dag: Airflow DAG instance
    :param lifecycle_name: Custom name (default: dag_id)
    :param track_schedule: Monitor scheduled runs (for scheduled DAGs)
    :param timeout_seconds: Override timeout for DAG runs
    :param tags: Additional tags for all runs
    :param telomere_conn_id: Connection ID for Telomere
    :param fail_on_telomere_error: Whether to fail tasks if Telomere errors

    Example:
        dag = DAG("my_existing_dag", ...)
        # ... existing tasks ...
        enable_telomere_tracking(dag)  # That's it!
    """
    # Find root tasks (tasks with no upstream dependencies)
    root_tasks = [task for task in dag.tasks if not task.upstream_task_ids]

    # Find leaf tasks (tasks with no downstream dependencies)
    leaf_tasks = [task for task in dag.tasks if not task.downstream_task_ids]

    if not root_tasks or not leaf_tasks:
        raise ValueError("DAG must have at least one root and one leaf task")

    # Store original tasks before we start modifying the DAG
    # Used to find root and leaf tasks before adding telomere operators
    original_tasks = list(dag.tasks)

    # Create Telomere tracking operators
    with dag:
        # Start operator
        telomere_start = TelomereDAGStartOperator(
            task_id="telomere_dag_start",
            lifecycle_name=lifecycle_name,
            timeout_seconds=timeout_seconds,
            tags=tags,
            telomere_conn_id=telomere_conn_id,
            fail_on_telomere_error=fail_on_telomere_error,
        )

        # End operator (for success)
        telomere_end = TelomereDAGEndOperator(
            task_id="telomere_dag_end",
            telomere_conn_id=telomere_conn_id,
            fail_on_telomere_error=fail_on_telomere_error,
            trigger_rule="none_failed_or_skipped",
        )

        # Fail operator (for failures)
        telomere_fail = TelomereDAGFailOperator(
            task_id="telomere_dag_fail",
            telomere_conn_id=telomere_conn_id,
            fail_on_telomere_error=fail_on_telomere_error,
            trigger_rule="one_failed",
        )

        # Wire up dependencies
        # Start tracking before any root tasks
        telomere_start >> root_tasks

        # End tracking after all leaf tasks succeed
        leaf_tasks >> telomere_end

        # For failure tracking, connect leaf tasks directly to fail operator
        # This will trigger if any upstream task failed
        leaf_tasks >> telomere_fail