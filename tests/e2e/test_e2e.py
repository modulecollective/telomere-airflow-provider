"""
Tier 3: real Airflow scheduler, provider listener, and Telomere API.

Pure HTTP: these tests import neither Airflow nor the provider. Telomere runs
are matched by the Airflow run ID tag the listener writes at run start.
"""

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


class TelomereClient:
    def __init__(self, api_key):
        self.session = requests.Session()
        self.session.headers["Authorization"] = f"Bearer {api_key}"

    def get_run(self, run_id):
        resp = self.session.get(f"{TELOMERE_BASE_URL}/api/runs/{run_id}", timeout=30)
        resp.raise_for_status()
        return resp.json()

    def find_dag_run(self, dag_id, airflow_run_id, timeout=120):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            resp = self.session.get(
                f"{TELOMERE_BASE_URL}/api/runs",
                params={"lifecycleIdOrName": f"{dag_id}.dag", "pageSize": 100},
                timeout=30,
            )
            resp.raise_for_status()
            matches = [
                run
                for run in resp.json()["items"]
                if run.get("tags", {}).get("run_id") == airflow_run_id
            ]
            if matches:
                return max(matches, key=lambda run: run["startedAt"])
            time.sleep(3)
        raise TimeoutError(f"Telomere run for {dag_id}/{airflow_run_id} never appeared")

    def wait_for_run_state(self, run_id, states, timeout=120):
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            last = _run_state(self.get_run(run_id))
            if last in states:
                return last
            time.sleep(5)
        raise TimeoutError(f"run {run_id} stuck in state {last!r}, wanted one of {states}")


def _run_state(run):
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


def test_success_reports_completed(airflow, telomere):
    airflow_run_id = airflow.trigger("e2e_success")
    assert airflow.wait_for_dagrun("e2e_success", airflow_run_id) == "success"
    telomere_run_id = telomere.find_dag_run("e2e_success", airflow_run_id)["id"]
    assert telomere.wait_for_run_state(telomere_run_id, COMPLETED_STATES, timeout=60)


def test_midgraph_failure_reports_failed(airflow, telomere):
    airflow_run_id = airflow.trigger("e2e_midgraph_fail")
    assert airflow.wait_for_dagrun("e2e_midgraph_fail", airflow_run_id) == "failed"
    telomere_run_id = telomere.find_dag_run("e2e_midgraph_fail", airflow_run_id)["id"]
    state = telomere.wait_for_run_state(telomere_run_id, ALERTING_STATES, timeout=60)
    assert state not in COMPLETED_STATES


def test_dagrun_timeout_reports_failed(airflow, telomere):
    airflow_run_id = airflow.trigger("e2e_timeout_net")
    telomere_run_id = telomere.find_dag_run("e2e_timeout_net", airflow_run_id)["id"]
    assert airflow.wait_for_dagrun("e2e_timeout_net", airflow_run_id) == "failed"
    state = telomere.wait_for_run_state(telomere_run_id, ALERTING_STATES, timeout=60)
    assert state not in COMPLETED_STATES


def test_concurrent_runs_resolve_their_own_runs(airflow, telomere):
    first = airflow.trigger("e2e_success")
    second = airflow.trigger("e2e_success")
    assert airflow.wait_for_dagrun("e2e_success", first) == "success"
    assert airflow.wait_for_dagrun("e2e_success", second) == "success"

    telomere_first = telomere.find_dag_run("e2e_success", first)["id"]
    telomere_second = telomere.find_dag_run("e2e_success", second)["id"]
    assert telomere_first != telomere_second
    assert telomere.wait_for_run_state(telomere_first, COMPLETED_STATES, timeout=60)
    assert telomere.wait_for_run_state(telomere_second, COMPLETED_STATES, timeout=60)
