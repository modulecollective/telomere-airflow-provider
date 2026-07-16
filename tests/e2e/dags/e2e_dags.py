"""
DAGs for the e2e tier (tier 3): real scheduler, real Telomere API.

Mounted into the docker compose stack at /opt/airflow/dags/e2e. The e2e tests
trigger them over the Airflow REST API and assert the resulting Telomere run
states over the Telomere API.
"""

from datetime import datetime, timedelta
from time import sleep

from airflow.providers.standard.operators.python import PythonOperator
from airflow.sdk import DAG

from telomere_provider.utils import enable_telomere_tracking


def ok():
    return "ok"


def boom():
    raise RuntimeError("induced e2e failure")


# Scenario: all tasks succeed -> Telomere run completed
with DAG(
    "e2e_success",
    schedule=None,
    start_date=datetime(2026, 1, 1),
    catchup=False,
) as dag_success:
    a = PythonOperator(task_id="a", python_callable=ok)
    b = PythonOperator(task_id="b", python_callable=ok)
    a >> b
enable_telomere_tracking(dag_success)


# Scenario: mid-graph failure -> Telomere run failed (the 0.0.1 miss)
with DAG(
    "e2e_midgraph_fail",
    schedule=None,
    start_date=datetime(2026, 1, 1),
    catchup=False,
) as dag_midgraph_fail:
    a = PythonOperator(task_id="a", python_callable=ok)
    mid = PythonOperator(task_id="mid", python_callable=boom)
    c = PythonOperator(task_id="c", python_callable=ok)
    a >> mid >> c
enable_telomere_tracking(dag_midgraph_fail)


def sleep_forever():
    sleep(300)


# Scenario: the timeout net (Layer 2). dagrun_timeout kills the run mid-flight
# so nothing ever reports explicitly; the Telomere run must still alert by
# hitting the timeout it was created with. This is the only tier that can
# prove Layer 2 end to end.
with DAG(
    "e2e_timeout_net",
    schedule=None,
    start_date=datetime(2026, 1, 1),
    catchup=False,
    dagrun_timeout=timedelta(seconds=30),
) as dag_timeout_net:
    PythonOperator(task_id="sleeper", python_callable=sleep_forever)
enable_telomere_tracking(dag_timeout_net, timeout_seconds=25)
