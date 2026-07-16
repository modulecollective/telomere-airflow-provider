"""
Telomere operators for DAG-level lifecycle tracking.

The tracking shape injected by ``enable_telomere_tracking`` is::

    telomere_dag_start >> [roots]; [leaves] >> telomere_canary >> telomere_finalize

- The start operator creates the Telomere run (with a timeout — the Layer 2
  safety net) and pushes its ID as an XCom.
- The canary runs iff the dag run is succeeding: ``none_failed`` over the
  leaves is exactly Airflow's dag-run success predicate, and upstream_failed /
  failed leaves propagate a skip to it. Its XCom marker is the verdict.
- The finalize operator (``all_done``, so it always runs) reports completed or
  failed based on the canary marker.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from airflow.sdk import BaseOperator, Context

from telomere_provider.hooks.telomere import TelomereHook
from telomere_provider.utils.urls import build_airflow_url

DEFAULT_TIMEOUT_SECONDS = 3600
SCHEDULE_GRACE_SECONDS = 300


class TelomereDAGStartOperator(BaseOperator):
    """
    Marks the start of a DAG run in Telomere.

    This operator should be placed at the beginning of your DAG (upstream of
    all roots) to start tracking the entire DAG execution as a Telomere
    lifecycle. It pushes the Telomere run ID as its return-value XCom for the
    finalize operator to pull. For scheduled DAGs, it also manages a separate
    schedule lifecycle to monitor that runs start on time.

    :param lifecycle_name: Name for the lifecycle (default: dag_id)
    :param timeout_seconds: Timeout for the entire DAG run (defaults to the
        data interval length for scheduled DAGs, else 1 hour). This timeout is
        the safety net: if the run is never explicitly ended, Telomere alerts
        when it expires — so it should genuinely bound the DAG's duration.
    :param tags: Additional tags for the run
    :param track_schedule: Also maintain a `.schedule` lifecycle that alerts
        if the next scheduled run does not start on time (scheduled DAGs only)
    :param telomere_conn_id: Connection ID for Telomere
    :param fail_on_telomere_error: Whether to fail the task if Telomere operations fail
    """

    template_fields = ["lifecycle_name", "tags", "timeout_seconds"]
    template_fields_renderers = {"tags": "json"}

    def __init__(
        self,
        lifecycle_name: str | None = None,
        timeout_seconds: int | None = None,
        tags: dict[str, str] | None = None,
        track_schedule: bool = True,
        telomere_conn_id: str = TelomereHook.default_conn_name,
        fail_on_telomere_error: bool = False,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.lifecycle_name = lifecycle_name
        self.timeout_seconds = timeout_seconds
        self.tags = tags
        self.track_schedule = track_schedule
        self.telomere_conn_id = telomere_conn_id
        self.fail_on_telomere_error = fail_on_telomere_error

    def _resolve_lifecycle_name(self, dag_id: str) -> str:
        """Namespace the lifecycle name with the DAG ID and a .dag suffix."""
        if not self.lifecycle_name:
            return f"{dag_id}.dag"
        name = self.lifecycle_name
        if not name.startswith(f"{dag_id}."):
            name = f"{dag_id}.{name}"
        if not name.endswith(".dag"):
            name = f"{name}.dag"
        return name

    def _derive_timeout(self, dag_run: Any) -> int:
        """Timeout for the run: explicit, else data interval length, else 1h."""
        if self.timeout_seconds is not None:
            return int(self.timeout_seconds)
        interval_start = getattr(dag_run, "data_interval_start", None)
        interval_end = getattr(dag_run, "data_interval_end", None)
        if interval_start and interval_end and interval_end > interval_start:
            return int((interval_end - interval_start).total_seconds())
        return DEFAULT_TIMEOUT_SECONDS

    def _build_tags(self, context: Context) -> dict[str, str]:
        tags = {
            "dag_id": context["dag"].dag_id,
            "run_id": context["run_id"],
        }
        # logical_date can be None for manually/asset-triggered runs in Airflow 3
        logical_date = context.get("logical_date")
        if logical_date:
            tags["logical_date"] = logical_date.isoformat()
        if self.tags:
            tags.update(self.tags)
        return tags

    def _respawn_schedule_lifecycle(
        self,
        hook: TelomereHook,
        lifecycle_name: str,
        timeout_seconds: int,
        tags: dict[str, str],
        url: str | None,
        dag_run: Any,
    ) -> None:
        """
        Respawn the `.schedule` deadman lifecycle.

        Every start of a scheduled DAG completes the previous schedule run and
        opens a new one whose timeout is the deadline for the *next* run to
        start. If the DAG stops running entirely (paused, parse error,
        scheduler down), the last deadline fires and alerts.
        """
        base_name = lifecycle_name[:-4] if lifecycle_name.endswith(".dag") else lifecycle_name
        schedule_lifecycle_name = f"{base_name}.schedule"

        hook.ensure_lifecycle(
            name=schedule_lifecycle_name,
            default_timeout_seconds=timeout_seconds,
            description=f"Airflow DAG schedule monitor: {tags['dag_id']}",
        )

        # Fallback: assume the next run starts within one interval + grace
        schedule_timeout = timeout_seconds + SCHEDULE_GRACE_SECONDS

        # For runs with a future data_interval_end, that's when the next run
        # should be created — use it as a precise deadline.
        interval_end = getattr(dag_run, "data_interval_end", None)
        if interval_end is not None:
            now = datetime.now(timezone.utc)
            if interval_end > now:
                schedule_timeout = (
                    int((interval_end - now).total_seconds()) + SCHEDULE_GRACE_SECONDS
                )

        schedule_tags = dict(tags)
        schedule_tags["type"] = "schedule"
        hook.respawn(
            lifecycle_name=schedule_lifecycle_name,
            timeout_seconds=schedule_timeout,
            tags=schedule_tags,
            url=url,
            previous_run_resolution="complete",
        )
        self.log.info("Respawned schedule lifecycle %s for next run", schedule_lifecycle_name)

    def execute(self, context: Context) -> str | None:
        """Start tracking the DAG run. Returns the Telomere run ID (as XCom)."""
        dag = context["dag"]
        dag_run = context.get("dag_run")
        lifecycle_name = self._resolve_lifecycle_name(dag.dag_id)
        timeout_seconds = self._derive_timeout(dag_run)
        tags = self._build_tags(context)
        url = build_airflow_url(dag.dag_id, context["run_id"])

        try:
            hook = TelomereHook(self.telomere_conn_id)
            hook.ensure_lifecycle(
                name=lifecycle_name,
                default_timeout_seconds=timeout_seconds,
                description=f"Airflow DAG execution: {dag.dag_id}",
            )
            run = hook.start_run(
                lifecycle_name=lifecycle_name,
                timeout_seconds=timeout_seconds,
                tags=tags,
                url=url,
            )
            run_id = run["id"]
            self.log.info("Started Telomere run %s for DAG lifecycle %s", run_id, lifecycle_name)
        except Exception as e:
            self.log.error("Failed to start Telomere DAG run: %s", e)
            if self.fail_on_telomere_error:
                raise
            return None

        # The schedule lifecycle is best-effort on top of the already-started
        # run: a failure here must not lose the run ID XCom.
        if self.track_schedule and dag.timetable.can_be_scheduled:
            try:
                self._respawn_schedule_lifecycle(
                    hook, lifecycle_name, timeout_seconds, tags, url, dag_run
                )
            except Exception as e:
                self.log.error("Failed to respawn Telomere schedule lifecycle: %s", e)
                if self.fail_on_telomere_error:
                    raise

        return run_id


class TelomereCanaryOperator(BaseOperator):
    """
    No-op task whose *execution* is the dag-run verdict.

    Wired downstream of all leaves with ``trigger_rule="none_failed"``: it runs
    (and pushes a marker XCom) iff every leaf ended success or skipped — which
    is exactly Airflow's dag-run success predicate. If any leaf failed or was
    upstream_failed, skip propagation means this task never runs and no marker
    exists.

    This must NOT be an EmptyOperator: Airflow optimizes EmptyOperator to
    success without executing it, so no XCom would ever be pushed and every
    run would be reported as failed.
    """

    def execute(self, context: Context) -> str:
        return "dag_run_succeeded"


class TelomereFinalizeOperator(BaseOperator):
    """
    Reports the dag run's final state to Telomere.

    Wired downstream of the canary with ``trigger_rule="all_done"`` so it
    always runs. Pulls the Telomere run ID from the start operator's XCom and
    the canary marker: marker present means the run succeeded (``end_run``),
    absent means it failed (``fail_run``).

    :param start_task_id: Task ID of the TelomereDAGStartOperator
    :param canary_task_id: Task ID of the TelomereCanaryOperator
    :param telomere_conn_id: Connection ID for Telomere
    :param fail_on_telomere_error: Whether to fail the task if Telomere operations fail
    """

    def __init__(
        self,
        start_task_id: str = "telomere_dag_start",
        canary_task_id: str = "telomere_canary",
        telomere_conn_id: str = TelomereHook.default_conn_name,
        fail_on_telomere_error: bool = False,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.start_task_id = start_task_id
        self.canary_task_id = canary_task_id
        self.telomere_conn_id = telomere_conn_id
        self.fail_on_telomere_error = fail_on_telomere_error

    def execute(self, context: Context) -> None:
        """Report completed/failed for the tracked run."""
        ti = context["ti"]
        run_id = ti.xcom_pull(task_ids=self.start_task_id, key="return_value")
        if not run_id:
            # Row 9: Telomere was unreachable at start; there is no run to
            # resolve. Scheduled DAGs are still covered by the .schedule
            # lifecycle's deadline.
            self.log.warning("No Telomere run ID found, skipping finalize")
            return

        marker = ti.xcom_pull(task_ids=self.canary_task_id, key="return_value")
        dag_id = context["dag"].dag_id
        url = build_airflow_url(dag_id, context["run_id"])

        try:
            hook = TelomereHook(self.telomere_conn_id)
            if marker:
                hook.end_run(run_id, message="DAG run completed successfully")
                self.log.info("Ended Telomere run %s as completed", run_id)
            else:
                message = "DAG run failed"
                if url:
                    message = f"{message}: {url}"
                hook.fail_run(run_id, message=message)
                self.log.info("Marked Telomere run %s as failed", run_id)
        except Exception as e:
            self.log.error("Failed to finalize Telomere DAG run: %s", e)
            if self.fail_on_telomere_error:
                raise
