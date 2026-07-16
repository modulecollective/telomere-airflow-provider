"""Tier 1: TelomereLifecycleOperator (task-level tracking) with a mocked hook."""

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from telomere_provider.operators.telomere import TelomereLifecycleOperator


@pytest.fixture
def hook(monkeypatch):
    instance = MagicMock()
    instance.start_run.return_value = {"id": "tlm-task-run-1"}
    monkeypatch.setattr(
        "telomere_provider.operators.telomere.TelomereHook", MagicMock(return_value=instance)
    )
    return instance


def make_context(dag_id="my_dag", task_id="my_task", execution_timeout=None, try_number=1):
    ti = MagicMock()
    ti.dag_id = dag_id
    ti.task_id = task_id
    ti.run_id = "manual__2026-01-01"
    ti.try_number = try_number
    return {
        "dag": SimpleNamespace(dag_id=dag_id),
        "task": SimpleNamespace(task_id=task_id, execution_timeout=execution_timeout),
        "run_id": "manual__2026-01-01",
        "ti": ti,
    }


class TestLifecycleName:
    def test_defaults_to_dag_and_task_id(self, hook):
        op = TelomereLifecycleOperator(task_id="t", python_callable=lambda: None)
        op.execute(make_context(task_id="t"))
        assert hook.start_run.call_args.kwargs["lifecycle_name"] == "my_dag.t"

    def test_custom_name_namespaced(self, hook):
        op = TelomereLifecycleOperator(
            task_id="t", lifecycle_name="payments", python_callable=lambda: None
        )
        op.execute(make_context())
        assert hook.start_run.call_args.kwargs["lifecycle_name"] == "my_dag.payments"

    def test_callable_name(self, hook):
        op = TelomereLifecycleOperator(
            task_id="t",
            lifecycle_name=lambda **ctx: f"dyn_{ctx['task'].task_id}",
            python_callable=lambda: None,
        )
        op.execute(make_context(task_id="t"))
        assert hook.start_run.call_args.kwargs["lifecycle_name"] == "my_dag.dyn_t"


class TestTimeout:
    def test_explicit(self, hook):
        op = TelomereLifecycleOperator(task_id="t", timeout_seconds=99, python_callable=lambda: 1)
        op.execute(make_context())
        assert hook.start_run.call_args.kwargs["timeout_seconds"] == 99

    def test_from_execution_timeout(self, hook):
        op = TelomereLifecycleOperator(task_id="t", python_callable=lambda: 1)
        op.execute(make_context(execution_timeout=timedelta(minutes=5)))
        assert hook.start_run.call_args.kwargs["timeout_seconds"] == 300

    def test_default_one_hour(self, hook):
        op = TelomereLifecycleOperator(task_id="t", python_callable=lambda: 1)
        op.execute(make_context())
        assert hook.start_run.call_args.kwargs["timeout_seconds"] == 3600

    def test_callable_timeout(self, hook):
        op = TelomereLifecycleOperator(
            task_id="t", timeout_seconds=lambda **ctx: 123, python_callable=lambda: 1
        )
        op.execute(make_context())
        assert hook.start_run.call_args.kwargs["timeout_seconds"] == 123


class TestTags:
    def test_base_tags(self, hook):
        op = TelomereLifecycleOperator(task_id="t", tags={"x": "y"}, python_callable=lambda: 1)
        op.execute(make_context(task_id="t", try_number=2))
        tags = hook.start_run.call_args.kwargs["tags"]
        assert tags == {
            "dag_id": "my_dag",
            "task_id": "t",
            "run_id": "manual__2026-01-01",
            "try_number": "2",
            "x": "y",
        }

    def test_callable_tags(self, hook):
        op = TelomereLifecycleOperator(
            task_id="t", tags=lambda **ctx: {"dyn": "1"}, python_callable=lambda: 1
        )
        op.execute(make_context())
        assert hook.start_run.call_args.kwargs["tags"]["dyn"] == "1"


