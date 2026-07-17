"""
Hook for interacting with Telomere API.
"""

from __future__ import annotations

import json
from typing import Any

import requests
from airflow.exceptions import AirflowException

try:
    from airflow.sdk.bases.hook import BaseHook
except ImportError:  # Airflow 3.0.x, before airflow.sdk.bases existed
    from airflow.hooks.base import BaseHook
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


class TelomereConnectionError(AirflowException):
    """Exception raised when unable to connect to Telomere."""


class TelomereApiError(AirflowException):
    """Exception raised when Telomere returns an HTTP error status."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class TelomereHook(BaseHook):
    """
    Hook for interacting with Telomere API.

    Connection fields:
    - password: API key
    - extra: JSON with additional config (timeout, retry settings)

    :param telomere_conn_id: Connection ID for Telomere
    """

    conn_name_attr = "telomere_conn_id"
    default_conn_name = "telomere_default"
    conn_type = "telomere"
    hook_name = "Telomere"

    BASE_URL = "https://telomere.modulecollective.com"
    DEFAULT_TIMEOUT = 30

    def __init__(self, telomere_conn_id: str = default_conn_name) -> None:
        super().__init__()
        self.telomere_conn_id = telomere_conn_id
        self.timeout: int = self.DEFAULT_TIMEOUT
        self._session: requests.Session | None = None

    @property
    def session(self) -> requests.Session:
        """Get configured requests session with auth and retries."""
        if self._session is None:
            self._session = self._create_session()
        return self._session

    def _create_session(self) -> requests.Session:
        """Create a requests session with proper configuration."""
        conn = self.get_connection(self.telomere_conn_id)

        # Parse extra config
        extra_config = {}
        if conn.extra:
            try:
                extra_config = json.loads(conn.extra)
            except json.JSONDecodeError:
                self.log.warning("Failed to parse extra config as JSON")

        # Create session
        session = requests.Session()

        # Set up authentication
        if conn.password:
            session.headers["Authorization"] = f"Bearer {conn.password}"
        else:
            raise TelomereConnectionError("No API key found in connection")

        # Set up retries. POST is deliberately not retried at this layer: a POST
        # that reached the server but returned 5xx could otherwise be replayed
        # and double-start runs. Callers handle idempotency instead (retried
        # end/fail is safe: the server returns 409 for non-running runs, which
        # the hook treats as success).
        retry_strategy = Retry(
            total=extra_config.get("max_retries", 3),
            backoff_factor=extra_config.get("backoff_factor", 0.3),
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["HEAD", "GET", "PUT", "DELETE", "OPTIONS", "TRACE"],
        )
        adapter = HTTPAdapter(max_retries=retry_strategy)
        session.mount("https://", adapter)

        self.timeout = extra_config.get("timeout", self.DEFAULT_TIMEOUT)

        return session

    def _request(
        self,
        method: str,
        endpoint: str,
        data: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        ok_404: bool = False,
    ) -> dict[str, Any] | None:
        """
        Make a request to the Telomere API.

        :param method: HTTP method
        :param endpoint: API endpoint (starting with /)
        :param data: JSON body
        :param params: Query parameters
        :param ok_404: If True, return None on 404 instead of raising
        :return: Parsed JSON response ({} for 204 No Content), or None on 404
            when ``ok_404`` is set
        """
        url = f"{self.BASE_URL}{endpoint}"

        try:
            response = self.session.request(
                method=method,
                url=url,
                json=data,
                params=params,
                timeout=self.timeout,
            )
            if ok_404 and response.status_code == 404:
                return None
            response.raise_for_status()

            # Return empty dict for 204 No Content
            if response.status_code == 204:
                return {}

            return response.json()
        except requests.exceptions.ConnectionError as e:
            raise TelomereConnectionError(f"Failed to connect to Telomere: {e}")
        except requests.exceptions.Timeout as e:
            raise TelomereConnectionError(f"Request to Telomere timed out: {e}")
        except requests.exceptions.HTTPError as e:
            status_code = e.response.status_code if e.response is not None else None
            error_msg = f"HTTP error from Telomere: {e}"
            try:
                error_detail = e.response.json()
                if "error" in error_detail:
                    error_msg = f"Telomere API error: {error_detail['error']}"
            except (ValueError, AttributeError):
                pass
            raise TelomereApiError(error_msg, status_code=status_code)

    def ensure_lifecycle(
        self,
        name: str,
        default_timeout_seconds: int = 3600,
        description: str | None = None,
        default_tags: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """
        Create or get existing lifecycle.

        :param name: Name of the lifecycle
        :param default_timeout_seconds: Default timeout for runs
        :param description: Optional description
        :param default_tags: Optional default tags
        :return: Lifecycle details
        """
        existing = self._request("GET", f"/api/lifecycles/{name}", ok_404=True)
        if existing is not None:
            self.log.info("Found existing lifecycle: %s", name)
            return existing

        self.log.info("Lifecycle '%s' not found, creating new lifecycle", name)
        data: dict[str, Any] = {
            "name": name,
            "defaultTimeoutSeconds": default_timeout_seconds,
        }
        if description:
            data["description"] = description
        if default_tags:
            data["defaultTags"] = default_tags

        try:
            result = self._request("POST", "/api/lifecycles", data=data)
            assert result is not None
            return result
        except TelomereApiError as e:
            if e.status_code == 409:
                # Lifecycle was created concurrently; fetch it
                self.log.info(
                    "Lifecycle '%s' was created by another process, fetching existing lifecycle",
                    name,
                )
                created = self._request("GET", f"/api/lifecycles/{name}")
                assert created is not None
                return created
            raise

    def start_run(
        self,
        lifecycle_name: str,
        timeout_seconds: int | None = None,
        tags: dict[str, str] | None = None,
        url: str | None = None,
    ) -> dict[str, Any]:
        """
        Start a new run for a lifecycle.

        :param lifecycle_name: Name of the lifecycle
        :param timeout_seconds: Override timeout for this run
        :param tags: Tags for this run
        :param url: Optional URL for this run
        :return: Run details
        """
        data: dict[str, Any] = {}
        if timeout_seconds is not None:
            data["timeoutSeconds"] = timeout_seconds
        if tags:
            data["tags"] = tags
        if url:
            data["url"] = url

        result = self._request("POST", f"/api/lifecycles/{lifecycle_name}/runs", data=data)
        assert result is not None
        return result

    def end_run(self, run_id: str, message: str | None = None) -> dict[str, Any]:
        """
        Mark run as completed.

        The server rejects end/fail on non-running runs with 409: only runs
        in "running" status can be ended. That 409 is treated as success so
        retried finalize calls stay idempotent — the first resolution wins.

        :param run_id: ID of the run
        :param message: Optional completion message
        :return: Updated run details ({} on a tolerated 409)
        """
        return self._resolve_run(run_id, "end", message)

    def fail_run(self, run_id: str, message: str | None = None) -> dict[str, Any]:
        """
        Mark run as failed.

        The server rejects end/fail on non-running runs with 409: only runs
        in "running" status can be failed. That 409 is treated as success so
        retried finalize calls stay idempotent — the first resolution wins.

        :param run_id: ID of the run
        :param message: Optional failure message
        :return: Updated run details ({} on a tolerated 409)
        """
        return self._resolve_run(run_id, "fail", message)

    def _resolve_run(self, run_id: str, action: str, message: str | None) -> dict[str, Any]:
        """POST /api/runs/{id}/{action}; a 409 is tolerated as success.

        The server enforces the running-only contract: end/fail on a
        non-running run returns 409. The 409 branch below is that contract
        path — a run already resolved (ended, failed, or timed out) stays as
        first resolved, and the retried finalize call is treated as success.
        """
        data = {}
        if message:
            data["message"] = message
        try:
            result = self._request("POST", f"/api/runs/{run_id}/{action}", data=data)
            assert result is not None
            return result
        except TelomereApiError as e:
            if e.status_code == 409:
                self.log.info(
                    "Run %s not running (already resolved); treating %s as success",
                    run_id,
                    action,
                )
                return {}
            raise

    def get_run(self, run_id: str) -> dict[str, Any]:
        """
        Get run details.

        :param run_id: ID of the run
        :return: Run details
        """
        result = self._request("GET", f"/api/runs/{run_id}")
        assert result is not None
        return result

    def list_runs(
        self,
        lifecycle_name: str,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        """List all runs for a lifecycle, optionally filtered by status."""
        page = 1
        page_size = 100
        runs: list[dict[str, Any]] = []

        while True:
            params: dict[str, Any] = {
                "lifecycleIdOrName": lifecycle_name,
                "page": page,
                "pageSize": page_size,
            }
            if status is not None:
                params["status"] = status

            result = self._request("GET", "/api/runs", params=params)
            assert result is not None
            items = result["items"]
            runs.extend(items)

            if len(runs) >= result["total"]:
                return runs
            page += 1

    def respawn(
        self,
        lifecycle_name: str,
        timeout_seconds: int | None = None,
        tags: dict[str, str] | None = None,
        url: str | None = None,
        previous_run_resolution: str = "complete",
    ) -> dict[str, Any]:
        """
        Atomically complete any running runs and start a new run.

        :param lifecycle_name: Name of the lifecycle
        :param timeout_seconds: Override timeout for new run
        :param tags: Tags for new run
        :param url: Optional URL for new run
        :param previous_run_resolution: How to resolve previous runs (complete/fail/timeout)
        :return: Response containing previous and new run details
        """
        data: dict[str, Any] = {"previousRunResolution": previous_run_resolution}
        if timeout_seconds is not None:
            data["timeoutSeconds"] = timeout_seconds
        if tags:
            data["tags"] = tags
        if url:
            data["url"] = url

        result = self._request("POST", f"/api/lifecycles/{lifecycle_name}/respawn", data=data)
        assert result is not None
        return result

    def unspawn(self, lifecycle_name: str, resolution: str = "complete") -> dict[str, Any]:
        """
        Complete all running runs without starting a new one.

        :param lifecycle_name: Name of the lifecycle
        :param resolution: How to resolve running runs (complete/fail/timeout)
        :return: Details of ended runs
        """
        existing = self._request("GET", f"/api/lifecycles/{lifecycle_name}", ok_404=True)
        if existing is None:
            self.log.info("Lifecycle '%s' not found, skipping unspawn", lifecycle_name)
            return {"endedRuns": []}

        result = self._request(
            "POST",
            f"/api/lifecycles/{lifecycle_name}/unspawn",
            data={"resolution": resolution},
        )
        assert result is not None
        return result

    def test_connection(self) -> tuple[bool, str]:
        """Test Telomere connection."""
        try:
            self._request("GET", "/api/lifecycles", params={"pageSize": 1})
            return True, "Connection successful"
        except Exception as e:
            return False, str(e)

    @staticmethod
    def get_ui_field_behaviour() -> dict[str, Any]:
        """Return custom UI field behaviour for Telomere connection."""
        return {
            "hidden_fields": ["schema", "login", "port", "host", "extra"],
            "relabeling": {
                "password": "API Key",
            },
            "placeholders": {
                "password": "your-api-key-here",
            },
        }
