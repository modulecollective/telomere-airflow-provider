"""Fixtures for the failure-mode matrix: metadata DB + a fake Telomere API."""

import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
import requests

from telomere_provider.hooks.telomere import TelomereHook

BASE = TelomereHook.BASE_URL


@pytest.fixture(scope="session", autouse=True)
def airflow_db():
    """dag.test() needs a migrated metadata DB; migrate once per Airflow version."""
    db_path = Path(os.environ["AIRFLOW_HOME"]) / "airflow.db"
    if not db_path.exists():
        subprocess.run(
            [sys.executable, "-m", "airflow", "db", "migrate"],
            check=True,
            capture_output=True,
        )


class FakeTelomere:
    """
    The Telomere endpoints the provider touches, over requests-mock.

    Lifecycle GETs 404 (forcing creation), run starts hand out sequential IDs,
    end/fail/respawn succeed. Individual endpoints can be taken "down"
    (raising ConnectionError) to simulate API outages.
    """

    def __init__(self, requests_mock):
        self.m = requests_mock
        self._next_run = 0
        self._runs = []
        self._resolved = []
        self.m.get(re.compile(rf"{BASE}/api/lifecycles/[^/]+$"), status_code=404)
        self.m.post(f"{BASE}/api/lifecycles", status_code=201, json={"created": True})
        self.m.post(
            re.compile(rf"{BASE}/api/lifecycles/[^/]+/respawn$"), json={"id": "schedule-run"}
        )
        self.m.post(re.compile(rf"{BASE}/api/lifecycles/[^/]+/runs$"), json=self._start_run)
        self.m.get(f"{BASE}/api/runs", json=self._list_runs)
        self.m.post(re.compile(rf"{BASE}/api/runs/[^/]+/end$"), json=self._resolve_run)
        self.m.post(re.compile(rf"{BASE}/api/runs/[^/]+/fail$"), json=self._resolve_run)

    def _start_run(self, request, context):
        self._next_run += 1
        run = {
            "id": f"tlm-run-{self._next_run}",
            "status": "running",
            "startedAt": datetime.now(timezone.utc).isoformat(),
            "tags": request.json().get("tags", {}),
        }
        self._runs.insert(0, run)
        return run

    def _list_runs(self, request, context):
        status = request.qs.get("status", [None])[0]
        items = [run for run in self._runs if status is None or run["status"] == status]
        return {"items": items, "total": len(items), "page": 1, "pageSize": 100}

    def _resolve_run(self, request, context):
        action, run_id = self._parse_resolution(request.path)
        self._resolved.append((action, run_id))
        for run in self._runs:
            if run["id"] == run_id:
                run["status"] = "completed" if action == "end" else "failed"
        return {"status": "completed" if action == "end" else "failed"}

    @staticmethod
    def _parse_resolution(path):
        match = re.fullmatch(r"/api/runs/([^/]+)/(end|fail)", path)
        return match.group(2), match.group(1)

    def start_down(self):
        """Simulate the API being unreachable when a run would be started."""
        self.m.post(
            re.compile(rf"{BASE}/api/lifecycles/[^/]+/runs$"),
            exc=requests.exceptions.ConnectionError,
        )
        # ensure_lifecycle happens first; take it down too for a full outage
        self.m.get(
            re.compile(rf"{BASE}/api/lifecycles/[^/]+$"),
            exc=requests.exceptions.ConnectionError,
        )

    def resolve_down(self):
        """Simulate the API being unreachable when a run would be resolved."""
        self.m.post(
            re.compile(rf"{BASE}/api/runs/[^/]+/end$"),
            exc=requests.exceptions.ConnectionError,
        )
        self.m.post(
            re.compile(rf"{BASE}/api/runs/[^/]+/fail$"),
            exc=requests.exceptions.ConnectionError,
        )

    def resolutions(self):
        """Ordered ("end"|"fail", run_id) resolutions the API *accepted*."""
        return list(self._resolved)

    def attempted_resolutions(self):
        """Like resolutions(), but including requests that errored in flight."""
        return [
            self._parse_resolution(r.path)
            for r in self.m.request_history
            if re.fullmatch(r"/api/runs/[^/]+/(end|fail)", r.path)
        ]


@pytest.fixture
def telomere(requests_mock):
    return FakeTelomere(requests_mock)
