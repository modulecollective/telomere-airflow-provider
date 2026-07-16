"""Track opted-in DAG runs from Airflow's scheduler-computed state."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from airflow.exceptions import AirflowException
from airflow.listeners import hookimpl
from airflow.models.dag import DagModel, DagTag
from airflow.utils.session import NEW_SESSION, provide_session
from sqlalchemy import select
from sqlalchemy.orm import Session

from telomere_provider.hooks.telomere import TelomereHook
from telomere_provider.utils.dag_tracker import TELOMERE_DAG_TAG
from telomere_provider.utils.urls import build_airflow_url

DEFAULT_TIMEOUT_SECONDS = 3600
SCHEDULE_GRACE_SECONDS = 300

log = logging.getLogger(__name__)


@provide_session
def _dag_model_metadata(
    dag_id: str,
    *,
    session: Session = NEW_SESSION,
) -> tuple[set[str], datetime | None]:
    """Read the metadata needed when no serialized DAG is attached."""
    tags = set(session.scalars(select(DagTag.name).where(DagTag.dag_id == dag_id)).all())
    next_run = session.scalar(
        select(DagModel.next_dagrun_create_after).where(DagModel.dag_id == dag_id)
    )
    return tags, next_run


def _attached_dag(dag_run: Any) -> Any | None:
    dag = getattr(dag_run, "dag", None)
    if dag is not None:
        return dag
    try:
        return dag_run.get_dag()
    except AirflowException:
        return None


def _is_tracked(dag_run: Any) -> bool:
    dag = _attached_dag(dag_run)
    if dag is not None:
        return TELOMERE_DAG_TAG in dag.tags
    tags, _ = _dag_model_metadata(dag_run.dag_id)
    return TELOMERE_DAG_TAG in tags


def _run_timeout(dag_run: Any, dag: Any) -> int:
    if dag.dagrun_timeout is not None:
        return int(dag.dagrun_timeout.total_seconds())
    if (
        dag_run.data_interval_start is not None
        and dag_run.data_interval_end is not None
        and dag_run.data_interval_end > dag_run.data_interval_start
    ):
        return int((dag_run.data_interval_end - dag_run.data_interval_start).total_seconds())
    return DEFAULT_TIMEOUT_SECONDS


def _run_tags(dag_run: Any) -> dict[str, str]:
    tags = {
        "dag_id": dag_run.dag_id,
        "run_id": dag_run.run_id,
        "run_type": str(dag_run.run_type),
    }
    if dag_run.logical_date is not None:
        tags["logical_date"] = dag_run.logical_date.isoformat()
    return tags


def _schedule_timeout(dag_run: Any, fallback: int, next_run: datetime | None) -> int:
    now = datetime.now(timezone.utc)
    deadline = next_run
    if deadline is None:
        deadline = dag_run.data_interval_end
    if deadline is not None and deadline > now:
        return int((deadline - now).total_seconds()) + SCHEDULE_GRACE_SECONDS
    return fallback + SCHEDULE_GRACE_SECONDS


def _respawn_schedule(
    hook: TelomereHook,
    dag_run: Any,
    timeout_seconds: int,
    tags: dict[str, str],
    url: str | None,
) -> None:
    lifecycle_name = f"{dag_run.dag_id}.schedule"
    hook.ensure_lifecycle(
        name=lifecycle_name,
        default_timeout_seconds=timeout_seconds,
        description=f"Airflow DAG schedule monitor: {dag_run.dag_id}",
    )
    _, next_run = _dag_model_metadata(dag_run.dag_id)
    schedule_tags = {**tags, "type": "schedule"}
    hook.respawn(
        lifecycle_name=lifecycle_name,
        timeout_seconds=_schedule_timeout(dag_run, timeout_seconds, next_run),
        tags=schedule_tags,
        url=url,
        previous_run_resolution="complete",
    )


@hookimpl
def on_dag_run_running(dag_run: Any, msg: str) -> None:
    """Start Telomere execution and schedule runs for an opted-in DAG."""
    dag = dag_run.get_dag()
    if TELOMERE_DAG_TAG not in dag.tags:
        return

    lifecycle_name = f"{dag_run.dag_id}.dag"
    timeout_seconds = _run_timeout(dag_run, dag)
    tags = _run_tags(dag_run)
    url = build_airflow_url(dag_run.dag_id, dag_run.run_id)
    hook = TelomereHook()
    hook.ensure_lifecycle(
        name=lifecycle_name,
        default_timeout_seconds=timeout_seconds,
        description=f"Airflow DAG execution: {dag_run.dag_id}",
    )
    run = hook.start_run(
        lifecycle_name=lifecycle_name,
        timeout_seconds=timeout_seconds,
        tags=tags,
        url=url,
    )
    log.info("Started Telomere run %s for DAG %s", run["id"], dag_run.dag_id)

    if dag.timetable.can_be_scheduled:
        _respawn_schedule(hook, dag_run, timeout_seconds, tags, url)


def _resolve_terminal_run(dag_run: Any, msg: str, *, succeeded: bool) -> None:
    if not _is_tracked(dag_run):
        return

    lifecycle_name = f"{dag_run.dag_id}.dag"
    hook = TelomereHook()
    running = hook.list_runs(lifecycle_name, status="running")
    matches = [run for run in running if run.get("tags", {}).get("run_id") == dag_run.run_id]
    if not matches:
        log.warning("No running Telomere run found for %s/%s", dag_run.dag_id, dag_run.run_id)
        return

    run = max(matches, key=lambda item: item["startedAt"])
    if succeeded:
        hook.end_run(run["id"], message=f"Airflow DAG run succeeded: {msg}")
    else:
        hook.fail_run(run["id"], message=f"Airflow DAG run failed: {msg}")


@hookimpl
def on_dag_run_success(dag_run: Any, msg: str) -> None:
    """Resolve the matching Telomere run from Airflow's success verdict."""
    _resolve_terminal_run(dag_run, msg, succeeded=True)


@hookimpl
def on_dag_run_failed(dag_run: Any, msg: str) -> None:
    """Resolve the matching Telomere run from Airflow's failure verdict."""
    _resolve_terminal_run(dag_run, msg, succeeded=False)