class TestExecution:
    def test_callable_result_returned_and_run_ended(self, hook):
        op = TelomereLifecycleOperator(task_id="t", python_callable=lambda: "result")
        assert op.execute(make_context()) == "result"
        hook.end_run.assert_called_once()
        assert hook.end_run.call_args.args[0] == "tlm-task-run-1"
        assert not hook.fail_run.called

    def test_callable_with_kwargs_receives_context(self, hook):
        seen = {}

        def fn(**kwargs):
            seen.update(kwargs)
            return "ok"

        op = TelomereLifecycleOperator(
            task_id="t", python_callable=fn, op_kwargs={"extra": "arg"}
        )
        op.execute(make_context())
        assert seen["extra"] == "arg"
        assert "run_id" in seen  # context merged in

    def test_callable_without_kwargs_gets_op_args_only(self, hook):
        def fn(a, b):
            return a + b

        op = TelomereLifecycleOperator(task_id="t", python_callable=fn, op_args=[1, 2])
        assert op.execute(make_context()) == 3

    def test_failure_reports_fail_run_and_reraises(self, hook):
        # Row 18: callable raises -> failed
        def boom():
            raise ValueError("task exploded")

        op = TelomereLifecycleOperator(task_id="t", python_callable=boom)
        with pytest.raises(ValueError, match="task exploded"):
            op.execute(make_context())
        hook.fail_run.assert_called_once()
        assert "task exploded" in hook.fail_run.call_args.kwargs["message"]
        assert not hook.end_run.called

    def test_execution_timeout_reports_fail_run(self, hook):
        # Row 19: AirflowTaskTimeout inherits BaseException, not Exception —
        # it must still be reported as a failed run before propagating.
        from airflow.sdk.exceptions import AirflowTaskTimeout

        def timeout():
            raise AirflowTaskTimeout("timed out")

        op = TelomereLifecycleOperator(task_id="t", python_callable=timeout)
        with pytest.raises(AirflowTaskTimeout):
            op.execute(make_context())
        hook.fail_run.assert_called_once()

    def test_failure_message_truncated(self, hook):
        def boom():
            raise ValueError("x" * 5000)

        op = TelomereLifecycleOperator(task_id="t", python_callable=boom)
        with pytest.raises(ValueError):
            op.execute(make_context())
        assert len(hook.fail_run.call_args.kwargs["message"]) == 1000

    def test_subclass_execute_hook(self, hook):
        class MyOp(TelomereLifecycleOperator):
            def _execute(self, context):
                return "sub"

        assert MyOp(task_id="t").execute(make_context()) == "sub"
        hook.end_run.assert_called_once()

    def test_no_callable_and_no_subclass_raises(self, hook):
        op = TelomereLifecycleOperator(task_id="t")
        with pytest.raises(NotImplementedError):
            op.execute(make_context())


class TestErrorHandling:
    def test_start_failure_swallowed_by_default(self, hook):
        hook.start_run.side_effect = RuntimeError("api down")
        op = TelomereLifecycleOperator(task_id="t", python_callable=lambda: "ok")
        assert op.execute(make_context()) == "ok"

    def test_start_failure_raises_when_flagged(self, hook):
        hook.start_run.side_effect = RuntimeError("api down")
        op = TelomereLifecycleOperator(
            task_id="t", python_callable=lambda: "ok", fail_on_telomere_error=True
        )
        with pytest.raises(RuntimeError, match="api down"):
            op.execute(make_context())

    def test_end_failure_swallowed_by_default(self, hook):
        hook.end_run.side_effect = RuntimeError("api down")
        op = TelomereLifecycleOperator(task_id="t", python_callable=lambda: "ok")
        assert op.execute(make_context()) == "ok"


class TestOnKill:
    def test_on_kill_fails_run(self, hook):
        # Row 20: externally stopped task reports failure instead of dangling
        op = TelomereLifecycleOperator(task_id="t", python_callable=lambda: 1)
        op._run_id = "tlm-task-run-1"
        op.on_kill()
        hook.fail_run.assert_called_once()
        assert hook.fail_run.call_args.args[0] == "tlm-task-run-1"

    def test_on_kill_noop_without_run(self, hook):
        op = TelomereLifecycleOperator(task_id="t", python_callable=lambda: 1)
        op.on_kill()
        assert not hook.fail_run.called

    def test_on_kill_swallows_api_errors(self, hook):
        hook.fail_run.side_effect = RuntimeError("api down")
        op = TelomereLifecycleOperator(task_id="t", python_callable=lambda: 1)
        op._run_id = "r"
        op.on_kill()  # must not raise
