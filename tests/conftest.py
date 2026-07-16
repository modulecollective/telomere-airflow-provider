"""
Session-wide test environment.

Everything here must run before the first `airflow` import anywhere in the
test session: Airflow freezes its configuration (AIRFLOW_HOME, dags folder)
at import time.
"""

import os
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent

# Version-scoped home so a cached metadata DB never outlives a schema change.
# The e2e tier runs without airflow installed (it drives the compose stack
# over HTTP only) — any placeholder version is fine there.
try:
    _airflow_version = version("apache-airflow")
except PackageNotFoundError:
    _airflow_version = "none"
_tmp = Path(os.environ.get("TMPDIR") or "/tmp")
_airflow_home = _tmp / f"telomere-provider-tests-airflow-{_airflow_version}"

os.environ.setdefault("AIRFLOW_HOME", str(_airflow_home))
os.environ.setdefault("AIRFLOW__CORE__DAGS_FOLDER", str(TESTS_DIR / "matrix" / "dags"))
os.environ.setdefault("AIRFLOW__CORE__LOAD_EXAMPLES", "False")
os.environ.setdefault("AIRFLOW__CORE__DAGS_ARE_PAUSED_AT_CREATION", "False")
# All tests talk to Telomere through this connection; the API itself is mocked.
os.environ.setdefault("AIRFLOW_CONN_TELOMERE_DEFAULT", "telomere://:test-api-key@")

Path(os.environ["AIRFLOW_HOME"]).mkdir(parents=True, exist_ok=True)
