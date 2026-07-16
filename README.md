# Telomere Airflow Provider

Apache Airflow provider for [Telomere](https://telomere.modulecollective.com/)
lifecycle tracking. It monitors whole DAG runs and individual critical tasks,
with server-side timeouts as a fallback when Airflow cannot report.

Requires Apache Airflow >= 3.0, < 4 and Python >= 3.10. Airflow 2 users should
pin `telomere-airflow-provider==0.0.1`.

## Installation

```bash
pip install telomere-airflow-provider
```

Add a connection named `telomere_default` with connection type `Telomere` and
your API key in the password field:

```bash
airflow connections add telomere_default \
  --conn-type telomere \
  --conn-password YOUR_API_KEY
```

Or configure it through the environment:

```bash
export AIRFLOW_CONN_TELOMERE_DEFAULT='telomere://:YOUR_API_KEY@'
```

## DAG-run tracking

Opt a DAG in with the `telomere` tag:

```python
from airflow.sdk import DAG

dag = DAG(
    "daily_report",
    schedule="0 2 * * *",
    tags=["telomere"],
)
```

Existing callers can keep the one-line helper. In 2.0 it only adds the tag and
does not modify the task graph:

```python
from telomere_provider.utils import enable_telomere_tracking

enable_telomere_tracking(dag)
```

The provider's Airflow plugin listens for scheduler-computed DAG-run state
changes:

- `on_dag_run_running` creates `<dag_id>.dag` and starts a Telomere run.
- `on_dag_run_success` resolves the matching run as completed.
- `on_dag_run_failed` resolves it as failed, including scheduler timeouts and
  all-tasks-deadlocked outcomes.

Runs carry `dag_id`, Airflow `run_id`, `run_type`, and (when present)
`logical_date` tags, plus a link to the Airflow run. Terminal events find the
running Telomere run by its Airflow `run_id`, so tracking remains correct after
scheduler restarts and when a run is marked manually through the API server.

The run timeout is selected in this order:

1. the DAG's `dagrun_timeout`;
2. the data-interval length;
3. one hour.

Every schedulable DAG also maintains `<dag_id>.schedule`. Its deadline uses
Airflow's `next_dagrun_create_after` plus five minutes of grace, falling back
to the current data-interval end. This is the deadman switch for a scheduler
or DAG that stops creating runs entirely.

Listener errors are isolated by Airflow and cannot change a DAG run's state.
A write outage can therefore leave a Telomere run open until its timeout, but
it cannot fail the user's workflow.

### Upgrading to 2.0

`enable_telomere_tracking` no longer accepts `lifecycle_name`,
`timeout_seconds`, `tags`, `telomere_conn_id`, `fail_on_telomere_error`,
`track_schedule`, or other options. Stale calls raise `TypeError` during DAG
parsing instead of silently changing behavior.

Use `dagrun_timeout` for a per-DAG execution timeout. DAG lifecycle names are
now always `<dag_id>.dag`, schedule monitoring is automatic for schedulable
DAGs, and the listener always uses `telomere_default`. The removed
`TelomereDAGStartOperator`, `TelomereCanaryOperator`, and
`TelomereFinalizeOperator` are no longer needed because no tasks are injected.

## Task-level tracking

`TelomereLifecycleOperator` remains available for critical operations that
need their own lifecycle, custom timeout, tags, or connection:

```python
from telomere_provider.operators.telomere import TelomereLifecycleOperator

critical_task = TelomereLifecycleOperator(
    task_id="process_payment",
    python_callable=process_payment_batch,
    lifecycle_name="payment_processing",
    timeout_seconds=300,
    tags={"priority": "high"},
    dag=dag,
)
```

Relative lifecycle names are namespaced by the DAG ID. For example,
`lifecycle_name="validate"` becomes `<dag_id>.validate`. The value may also be
a callable that receives the Airflow task context.

By default, Telomere errors do not fail the task. Set
`fail_on_telomere_error=True` when inability to record the task should fail
the Airflow task itself.

## Connection options

The connection extra JSON supports read retry and timeout settings:

```json
{
  "timeout": 30,
  "max_retries": 3,
  "backoff_factor": 0.3
}
```

Retries apply to reads only. Writes are not replayed because retrying a run
start after an ambiguous response could create a duplicate.

## Development

```bash
uv sync
uv run ruff check .
uv run pytest
```

The test suite has three tiers:

- unit tests for the hook, listener, tag shim, and task operator;
- an Airflow-version matrix that drives terminal DAG shapes through
  `dag.test()` and the real listener plugin;
- end-to-end tests using a real scheduler and Telomere API.

Run the end-to-end tier with:

```bash
TELOMERE_API_KEY=... tests/e2e/run.sh
```

The Docker Compose stack exposes Airflow at `http://localhost:8080` with
username and password `airflow`. See [docker/README.md](docker/README.md).
