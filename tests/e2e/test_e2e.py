"""
Tier 3: end to end against the docker compose stack and the real Telomere API.

Requires:
- the compose stack up and healthy (tests/e2e/run.sh handles this), and
- TELOMERE_API_KEY in the environment (the same key the stack itself uses).

Pure HTTP: these tests import neither Airflow nor the provider. Telomere runs
are matched via the start task's return-value XCom (the Telomere run ID),
which stays correct under concurrent CI runs sharing lifecycles.
"""

import json
import os
import time

import pytest
import requests

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        not os.environ.get("TELOMERE_API_KEY"),
        reason="TELOMERE_API_KEY not set",
    ),
]

AIRFLOW_BASE_URL = os.environ.get("AIRFLOW_BASE_URL", "http://localhost:8080")
TELOMERE_BASE_URL = "https://telomere.modulecollective.com"

TERMINAL_DAGRUN_STATES = {"success", "failed"}
# Telomere state names, normalized via _run_state(); "completed" is the one
# state that must NEVER show up for a failed/abandoned dag run.
COMPLETED_STATES = {"completed"}
ALERTING_STATES = {"failed", "timedout", "timed_out", "timeout"}


class AirflowClient:
    def __init__(self, base_url):
        self.base_url = base_url
        resp = requests.post(
            f"{base_url}/auth/token",
            json={"username": "airflow", "password": "airflow"},
            timeout=30,
        )
        resp.raise_for_status()
        self.session = requests.Session()
        self.session.headers["Authorization"] = f"Bearer {resp.json()['access_token']}"

    def _url(self, path):
        return f"{self.base_url}/api/v2{path}"

    def wait_for_dag(self, dag_id, timeout=180):
        """Wait until the dag-processor has parsed and registered the DAG."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.session.get(self._url(f"/dags/{dag_id}"), timeout=30).status_code == 200:
                return
            time.sleep(3)
        raise TimeoutError(f"DAG {dag_id} never appeared in the API")

    def unpause(self, dag_id):
        resp = self.session.patch(
            self._url(f"/dags/{dag_id}"), json={"is_paused": False}, timeout=30
        )
        resp.raise_for_status()

    def trigger(self, dag_id):
        resp = self.session.post(
            self._url(f"/dags/{dag_id}/dagRuns"), json={"logical_date": None}, timeout=30
        )
        resp.raise_for_status()
        return resp.json()["dag_run_id"]

    def wait_for_dagrun(self, dag_id, run_id, timeout=300):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            resp = self.session.get(self._url(f"/dags/{dag_id}/dagRuns/{run_id}"), timeout=30)
            resp.raise_for_status()
            state = resp.json()["state"]
            if state in TERMINAL_DAGRUN_STATES:
                return state
            time.sleep(3)
        raise TimeoutError(f"dag run {dag_id}/{run_id} never reached a terminal state")

    def xcom(self, dag_id, run_id, task_id, key="return_value"):
        resp = self.session.get(
            self._url(
                f"/dags/{dag_id}/dagRuns/{run_id}/taskInstances/{task_id}/xcomEntries/{key}"
            ),
            timeout=30,
        )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        value = resp.json()["value"]
        # XCom values come back JSON-encoded
        if isinstance(value, str):
            try:
                return json.loads(value)
            except ValueError:
                return value
        return value


class TelomereClient:
    def __init__(self, api_key):
        self.session = requests.Session()
        self.session.headers["Authorization"] = f"Bearer {api_key}"

    def get_run(self, run_id):
        resp = self.session.get(f"{TELOMERE_BASE_URL}/api/runs/{run_id}", timeout=30)
        resp.raise_for_status()
        return resp.json()

    def wait_for_run_state(self, run_id, states, timeout=120):
        """Poll until the run's state lands in `states` (normalized)."""
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            last = _run_state(self.get_run(run_id))
            if last in states:
                return last
            time.sleep(5)
        raise TimeoutError(f"run {run_id} stuck in state {last!r}, wanted one of {states}")


def _run_state(run):
    """Normalize the run's state field (be liberal about key and casing)."""
    for key in ("state", "status"):
        if key in run:
            return str(run[key]).strip().lower()
    raise KeyError(f"no state field in Telomere run payload: {sorted(run)}")


@pytest.fixture(scope="session")
def airflow():
    client = AirflowClient(AIRFLOW_BASE_URL)
    for dag_id in ("e2e_success", "e2e_midgraph_fail", "e2e_timeout_net"):
        client.wait_for_dag(dag_id)
        client.unpause(dag_id)
    return client


@pytest.fixture(scope="session")
def telomere():
    return TelomereClient(os.environ["TELOMERE_API_KEY"])


def run_and_get_telomere_run_id(airflow, dag_id, expected_state):
    run_id = airflow.trigger(dag_id)
    state = airflow.wait_for_dagrun(dag_id, run_id)
    assert state == expected_state
    tlm_run_id = airflow.xcom(dag_id, run_id, "telomere_dag_start")
    assert tlm_run_id, "start operator pushed no Telomere run ID"
    return tlm_run_id


def test_success_reports_completed(airflow, telomere):
    tlm_run_id = run_and_get_telomere_run_id(airflow, "e2e_success", "success")
    state = telomere.wait_for_run_state(tlm_run_id, COMPLETED_STATES, timeout=60)
    assert state in COMPLETED_STATES


def test_midgraph_failure_reports_failed(airflow, telomere):
    # The exact miss the 0.0.1 shape had: a non-leaf failure.
    tlm_run_id = run_and_get_telomere_run_id(airflow, "e2e_midgraph_fail", "failed")
    state = telomere.wait_for_run_state(tlm_run_id, ALERTING_STATES, timeout=60)
    assert state not in COMPLETED_STATES


def test_timeout_net_catches_abandoned_run(airflow, telomere):
    # Layer 2: dagrun_timeout kills the run; nothing reports explicitly. The
    # Telomere run was created with a 25s timeout and must alert by itself.
    run_id = airflow.trigger("e2e_timeout_net")
    tlm_run_id = None
    # The start task finishes quickly even though the run as a whole hangs
    deadline = time.monotonic() + 120
    while tlm_run_id is None and time.monotonic() < deadline:
        tlm_run_id = airflow.xcom("e2e_timeout_net", run_id, "telomere_dag_start")
        if tlm_run_id is None:
            time.sleep(3)
    assert tlm_run_id, "start operator never pushed a Telomere run ID"

    # Never completed — only failed/timed out counts as landing correctly.
    state = telomere.wait_for_run_state(tlm_run_id, ALERTING_STATES, timeout=240)
    assert state not in COMPLETED_STATES

    # The Airflow run itself must have been killed by dagrun_timeout.
    assert airflow.wait_for_dagrun("e2e_timeout_net", run_id) == "failed"


def test_concurrent_runs_resolve_their_own_runs(airflow, telomere):
    first = airflow.trigger("e2e_success")
    second = airflow.trigger("e2e_success")
    assert airflow.wait_for_dagrun("e2e_success", first) == "success"
    assert airflow.wait_for_dagrun("e2e_success", second) == "success"

    tlm_first = airflow.xcom("e2e_success", first, "telomere_dag_start")
    tlm_second = airflow.xcom("e2e_success", second, "telomere_dag_start")
    assert tlm_first and tlm_second and tlm_first != tlm_second

    assert telomere.wait_for_run_state(tlm_first, COMPLETED_STATES, timeout=60)
    assert telomere.wait_for_run_state(tlm_second, COMPLETED_STATES, timeout=60)
