"""
Scenario DAGs for the failure-mode matrix (tier 2).

Each DAG is one way a dag run can end. dag.test() requires DAGs to live in
the configured dags folder (this directory — see tests/conftest.py), so they
are defined here rather than inline in the tests.
"""

from datetime import timedelta

from airflow.providers.standard.operators.python import (
    BranchPythonOperator,
    PythonOperator,
    ShortCircuitOperator,
)
from airflow.sdk import DAG

from telomere_provider.utils import enable_telomere_tracking


def track(dag):
    """Opt the DAG into listener-based tracking."""
    enable_telomere_tracking(dag)
    return dag


def ok():
    return "ok"


def boom():
    raise RuntimeError("induced task failure")


# Row 1: all tasks succeed -> completed
with DAG("matrix_success", schedule=None) as dag_success:
    a = PythonOperator(task_id="a", python_callable=ok)
    b = PythonOperator(task_id="b", python_callable=ok)
    a >> b
track(dag_success)


# Row 2: a leaf task fails -> failed
with DAG("matrix_leaf_fails", schedule=None) as dag_leaf_fails:
    a = PythonOperator(task_id="a", python_callable=ok)
    fail_leaf = PythonOperator(task_id="fail_leaf", python_callable=boom)
    a >> fail_leaf
track(dag_leaf_fails)


# Row 3: a mid-graph task fails; leaves end upstream_failed, not failed.
# This is the exact miss in 0.0.1's one_failed-on-leaves shape.
with DAG("matrix_midgraph_fails", schedule=None) as dag_midgraph_fails:
    a = PythonOperator(task_id="a", python_callable=ok)
    mid = PythonOperator(task_id="mid", python_callable=boom)
    c = PythonOperator(task_id="c", python_callable=ok)
    a >> mid >> c
track(dag_midgraph_fails)


# Row 4: branching; the untaken path is skipped -> completed
with DAG("matrix_branch_skip", schedule=None) as dag_branch_skip:
    branch = BranchPythonOperator(task_id="branch", python_callable=lambda: "left")
    left = PythonOperator(task_id="left", python_callable=ok)
    right = PythonOperator(task_id="right", python_callable=ok)
    branch >> [left, right]
track(dag_branch_skip)


# Row 5: every user leaf is skipped (short-circuit that respects trigger
# rules) -> Airflow marks the run successful -> listener reports completed.
with DAG("matrix_all_skipped", schedule=None) as dag_all_skipped:
    sc = ShortCircuitOperator(
        task_id="sc",
        python_callable=lambda: False,
        ignore_downstream_trigger_rules=False,
    )
    t = PythonOperator(task_id="t", python_callable=ok)
    sc >> t
track(dag_all_skipped)


# Row 5b: a default ShortCircuitOperator force-skips ALL downstream tasks,
# trigger rules ignored. Airflow still marks the run successful, and the
# listener reports that verdict directly.
with DAG("matrix_force_skip", schedule=None) as dag_force_skip:
    sc = ShortCircuitOperator(task_id="sc", python_callable=lambda: False)
    t = PythonOperator(task_id="t", python_callable=ok)
    sc >> t
track(dag_force_skip)


# Row 6: mid-graph failure + an all_done recovery leaf that succeeds. The
# Airflow run is a *success* (leaves are clean) and must be reported
# completed — a recovered run must not alert (operator decision 2026-07-16).
with DAG("matrix_recovery_leaf", schedule=None) as dag_recovery_leaf:
    a = PythonOperator(task_id="a", python_callable=boom)
    mid = PythonOperator(task_id="mid", python_callable=ok)
    recover = PythonOperator(task_id="recover", python_callable=ok, trigger_rule="all_done")
    a >> mid >> recover
track(dag_recovery_leaf)


def flaky(**context):
    if context["ti"].try_number < 2:
        raise RuntimeError("first attempt fails")
    return "ok on retry"


# Row 7: task fails then its retry succeeds; trigger rules only see final
# states, so this must be reported completed with no premature fail.
with DAG("matrix_task_retry", schedule=None) as dag_task_retry:
    PythonOperator(
        task_id="flaky",
        python_callable=flaky,
        retries=1,
        retry_delay=timedelta(seconds=0),
    )
track(dag_task_retry)


def mapped_fail(x):
    if x == 2:
        raise RuntimeError("mapped instance failure")
    return x


# Row 8: dynamic-mapped leaf where one mapped instance fails -> failed
with DAG("matrix_mapped_fail", schedule=None) as dag_mapped_fail:
    PythonOperator.partial(task_id="mapped", python_callable=mapped_fail).expand(
        op_args=[[1], [2], [3]]
    )
track(dag_mapped_fail)
