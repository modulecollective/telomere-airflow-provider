"""Tier 1: DAG-level operators with hand-built contexts and a mocked hook."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from telomere_provider.operators.dag import (
    TelomereCanaryOperator,
    TelomereDAGStartOperator,
    TelomereFinalizeOperator,
)


@pytest.fixture
def hook(monkeypatch):
    """Replace TelomereHook in the operator module; return the instance mock."""
    instance = MagicMock()
    instance.start_run.return_value = {"id": "tlm-run-1"}
    monkeypatch.setattr(
        "telomere_provider.operators.dag.TelomereHook", MagicMock(return_value=instance)
    )
    return instance


def make_context(
    dag_id="my_dag",
    run_id="manual__2026-01-01",
    can_be_scheduled=False,
    data_interval=None,
    logical_date=None,
    ti=None,
):
    dag_run = SimpleNamespace(
        data_interval_start=data_interval[0] if data_interval else None,
        data_interval_end=data_interval[1] if data_interval else None,
    )
    context = {
        "dag": SimpleNamespace(
            dag_id=dag_id, timetable=SimpleNamespace(can_be_scheduled=can_be_scheduled)
        ),
        "dag_run": dag_run,
        "run_id": run_id,
        "logical_date": logical_date,
        "ti": ti or MagicMock(),
    }
    return context


class TestStartOperatorLifecycleName:
    @pytest.mark.parametrize(
        "given, expected",
        [
            (None, "my_dag.dag"),
            ("etl", "my_dag.etl.dag"),
            ("my_dag.etl", "my_dag.etl.dag"),
            ("my_dag.etl.dag", "my_dag.etl.dag"),
        ],
    )
    def test_normalization(self, hook, given, expected):
        op = TelomereDAGStartOperator(task_id="t", lifecycle_name=given)
        op.execute(make_context())
        assert hook.start_run.call_args.kwargs["lifecycle_name"] == expected


class TestStartOperatorTimeout:
    def test_explicit_timeout_wins(self, hook):
        op = TelomereDAGStartOperator(task_id="t", timeout_seconds=42)
        interval = (
            datetime(2026, 1, 1, tzinfo=timezone.utc),
            datetime(2026, 1, 1, 2, tzinfo=timezone.utc),
        )
        op.execute(make_context(data_interval=interval))
        assert hook.start_run.call_args.kwargs["timeout_seconds"] == 42

    def test_data_interval_length(self, hook):
        op = TelomereDAGStartOperator(task_id="t")
        interval = (
            datetime(2026, 1, 1, tzinfo=timezone.utc),
            datetime(2026, 1, 1, 2, tzinfo=timezone.utc),
        )
        op.execute(make_context(data_interval=interval))
        assert hook.start_run.call_args.kwargs["timeout_seconds"] == 7200

    def test_manual_run_defaults_to_one_hour(self, hook):
        op = TelomereDAGStartOperator(task_id="t")
        op.execute(make_context())
        assert hook.start_run.call_args.kwargs["timeout_seconds"] == 3600


class TestStartOperatorTags:
    def test_base_tags_with_logical_date(self, hook):
        op = TelomereDAGStartOperator(task_id="t", tags={"team": "data"})
        logical_date = datetime(2026, 1, 5, tzinfo=timezone.utc)
        op.execute(make_context(logical_date=logical_date))
        tags = hook.start_run.call_args.kwargs["tags"]
        assert tags["dag_id"] == "my_dag"
        assert tags["run_id"] == "manual__2026-01-01"
        assert tags["logical_date"] == "2026-01-05T00:00:00+00:00"
        assert tags["team"] == "data"

    def test_logical_date_omitted_when_none(self, hook):
        # Manually/asset-triggered runs can have logical_date=None in Airflow 3
        op = TelomereDAGStartOperator(task_id="t")
        op.execute(make_context(logical_date=None))
        assert "logical_date" not in hook.start_run.call_args.kwargs["tags"]


class TestStartOperatorRunHandling:
    def test_returns_run_id_as_xcom(self, hook):
        op = TelomereDAGStartOperator(task_id="t")
        assert op.execute(make_context()) == "tlm-run-1"

    def test_start_error_swallowed_by_default(self, hook):
        hook.ensure_lifecycle.side_effect = RuntimeError("api down")
        op = TelomereDAGStartOperator(task_id="t")
        assert op.execute(make_context()) is None

    def test_start_error_raises_when_flagged(self, hook):
        hook.ensure_lifecycle.side_effect = RuntimeError("api down")
        op = TelomereDAGStartOperator(task_id="t", fail_on_telomere_error=True)
        with pytest.raises(RuntimeError, match="api down"):
            op.execute(make_context())


class TestStartOperatorScheduleLifecycle:
    def test_respawns_for_scheduled_dag(self, hook):
        op = TelomereDAGStartOperator(task_id="t")
        op.execute(make_context(can_be_scheduled=True))
        assert hook.respawn.called
        kwargs = hook.respawn.call_args.kwargs
        assert kwargs["lifecycle_name"] == "my_dag.schedule"
        assert kwargs["tags"]["type"] == "schedule"
        assert kwargs["previous_run_resolution"] == "complete"

    def test_no_respawn_for_unscheduled_dag(self, hook):
        op = TelomereDAGStartOperator(task_id="t")
        op.execute(make_context(can_be_scheduled=False))
        assert not hook.respawn.called

    def test_no_respawn_when_track_schedule_off(self, hook):
        op = TelomereDAGStartOperator(task_id="t", track_schedule=False)
        op.execute(make_context(can_be_scheduled=True))
        assert not hook.respawn.called

    def test_deadline_from_future_data_interval_end(self, hook):
        # Next run is due at data_interval_end; deadline = time until then + grace
        now = datetime.now(timezone.utc)
        interval = (now - timedelta(hours=1), now + timedelta(hours=1))
        op = TelomereDAGStartOperator(task_id="t")
        op.execute(make_context(can_be_scheduled=True, data_interval=interval))
        deadline = hook.respawn.call_args.kwargs["timeout_seconds"]
        assert 3600 + 240 <= deadline <= 3600 + 300

    def test_deadline_fallback_for_past_interval(self, hook):
        # data_interval_end in the past (catchup): one run-timeout + grace
        now = datetime.now(timezone.utc)
        interval = (now - timedelta(hours=3), now - timedelta(hours=1))
        op = TelomereDAGStartOperator(task_id="t")
        op.execute(make_context(can_be_scheduled=True, data_interval=interval))
        assert hook.respawn.call_args.kwargs["timeout_seconds"] == 7200 + 300

    def test_respawn_failure_does_not_lose_run_id(self, hook):
        # The run is already started; a schedule-lifecycle hiccup must not
        # prevent the run-ID XCom from being pushed.
        hook.respawn.side_effect = RuntimeError("api down")
        op = TelomereDAGStartOperator(task_id="t")
        assert op.execute(make_context(can_be_scheduled=True)) == "tlm-run-1"

    def test_respawn_failure_raises_when_flagged(self, hook):
        hook.respawn.side_effect = RuntimeError("api down")
        op = TelomereDAGStartOperator(task_id="t", fail_on_telomere_error=True)
        with pytest.raises(RuntimeError, match="api down"):
            op.execute(make_context(can_be_scheduled=True))


class TestCanaryOperator:
    def test_returns_truthy_marker(self):
        op = TelomereCanaryOperator(task_id="c")
        assert op.execute({})

    def test_is_not_an_empty_operator(self):
        # Airflow optimizes EmptyOperator to success WITHOUT executing it — an
        # EmptyOperator canary would never push its marker XCom and every run
        # would be reported failed. Pin that the canary stays a real operator.
        from airflow.providers.standard.operators.empty import EmptyOperator

        assert not isinstance(TelomereCanaryOperator(task_id="c"), EmptyOperator)
        assert TelomereCanaryOperator.execute is not EmptyOperator.execute


class TestFinalizeOperator:
    def make_finalize_context(self, run_id="tlm-run-1", marker="dag_run_succeeded"):
        def xcom_pull(task_ids=None, key="return_value"):
            return {"telomere_dag_start": run_id, "telomere_canary": marker}.get(task_ids)

        ti = MagicMock()
        ti.xcom_pull.side_effect = xcom_pull
        return make_context(ti=ti)

    def test_marker_present_ends_run(self, hook):
        op = TelomereFinalizeOperator(task_id="f")
        op.execute(self.make_finalize_context())
        hook.end_run.assert_called_once()
        assert hook.end_run.call_args.args[0] == "tlm-run-1"
        assert not hook.fail_run.called

    def test_marker_absent_fails_run(self, hook):
        op = TelomereFinalizeOperator(task_id="f")
        op.execute(self.make_finalize_context(marker=None))
        hook.fail_run.assert_called_once()
        assert hook.fail_run.call_args.args[0] == "tlm-run-1"
        assert "DAG run failed" in hook.fail_run.call_args.kwargs["message"]
        assert not hook.end_run.called

    def test_no_run_id_is_noop(self, hook):
        # Row 9: Telomere was down at start; nothing to resolve.
        op = TelomereFinalizeOperator(task_id="f")
        op.execute(self.make_finalize_context(run_id=None))
        assert not hook.end_run.called
        assert not hook.fail_run.called

    def test_api_error_swallowed_by_default(self, hook):
        hook.end_run.side_effect = RuntimeError("api down")
        op = TelomereFinalizeOperator(task_id="f")
        op.execute(self.make_finalize_context())  # must not raise

    def test_api_error_raises_when_flagged(self, hook):
        hook.end_run.side_effect = RuntimeError("api down")
        op = TelomereFinalizeOperator(task_id="f", fail_on_telomere_error=True)
        with pytest.raises(RuntimeError, match="api down"):
            op.execute(self.make_finalize_context())

    def test_custom_task_ids(self, hook):
        def xcom_pull(task_ids=None, key="return_value"):
            return {"my_start": "tlm-run-9", "my_canary": "ok"}.get(task_ids)

        ti = MagicMock()
        ti.xcom_pull.side_effect = xcom_pull
        op = TelomereFinalizeOperator(task_id="f", start_task_id="my_start", canary_task_id="my_canary")
        op.execute(make_context(ti=ti))
        assert hook.end_run.call_args.args[0] == "tlm-run-9"
