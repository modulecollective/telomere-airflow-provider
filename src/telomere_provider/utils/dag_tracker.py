"""
Utility for automatic DAG tracking with Telomere.
"""

from __future__ import annotations

from datetime import timedelta

from airflow.sdk import DAG

from telomere_provider.hooks.telomere import TelomereHook
from telomere_provider.operators.dag import (
    TelomereCanaryOperator,
    TelomereDAGStartOperator,
    TelomereFinalizeOperator,
)

START_TASK_ID = "telomere_dag_start"
CANARY_TASK_ID = "telomere_canary"
FINALIZE_TASK_ID = "telomere_finalize"

# Transient worker deaths on the injected tasks re-run them (Layer 1 stays
# fast) instead of falling through to the Telomere run timeout (Layer 2).
INJECTED_TASK_RETRIES = 2
INJECTED_TASK_RETRY_DELAY = timedelta(seconds=30)


def enable_telomere_tracking(
    dag: DAG,
    lifecycle_name: str | None = None,
    track_schedule: bool = True,
    timeout_seconds: int | None = None,
    tags: dict[str, str] | None = None,
    telomere_conn_id: str = TelomereHook.default_conn_name,
    fail_on_telomere_error: bool = False,
) -> None:
    """
    Enable automatic Telomere tracking for a DAG with one line of code.

    Injects three tasks::

        telomere_dag_start >> [roots]; [leaves] >> telomere_canary >> telomere_finalize

    The start task creates the Telomere run (whose timeout is the safety net if
    nothing else ever reports). The canary, with ``trigger_rule="none_failed"``
    over all leaves, executes iff the dag run is succeeding — a failure
    anywhere in the graph (including mid-graph failures that only mark leaves
    ``upstream_failed``) skips it. The finalize task always runs and reports
    completed or failed based on the canary's marker XCom, mirroring Airflow's
    own dag-run final state.

    Call this *after* all tasks have been added to the DAG.

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
    injected_ids = {START_TASK_ID, CANARY_TASK_ID, FINALIZE_TASK_ID}
    collisions = injected_ids.intersection(dag.task_ids)
    if collisions:
        raise ValueError(
            f"DAG '{dag.dag_id}' already has task(s) {sorted(collisions)}; "
            "was enable_telomere_tracking() called twice?"
        )

    # Roots and leaves of the user's graph, before injection
    root_tasks = [task for task in dag.tasks if not task.upstream_task_ids]
    leaf_tasks = [task for task in dag.tasks if not task.downstream_task_ids]

    if not root_tasks or not leaf_tasks:
        raise ValueError(
            f"DAG '{dag.dag_id}' has no tasks; call enable_telomere_tracking() "
            "after all tasks have been added"
        )

    with dag:
        telomere_start = TelomereDAGStartOperator(
            task_id=START_TASK_ID,
            lifecycle_name=lifecycle_name,
            timeout_seconds=timeout_seconds,
            tags=tags,
            track_schedule=track_schedule,
            telomere_conn_id=telomere_conn_id,
            fail_on_telomere_error=fail_on_telomere_error,
            retries=INJECTED_TASK_RETRIES,
            retry_delay=INJECTED_TASK_RETRY_DELAY,
        )

        # NOT an EmptyOperator: Airflow would optimize that to success without
        # executing it, so the marker XCom would never exist.
        telomere_canary = TelomereCanaryOperator(
            task_id=CANARY_TASK_ID,
            trigger_rule="none_failed",
            retries=INJECTED_TASK_RETRIES,
            retry_delay=INJECTED_TASK_RETRY_DELAY,
        )

        telomere_finalize = TelomereFinalizeOperator(
            task_id=FINALIZE_TASK_ID,
            start_task_id=START_TASK_ID,
            canary_task_id=CANARY_TASK_ID,
            telomere_conn_id=telomere_conn_id,
            fail_on_telomere_error=fail_on_telomere_error,
            trigger_rule="all_done",
            retries=INJECTED_TASK_RETRIES,
            retry_delay=INJECTED_TASK_RETRY_DELAY,
        )

        telomere_start >> root_tasks
        leaf_tasks >> telomere_canary >> telomere_finalize

        # Teardown: the finalize task must always run (all_done) yet must not
        # decide the Airflow dag-run state — otherwise a swallowed Telomere
        # error on the sole leaf would mark every dag run successful. As a
        # teardown it is ignored for run-state purposes; the canary (whose
        # none_failed verdict mirrors the leaves) decides instead.
        telomere_finalize.as_teardown()
