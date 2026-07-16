"""Tier 1: graph shape injected by enable_telomere_tracking."""

import pytest
from airflow.providers.standard.operators.empty import EmptyOperator
from airflow.sdk import DAG

from telomere_provider.operators.dag import (
    TelomereCanaryOperator,
    TelomereDAGStartOperator,
    TelomereFinalizeOperator,
)
from telomere_provider.utils import enable_telomere_tracking
from telomere_provider.utils.dag_tracker import (
    CANARY_TASK_ID,
    FINALIZE_TASK_ID,
    START_TASK_ID,
)


def make_dag(dag_id="tracked"):
    """a >> b >> [c, d]; e isolated. Roots: a, e. Leaves: c, d, e."""
    with DAG(dag_id, schedule=None) as dag:
        a = EmptyOperator(task_id="a")
        b = EmptyOperator(task_id="b")
        c = EmptyOperator(task_id="c")
        d = EmptyOperator(task_id="d")
        EmptyOperator(task_id="e")
        a >> b >> [c, d]
    return dag


class TestGraphShape:
    def test_full_shape(self):
        dag = make_dag()
        enable_telomere_tracking(dag)

        start = dag.get_task(START_TASK_ID)
        canary = dag.get_task(CANARY_TASK_ID)
        finalize = dag.get_task(FINALIZE_TASK_ID)

        # Start feeds every root
        assert set(start.downstream_task_ids) == {"a", "e"}
        # Every leaf feeds the canary
        assert set(canary.upstream_task_ids) == {"c", "d", "e"}
        # Canary feeds finalize; finalize is the sole leaf
        assert set(canary.downstream_task_ids) == {FINALIZE_TASK_ID}
        assert set(finalize.upstream_task_ids) == {CANARY_TASK_ID}

        assert isinstance(start, TelomereDAGStartOperator)
        assert isinstance(canary, TelomereCanaryOperator)
        assert isinstance(finalize, TelomereFinalizeOperator)

    def test_trigger_rules_and_retries(self):
        dag = make_dag()
        enable_telomere_tracking(dag)

        canary = dag.get_task(CANARY_TASK_ID)
        finalize = dag.get_task(FINALIZE_TASK_ID)

        # none_failed over the leaves == Airflow's dag-run success predicate
        assert canary.trigger_rule == "none_failed"
        # Both injected downstream tasks are teardowns: they must not decide
        # the dag-run state (the user's leaves keep that role) and teardowns
        # are exempt from force-skips (default ShortCircuitOperator), which
        # would otherwise skip the canary and cause a false failure report.
        assert canary.is_teardown
        # The canary must keep none_failed, so it is flagged directly rather
        # than via as_teardown() (which rewrites the trigger rule).
        assert finalize.is_teardown
        assert not finalize.on_failure_fail_dagrun
        # as_teardown() rewrote finalize's rule; for a teardown with no setups
        # this behaves as all_done.
        assert finalize.trigger_rule == "all_done_setup_success"

        for task_id in (START_TASK_ID, CANARY_TASK_ID, FINALIZE_TASK_ID):
            assert dag.get_task(task_id).retries == 2

    def test_settings_passed_through(self):
        dag = make_dag()
        enable_telomere_tracking(
            dag,
            lifecycle_name="custom",
            track_schedule=False,
            timeout_seconds=120,
            tags={"team": "x"},
            telomere_conn_id="other_conn",
            fail_on_telomere_error=True,
        )
        start = dag.get_task(START_TASK_ID)
        assert start.lifecycle_name == "custom"
        assert start.track_schedule is False
        assert start.timeout_seconds == 120
        assert start.tags == {"team": "x"}
        assert start.telomere_conn_id == "other_conn"
        assert start.fail_on_telomere_error is True

        finalize = dag.get_task(FINALIZE_TASK_ID)
        assert finalize.telomere_conn_id == "other_conn"
        assert finalize.fail_on_telomere_error is True
        assert finalize.start_task_id == START_TASK_ID
        assert finalize.canary_task_id == CANARY_TASK_ID


class TestValidation:
    def test_double_call_raises(self):
        dag = make_dag()
        enable_telomere_tracking(dag)
        with pytest.raises(ValueError, match="already has task"):
            enable_telomere_tracking(dag)

    def test_empty_dag_raises(self):
        with DAG("empty", schedule=None) as dag:
            pass
        with pytest.raises(ValueError, match="has no tasks"):
            enable_telomere_tracking(dag)

    def test_user_task_colliding_with_injected_id_raises(self):
        with DAG("colliding", schedule=None) as dag:
            EmptyOperator(task_id=CANARY_TASK_ID)
        with pytest.raises(ValueError, match="telomere_canary"):
            enable_telomere_tracking(dag)
