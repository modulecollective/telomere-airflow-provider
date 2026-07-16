"""
Tier 2: the failure-mode matrix, end to end through Airflow's own machinery.

Each test is one row of the failure-mode matrix: build the row's DAG shape,
induce the row's failure, run it through dag.test() (real trigger-rule
engine, real skip propagation, real retries — only the Telomere HTTP layer is
faked), and assert the exact Telomere resolutions AND that they mirror
Airflow's own dag-run final state.

The invariant every row protects: Telomere never reports "completed" for a
dag run whose Airflow final state is failed. Anything that prevents explicit
reporting must leave the run dangling (timeout alert), never resolve it
wrongly.
"""

import importlib.util
from pathlib import Path
from unittest.mock import patch

from airflow.models.dagrun import DagRun
from airflow.utils.state import DagRunState, TaskInstanceState

_DAGS_PATH = Path(__file__).parent / "dags" / "matrix_dags.py"
_spec = importlib.util.spec_from_file_location("matrix_dags", _DAGS_PATH)
dags = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dags)


def ti_states(dr):
    return {ti.task_id: ti.state for ti in dr.get_task_instances()}


def run_dag(dag):
    """Run dag.test(), supplying the scheduler transition it bypasses."""
    real_update_state = DagRun.update_state
    notified_running = False

    def update_state(dag_run, *args, **kwargs):
        nonlocal notified_running
        if not notified_running:
            notified_running = True
            dag_run.notify_dagrun_state_changed("started")
        return real_update_state(dag_run, *args, **kwargs)

    with patch.object(DagRun, "update_state", update_state):
        return dag.test()


class TestLayer1Explicit:
    def test_row1_all_tasks_succeed(self, telomere):
        dr = run_dag(dags.dag_success)
        assert dr.state == DagRunState.SUCCESS
        assert telomere.resolutions() == [("end", "tlm-run-1")]

    def test_row2_leaf_task_fails(self, telomere):
        dr = run_dag(dags.dag_leaf_fails)
        assert dr.state == DagRunState.FAILED
        assert telomere.resolutions() == [("fail", "tlm-run-1")]

    def test_row3_midgraph_task_fails(self, telomere):
        # 0.0.1 missed this: the leaf goes upstream_failed (not failed), so a
        # one_failed fail-operator on the leaves never fired.
        dr = run_dag(dags.dag_midgraph_fails)
        assert dr.state == DagRunState.FAILED
        states = ti_states(dr)
        assert states["c"] == TaskInstanceState.UPSTREAM_FAILED
        assert telomere.resolutions() == [("fail", "tlm-run-1")]

    def test_row4_branch_skips_tolerated(self, telomere):
        dr = run_dag(dags.dag_branch_skip)
        assert dr.state == DagRunState.SUCCESS
        states = ti_states(dr)
        assert states["right"] == TaskInstanceState.SKIPPED
        assert telomere.resolutions() == [("end", "tlm-run-1")]

    def test_row5_all_leaves_skipped(self, telomere):
        # Fully short-circuited run is an Airflow success; must not alert.
        dr = run_dag(dags.dag_all_skipped)
        assert dr.state == DagRunState.SUCCESS
        assert ti_states(dr)["t"] == TaskInstanceState.SKIPPED
        assert telomere.resolutions() == [("end", "tlm-run-1")]

    def test_row5b_force_skip_still_reports_completed(self, telomere):
        # A default ShortCircuitOperator force-skips all downstream tasks.
        # Airflow still calls the DAG run successful, and the listener reports
        # that scheduler-computed verdict directly.
        dr = run_dag(dags.dag_force_skip)
        assert dr.state == DagRunState.SUCCESS
        assert ti_states(dr)["t"] == TaskInstanceState.SKIPPED
        assert telomere.resolutions() == [("end", "tlm-run-1")]

    def test_row6_recovery_leaf_mirrors_airflow_success(self, telomere):
        # Mid-graph failure + all_done recovery leaf that succeeds: Airflow
        # calls the run a success, so must Telomere (operator-confirmed:
        # recovered runs must not alert).
        dr = run_dag(dags.dag_recovery_leaf)
        assert dr.state == DagRunState.SUCCESS
        states = ti_states(dr)
        assert states["a"] == TaskInstanceState.FAILED
        assert states["recover"] == TaskInstanceState.SUCCESS
        assert telomere.resolutions() == [("end", "tlm-run-1")]

    def test_row7_task_retry_success_no_premature_fail(self, telomere):
        # Trigger rules see final states only: a fail-then-retry-success run
        # is completed, with no fail call in between.
        dr = run_dag(dags.dag_task_retry)
        assert dr.state == DagRunState.SUCCESS
        assert telomere.resolutions() == [("end", "tlm-run-1")]

    def test_row8_mapped_instance_fails(self, telomere):
        dr = run_dag(dags.dag_mapped_fail)
        assert dr.state == DagRunState.FAILED
        assert telomere.resolutions() == [("fail", "tlm-run-1")]

    def test_row9_api_down_at_start(self, telomere):
        # No run was ever started, so there is nothing to resolve: the
        # terminal listener must no-op, not invent a report. Scheduled DAGs stay
        # covered by the .schedule deadline; the manual-run gap is documented.
        telomere.start_down()
        dr = run_dag(dags.dag_success)
        assert dr.state == DagRunState.SUCCESS  # tracking failure never fails the DAG
        assert telomere.resolutions() == []

    def test_row10_api_down_at_terminal_event(self, telomere):
        # The report attempt errors; the run dangles server-side and the
        # Telomere timeout (Layer 2) raises the alert. Locally we assert no
        # resolution succeeded and the DAG itself was not failed by it.
        telomere.resolve_down()
        dr = run_dag(dags.dag_success)
        assert dr.state == DagRunState.SUCCESS
        # end was attempted (and blew up) — but never succeeded
        assert telomere.attempted_resolutions() == [("end", "tlm-run-1")]
        assert telomere.resolutions() == []
