"""Tier 1: listener event handling without an Airflow scheduler."""

from datetime import datetime, timedelta, timezone
from importlib.metadata import entry_points
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from airflow.exceptions import AirflowException
from airflow.listeners.listener import get_listener_manager
from airflow.sdk import DAG
from airflow.serialization import serialized_objects

from telomere_provider import get_provider_info
from telomere_provider.plugins import listener
from telomere_provider.plugins.plugin import TelomerePlugin


def make_dag(*, tracked=True, timeout=None, scheduled=False):
    return SimpleNamespace(
        tags=["telomere"] if tracked else [],
        dagrun_timeout=timeout,
        timetable=SimpleNamespace(can_be_scheduled=scheduled),
    )


def make_dag_run(dag=None, **overrides):
    values = {
        "dag_id": "example",
        "run_id": "manual__1",
        "run_type": "manual",
        "logical_date": datetime(2026, 1, 2, tzinfo=timezone.utc),
        "data_interval_start": datetime(2026, 1, 1, tzinfo=timezone.utc),
        "data_interval_end": datetime(2026, 1, 2, tzinfo=timezone.utc),
    }
    values.update(overrides)
    dag_run = SimpleNamespace(**values)
    dag_run.dag = dag
    dag_run.get_dag = (
        Mock(return_value=dag) if dag is not None else Mock(side_effect=AirflowException)
    )
    return dag_run


@pytest.fixture
def hook(monkeypatch):
    hook = Mock()
    hook.ensure_lifecycle.return_value = {"id": "lifecycle"}
    hook.start_run.return_value = {"id": "run-1"}
    monkeypatch.setattr(listener, "TelomereHook", Mock(return_value=hook))
    monkeypatch.setattr(listener, "build_airflow_url", Mock(return_value="http://airflow/run"))
    return hook


def test_untracked_running_event_costs_no_http(monkeypatch):
    hook_class = Mock()
    monkeypatch.setattr(listener, "TelomereHook", hook_class)

    listener.on_dag_run_running(make_dag_run(make_dag(tracked=False)), "started")

    hook_class.assert_not_called()


@pytest.mark.parametrize(
    ("dag_timeout", "interval_start", "interval_end", "expected"),
    [
        (
            timedelta(seconds=90),
            datetime(2026, 1, 1, tzinfo=timezone.utc),
            datetime(2026, 1, 2, tzinfo=timezone.utc),
            90,
        ),
        (
            None,
            datetime(2026, 1, 1, tzinfo=timezone.utc),
            datetime(2026, 1, 2, tzinfo=timezone.utc),
            86400,
        ),
        (None, None, None, 3600),
    ],
)
def test_running_event_timeout_precedence(
    hook, dag_timeout, interval_start, interval_end, expected
):
    dag_run = make_dag_run(
        make_dag(timeout=dag_timeout),
        data_interval_start=interval_start,
        data_interval_end=interval_end,
    )

    listener.on_dag_run_running(dag_run, "started")

    hook.ensure_lifecycle.assert_called_once_with(
        name="example.dag",
        default_timeout_seconds=expected,
        description="Airflow DAG execution: example",
    )
    hook.start_run.assert_called_once_with(
        lifecycle_name="example.dag",
        timeout_seconds=expected,
        tags={
            "dag_id": "example",
            "run_id": "manual__1",
            "run_type": "manual",
            "logical_date": "2026-01-02T00:00:00+00:00",
        },
        url="http://airflow/run",
    )


def test_running_event_respawns_schedule_from_airflow_deadline(hook, monkeypatch):
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(
        listener,
        "_dag_model_metadata",
        Mock(return_value=({"telomere"}, now + timedelta(seconds=120))),
    )
    dag_run = make_dag_run(make_dag(scheduled=True, timeout=timedelta(seconds=60)))

    listener.on_dag_run_running(dag_run, "started")

    assert hook.ensure_lifecycle.call_args_list[1].kwargs["name"] == "example.schedule"
    respawn = hook.respawn.call_args.kwargs
    assert 418 <= respawn["timeout_seconds"] <= 420
    assert respawn["tags"]["type"] == "schedule"
    assert respawn["previous_run_resolution"] == "complete"


def test_schedule_timeout_falls_back_to_interval_then_run_timeout():
    now = datetime.now(timezone.utc)
    dag_run = make_dag_run(data_interval_end=now + timedelta(seconds=60))
    assert 358 <= listener._schedule_timeout(dag_run, 100, None) <= 360

    dag_run.data_interval_end = now - timedelta(seconds=1)
    assert listener._schedule_timeout(dag_run, 100, None) == 400


@pytest.mark.parametrize(("succeeded", "action"), [(True, "end_run"), (False, "fail_run")])
def test_terminal_event_correlates_by_run_id_and_chooses_newest(
    hook, monkeypatch, succeeded, action
):
    monkeypatch.setattr(listener, "_is_tracked", Mock(return_value=True))
    hook.list_runs.return_value = [
        {"id": "other", "startedAt": "2026-01-04T00:00:00Z", "tags": {"run_id": "x"}},
        {"id": "old", "startedAt": "2026-01-01T00:00:00Z", "tags": {"run_id": "manual__1"}},
        {"id": "new", "startedAt": "2026-01-03T00:00:00Z", "tags": {"run_id": "manual__1"}},
    ]
    dag_run = make_dag_run(make_dag())

    listener._resolve_terminal_run(dag_run, "reason", succeeded=succeeded)

    hook.list_runs.assert_called_once_with("example.dag", status="running")
    getattr(hook, action).assert_called_once_with(
        "new", message=f"Airflow DAG run {'succeeded' if succeeded else 'failed'}: reason"
    )


def test_terminal_event_without_match_warns_and_returns(hook, monkeypatch, caplog):
    monkeypatch.setattr(listener, "_is_tracked", Mock(return_value=True))
    hook.list_runs.return_value = []

    listener.on_dag_run_failed(make_dag_run(make_dag()), "task_failure")

    assert "No running Telomere run found" in caplog.text
    hook.fail_run.assert_not_called()


def test_terminal_event_without_attached_dag_uses_model_tags(monkeypatch):
    monkeypatch.setattr(listener, "_dag_model_metadata", Mock(return_value=({"telomere"}, None)))

    assert listener._is_tracked(make_dag_run())


def test_terminal_event_for_untracked_dag_costs_no_http(monkeypatch):
    hook_class = Mock()
    monkeypatch.setattr(listener, "TelomereHook", hook_class)

    listener.on_dag_run_success(make_dag_run(make_dag(tracked=False)), "success")

    hook_class.assert_not_called()


def test_airflow_listener_manager_dispatches_running_event(hook):
    get_listener_manager().hook.on_dag_run_running(dag_run=make_dag_run(make_dag()), msg="started")

    hook.start_run.assert_called_once()


def test_provider_registers_listener_plugin():
    assert "plugins" not in get_provider_info()
    assert TelomerePlugin.listeners == [listener]
    plugin_entrypoints = {
        entry.name: entry.value for entry in entry_points(group="airflow.plugins")
    }
    assert plugin_entrypoints["telomere"] == "telomere_provider.plugins.plugin:TelomerePlugin"
    assert listener in get_listener_manager().pm.get_plugins()


def test_telomere_tag_survives_dag_serialization():
    dag = DAG("serialized", schedule=None, tags=["telomere"])
    serializer = getattr(serialized_objects, "DagSerialization", serialized_objects.SerializedDAG)

    assert "telomere" in serializer.serialize_dag(dag)["tags"]
