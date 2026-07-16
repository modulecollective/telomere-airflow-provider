"""
Tier 2: the failure-mode matrix, end to end through Airflow's own machinery.

Each test is one row of the matrix in the README: build the row's DAG shape,
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

from airflow.utils.state import DagRunState, TaskInstanceState

from telomere_provider.operators.dag import TelomereFinalizeOperator

_DAGS_PATH = Path(__file__).parent / "dags" / "matrix_dags.py"
_spec = importlib.util.spec_from_file_location("matrix_dags", _DAGS_PATH)
dags = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dags)


def ti_states(dr):
    return {ti.task_id: ti.state for ti in dr.get_task_instances()}


class TestLayer1Explicit:
    def test_row1_all_tasks_succeed(self, telomere):
        dr = dags.dag_success.test()
        assert dr.state == DagRunState.SUCCESS
        assert telomere.resolutions() == [("end", "tlm-run-1")]

    def test_row2_leaf_task_fails(self, telomere):
        dr = dags.dag_leaf_fails.test()
        assert dr.state == DagRunState.FAILED
        assert telomere.resolutions() == [("fail", "tlm-run-1")]

    def test_row3_midgraph_task_fails(self, telomere):
        # 0.0.1 missed this: the leaf goes upstream_failed (not failed), so a
        # one_failed fail-operator on the leaves never fired.
        dr = dags.dag_midgraph_fails.test()
        assert dr.state == DagRunState.FAILED
        states = ti_states(dr)
        assert states["c"] == TaskInstanceState.UPSTREAM_FAILED
        assert states["telomere_canary"] == TaskInstanceState.UPSTREAM_FAILED
        assert states["telomere_finalize"] == TaskInstanceState.SUCCESS
        assert telomere.resolutions() == [("fail", "tlm-run-1")]

    def test_row4_branch_skips_tolerated(self, telomere):
        dr = dags.dag_branch_skip.test()
        assert dr.state == DagRunState.SUCCESS
        states = ti_states(dr)
        assert states["right"] == TaskInstanceState.SKIPPED
        assert telomere.resolutions() == [("end", "tlm-run-1")]

    def test_row5_all_leaves_skipped(self, telomere):
        # Fully short-circuited run is an Airflow success; must not alert.
        dr = dags.dag_all_skipped.test()
        assert dr.state == DagRunState.SUCCESS
        states = ti_states(dr)
        assert states["t"] == TaskInstanceState.SKIPPED
        assert states["telomere_canary"] == TaskInstanceState.SUCCESS
        assert telomere.resolutions() == [("end", "tlm-run-1")]

    def test_row5b_force_skip_still_reports_completed(self, telomere):
        # A default ShortCircuitOperator force-skips ALL downstream tasks,
        # trigger rules ignored — but teardowns are exempt, which is exactly
        # why the injected canary and finalize are teardowns. Without that, a
        # fully short-circuited (successful!) dag run would report a false
        # failure: skipped canary, no marker.
        dr = dags.dag_force_skip.test()
        assert dr.state == DagRunState.SUCCESS
        states = ti_states(dr)
        assert states["t"] == TaskInstanceState.SKIPPED
        assert states["telomere_canary"] == TaskInstanceState.SUCCESS
        assert telomere.resolutions() == [("end", "tlm-run-1")]

    def test_row6_recovery_leaf_mirrors_airflow_success(self, telomere):
        # Mid-graph failure + all_done recovery leaf that succeeds: Airflow
        # calls the run a success, so must Telomere (operator-confirmed:
        # recovered runs must not alert).
        dr = dags.dag_recovery_leaf.test()
        assert dr.state == DagRunState.SUCCESS
        states = ti_states(dr)
        assert states["a"] == TaskInstanceState.FAILED
        assert states["recover"] == TaskInstanceState.SUCCESS
        assert telomere.resolutions() == [("end", "tlm-run-1")]

    def test_row7_task_retry_success_no_premature_fail(self, telomere):
        # Trigger rules see final states only: a fail-then-retry-success run
        # is completed, with no fail call in between.
        dr = dags.dag_task_retry.test()
        assert dr.state == DagRunState.SUCCESS
        assert telomere.resolutions() == [("end", "tlm-run-1")]

    def test_row8_mapped_instance_fails(self, telomere):
        dr = dags.dag_mapped_fail.test()
        assert dr.state == DagRunState.FAILED
        assert telomere.resolutions() == [("fail", "tlm-run-1")]

    def test_row9_api_down_at_start(self, telomere):
        # No run was ever started, so there is nothing to resolve: finalize
        # must no-op (no XCom), not invent a report. Scheduled DAGs stay
        # covered by the .schedule deadline; the manual-run gap is documented.
        telomere.start_down()
        dr = dags.dag_success.test()
        assert dr.state == DagRunState.SUCCESS  # tracking failure never fails the DAG
        assert telomere.resolutions() == []

    def test_row10_api_down_at_finalize(self, telomere):
        # The report attempt errors; the run dangles server-side and the
        # Telomere timeout (Layer 2) raises the alert. Locally we assert no
        # resolution succeeded and the DAG itself was not failed by it.
        telomere.resolve_down()
        dr = dags.dag_success.test()
        assert dr.state == DagRunState.SUCCESS
        # end was attempted (and blew up) — but never succeeded
        assert telomere.attempted_resolutions() == [("end", "tlm-run-1")]
        assert telomere.resolutions() == []

    def test_row11_finalize_crash_recovered_by_retry(self, telomere, monkeypatch):
        # A transient crash of the finalize task itself (worker death analog)
        # is absorbed by its retries=2 — Layer 1 still reports.
        real_execute = TelomereFinalizeOperator.execute
        crashes = {"n": 0}

        def crash_once(self, context):
            crashes["n"] += 1
            if crashes["n"] == 1:
                raise RuntimeError("simulated worker death")
            return real_execute(self, context)

        monkeypatch.setattr(TelomereFinalizeOperator, "execute", crash_once)
        dr = dags.dag_success.test()
        assert dr.state == DagRunState.SUCCESS
        assert crashes["n"] == 2
        assert telomere.resolutions() == [("end", "tlm-run-1")]


class TestEmptyOperatorTrap:
    def test_canary_marker_exists_after_success(self, telomere):
        # Regression pin for the EmptyOperator trap: if the canary were ever
        # "optimized" into a no-execute task, no marker XCom would exist and
        # this run would be reported failed instead of completed.
        dr = dags.dag_success.test()
        assert dr.state == DagRunState.SUCCESS
        assert telomere.resolutions() == [("end", "tlm-run-1")]
