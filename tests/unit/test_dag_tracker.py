"""Tier 1: the DAG opt-in shim."""

import pytest
from airflow.providers.standard.operators.empty import EmptyOperator
from airflow.sdk import DAG

from telomere_provider.utils import enable_telomere_tracking


def test_adds_tag_without_changing_graph():
    with DAG("tracked", schedule=None, tags=["existing"]) as dag:
        EmptyOperator(task_id="task")

    enable_telomere_tracking(dag)

    assert dag.tags == {"existing", "telomere"}
    assert dag.task_ids == ["task"]


def test_is_idempotent_and_allows_empty_dag():
    dag = DAG("tracked", schedule=None)

    enable_telomere_tracking(dag)
    enable_telomere_tracking(dag)

    assert dag.tags == {"telomere"}


def test_removed_options_fail_loudly():
    dag = DAG("tracked", schedule=None)

    with pytest.raises(TypeError, match="lifecycle_name"):
        enable_telomere_tracking(dag, lifecycle_name="custom")
