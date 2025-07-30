"""
Hook for interacting with Telomere API.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional
from urllib.parse import urljoin

import requests
from airflow.exceptions import AirflowException
from airflow.hooks.base import BaseHook
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


class TelomereConnectionError(AirflowException):
    """Exception raised when unable to connect to Telomere."""


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

    def __init__(self, telomere_conn_id: str = default_conn_name) -> None:
        self.telomere_conn_id = telomere_conn_id
        self._session: Optional[requests.Session] = None

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

        # Set up retries
        retry_strategy = Retry(
            total=extra_config.get("max_retries", 3),
            backoff_factor=extra_config.get("backoff_factor", 0.3),
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["HEAD", "GET", "PUT", "POST", "DELETE", "OPTIONS", "TRACE"],
        )
        adapter = HTTPAdapter(max_retries=retry_strategy)
        session.mount("https://", adapter)

        # Set default timeout
        session.timeout = extra_config.get("timeout", 30)

        return session

    def _request(
        self,
        method: str,
        endpoint: str,
        data: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """Make a request to the Telomere API."""
        url = urljoin(self.BASE_URL, endpoint.lstrip("/"))

        try:
            response = self.session.request(
                method=method,
                url=url,
                json=data,
                params=params,
                headers=headers,
                timeout=self.session.timeout,
            )
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
            error_msg = f"HTTP error from Telomere: {e}"
            try:
                error_detail = e.response.json()
                if "error" in error_detail:
                    error_msg = f"Telomere API error: {error_detail['error']}"
            except:
                pass
            raise AirflowException(error_msg)
        except Exception as e:
            raise AirflowException(f"Unexpected error calling Telomere: {e}")

    def ensure_lifecycle(
        self,
        name: str,
        default_timeout_seconds: int = 3600,
        description: Optional[str] = None,
        default_tags: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """
        Create or get existing lifecycle.

        :param name: Name of the lifecycle
        :param default_timeout_seconds: Default timeout for runs
        :param description: Optional description
        :param default_tags: Optional default tags
        :return: Lifecycle details
        """
        # First try to get existing lifecycle
        try:
            response = self.session.get(
                urljoin(self.BASE_URL, f"/api/lifecycles/{name}"),
                timeout=self.session.timeout,
            )

            if response.status_code == 404:
                # Lifecycle doesn't exist, create it
                self.log.info(f"Lifecycle '{name}' not found, creating new lifecycle")
            else:
                response.raise_for_status()
                self.log.info(f"Found existing lifecycle: {name}")
                return response.json()

        except requests.exceptions.HTTPError as e:
            raise AirflowException(f"Error checking lifecycle existence: {e}")
        except Exception as e:
            raise AirflowException(f"Unexpected error checking lifecycle: {e}")

        # Create the lifecycle
        data = {
            "name": name,
            "defaultTimeoutSeconds": default_timeout_seconds,
        }
        if description:
            data["description"] = description
        if default_tags:
            data["defaultTags"] = default_tags

        try:
            return self._request("POST", "/api/lifecycles", data=data)
        except AirflowException as e:
            # Check if it's a conflict error (lifecycle already exists)
            # The error message contains the response details
            if "CONFLICT" in str(e) or "already exists" in str(e):
                self.log.info(f"Lifecycle '{name}' was created by another process, fetching existing lifecycle")
                # Try to get the lifecycle again
                response = self.session.get(
                    urljoin(self.BASE_URL, f"/api/lifecycles/{name}"),
                    timeout=self.session.timeout,
                )
                response.raise_for_status()
                return response.json()
            else:
                # Re-raise for other errors
                raise

    def start_run(
        self,
        lifecycle_name: str,
        timeout_seconds: Optional[int] = None,
        tags: Optional[Dict[str, str]] = None,
        url: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Start a new run for a lifecycle.

        :param lifecycle_name: Name of the lifecycle
        :param timeout_seconds: Override timeout for this run
        :param tags: Tags for this run
        :param url: Optional URL for this run
        :return: Run details
        """
        data = {}
        if timeout_seconds is not None:
            data["timeoutSeconds"] = timeout_seconds
        if tags:
            data["tags"] = tags
        if url:
            data["url"] = url

        return self._request("POST", f"/api/lifecycles/{lifecycle_name}/runs", data=data)

    def end_run(self, run_id: str, message: Optional[str] = None) -> Dict[str, Any]:
        """
        Mark run as completed.

        :param run_id: ID of the run
        :param message: Optional completion message
        :return: Updated run details
        """
        data = {}
        if message:
            data["message"] = message
        return self._request("POST", f"/api/runs/{run_id}/end", data=data)

    def fail_run(self, run_id: str, message: Optional[str] = None) -> Dict[str, Any]:
        """
        Mark run as failed.

        :param run_id: ID of the run
        :param message: Optional failure message
        :return: Updated run details
        """
        data = {}
        if message:
            data["message"] = message

        return self._request("POST", f"/api/runs/{run_id}/fail", data=data)

    def get_run(self, run_id: str) -> Dict[str, Any]:
        """
        Get run details.

        :param run_id: ID of the run
        :return: Run details
        """
        return self._request("GET", f"/api/runs/{run_id}")

    def respawn(
        self,
        lifecycle_name: str,
        timeout_seconds: Optional[int] = None,
        tags: Optional[Dict[str, str]] = None,
        url: Optional[str] = None,
        previous_run_resolution: str = "complete",
    ) -> Dict[str, Any]:
        """
        Atomically complete any running runs and start a new run.

        :param lifecycle_name: Name of the lifecycle
        :param timeout_seconds: Override timeout for new run
        :param tags: Tags for new run
        :param url: Optional URL for new run
        :param previous_run_resolution: How to resolve previous runs (complete/fail/timeout)
        :return: Response containing previous and new run details
        """
        data = {"previousRunResolution": previous_run_resolution}
        if timeout_seconds is not None:
            data["timeoutSeconds"] = timeout_seconds
        if tags:
            data["tags"] = tags
        if url:
            data["url"] = url

        return self._request("POST", f"/api/lifecycles/{lifecycle_name}/respawn", data=data)

    def unspawn(self, lifecycle_name: str, resolution: str = "complete") -> Dict[str, Any]:
        """
        Complete all running runs without starting a new one.

        :param lifecycle_name: Name of the lifecycle
        :param resolution: How to resolve running runs (complete/fail/timeout)
        :return: Details of ended runs
        """
        # First check if the lifecycle exists
        try:
            response = self.session.get(
                urljoin(self.BASE_URL, f"/api/lifecycles/{lifecycle_name}"),
                timeout=self.session.timeout,
            )

            if response.status_code == 404:
                # Lifecycle doesn't exist, nothing to unspawn
                self.log.info(f"Lifecycle '{lifecycle_name}' not found, skipping unspawn")
                return {"endedRuns": []}

            response.raise_for_status()

        except requests.exceptions.HTTPError as e:
            raise AirflowException(f"Error checking lifecycle existence: {e}")
        except Exception as e:
            raise AirflowException(f"Unexpected error checking lifecycle: {e}")

        # Lifecycle exists, proceed with unspawn
        return self._request(
            "POST",
            f"/api/lifecycles/{lifecycle_name}/unspawn",
            data={"resolution": resolution}
        )

    def test_connection(self) -> tuple[bool, str]:
        """Test Telomere connection."""
        try:
            # Try to list lifecycles to test connection
            self._request("GET", "/api/lifecycles", params={"pageSize": 1})
            return True, "Connection successful"
        except Exception as e:
            return False, str(e)

    @staticmethod
    def get_connection_form_widgets() -> Dict[str, Any]:
        """Return connection widgets to add to Airflow connection form."""
        from flask_appbuilder.fieldwidgets import BS3PasswordFieldWidget
        from flask_babel import lazy_gettext
        from wtforms import PasswordField, IntegerField
        from wtforms.validators import Optional

        return {
            "password": PasswordField(
                lazy_gettext("API Key"),
                widget=BS3PasswordFieldWidget(),
            ),
            "extra__telomere__timeout": IntegerField(
                lazy_gettext("Request Timeout (seconds)"),
                validators=[Optional()],
                default=30,
            ),
            "extra__telomere__max_retries": IntegerField(
                lazy_gettext("Max Retries"),
                validators=[Optional()],
                default=3,
            ),
        }

    @staticmethod
    def get_ui_field_behaviour() -> Dict[str, Any]:
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