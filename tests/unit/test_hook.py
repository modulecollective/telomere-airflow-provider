"""Tier 1: TelomereHook against a mocked HTTP layer (no Airflow runtime)."""

import json
from types import SimpleNamespace

import pytest
import requests

from telomere_provider.hooks.telomere import (
    TelomereApiError,
    TelomereConnectionError,
    TelomereHook,
)

BASE = TelomereHook.BASE_URL


@pytest.fixture
def hook():
    return TelomereHook()


class TestSessionConfiguration:
    def test_auth_header_from_connection_password(self, hook):
        assert hook.session.headers["Authorization"] == "Bearer test-api-key"

    def test_missing_api_key_raises(self, monkeypatch):
        monkeypatch.setenv("AIRFLOW_CONN_TELOMERE_NOKEY", json.dumps({"conn_type": "telomere"}))
        with pytest.raises(TelomereConnectionError, match="No API key"):
            TelomereHook("telomere_nokey").session

    def test_retry_and_timeout_from_extra(self, monkeypatch):
        monkeypatch.setenv(
            "AIRFLOW_CONN_TELOMERE_EXTRA",
            json.dumps(
                {
                    "conn_type": "telomere",
                    "password": "key",
                    "extra": {"timeout": 5, "max_retries": 7, "backoff_factor": 1.5},
                }
            ),
        )
        hook = TelomereHook("telomere_extra")
        adapter = hook.session.get_adapter(BASE)
        assert hook.timeout == 5
        assert adapter.max_retries.total == 7
        assert adapter.max_retries.backoff_factor == 1.5

    def test_post_is_never_retried(self, hook):
        # A POST that reached the server but 5xx'd must not be replayed:
        # replaying start_run would double-start runs.
        adapter = hook.session.get_adapter(BASE)
        assert "POST" not in adapter.max_retries.allowed_methods
        assert "GET" in adapter.max_retries.allowed_methods

    def test_invalid_extra_json_tolerated(self, monkeypatch):
        # Stubbed connection: Airflow versions disagree on whether a non-JSON
        # __extra__ URI even parses; what we care about is the hook shrugging
        # off a non-JSON extra field.
        conn = SimpleNamespace(password="key", extra="notjson")
        hook = TelomereHook()
        monkeypatch.setattr(hook, "get_connection", lambda conn_id: conn)
        assert hook.session.headers["Authorization"] == "Bearer key"
        assert hook.timeout == TelomereHook.DEFAULT_TIMEOUT


class TestRequest:
    def test_returns_json(self, hook, requests_mock):
        requests_mock.get(f"{BASE}/api/runs/r1", json={"id": "r1"})
        assert hook._request("GET", "/api/runs/r1") == {"id": "r1"}

    def test_204_returns_empty_dict(self, hook, requests_mock):
        requests_mock.post(f"{BASE}/api/runs/r1/end", status_code=204)
        assert hook._request("POST", "/api/runs/r1/end") == {}

    def test_404_raises_by_default(self, hook, requests_mock):
        requests_mock.get(f"{BASE}/api/runs/nope", status_code=404)
        with pytest.raises(TelomereApiError) as exc_info:
            hook._request("GET", "/api/runs/nope")
        assert exc_info.value.status_code == 404

    def test_404_returns_none_with_ok_404(self, hook, requests_mock):
        requests_mock.get(f"{BASE}/api/runs/nope", status_code=404)
        assert hook._request("GET", "/api/runs/nope", ok_404=True) is None

    def test_api_error_message_extracted(self, hook, requests_mock):
        requests_mock.get(f"{BASE}/api/runs/r1", status_code=400, json={"error": "bad input"})
        with pytest.raises(TelomereApiError, match="Telomere API error: bad input") as exc_info:
            hook._request("GET", "/api/runs/r1")
        assert exc_info.value.status_code == 400

    def test_non_json_error_body_tolerated(self, hook, requests_mock):
        requests_mock.get(f"{BASE}/api/runs/r1", status_code=500, text="oops")
        with pytest.raises(TelomereApiError, match="HTTP error") as exc_info:
            hook._request("GET", "/api/runs/r1")
        assert exc_info.value.status_code == 500

    def test_connection_error(self, hook, requests_mock):
        requests_mock.get(f"{BASE}/api/runs/r1", exc=requests.exceptions.ConnectionError)
        with pytest.raises(TelomereConnectionError, match="Failed to connect"):
            hook._request("GET", "/api/runs/r1")

    def test_timeout_error(self, hook, requests_mock):
        requests_mock.get(f"{BASE}/api/runs/r1", exc=requests.exceptions.ReadTimeout)
        with pytest.raises(TelomereConnectionError, match="timed out"):
            hook._request("GET", "/api/runs/r1")


class TestEnsureLifecycle:
    def test_returns_existing_without_create(self, hook, requests_mock):
        get = requests_mock.get(f"{BASE}/api/lifecycles/foo", json={"name": "foo"})
        post = requests_mock.post(f"{BASE}/api/lifecycles", json={"name": "foo"})
        assert hook.ensure_lifecycle("foo") == {"name": "foo"}
        assert get.called
        assert not post.called

    def test_creates_when_missing(self, hook, requests_mock):
        requests_mock.get(f"{BASE}/api/lifecycles/foo", status_code=404)
        post = requests_mock.post(f"{BASE}/api/lifecycles", json={"name": "foo"})
        result = hook.ensure_lifecycle(
            "foo", default_timeout_seconds=60, description="d", default_tags={"a": "b"}
        )
        assert result == {"name": "foo"}
        assert post.last_request.json() == {
            "name": "foo",
            "defaultTimeoutSeconds": 60,
            "description": "d",
            "defaultTags": {"a": "b"},
        }

    def test_conflict_on_create_fetches_existing(self, hook, requests_mock):
        # Concurrent creation: GET 404s, POST 409s, second GET wins.
        requests_mock.get(
            f"{BASE}/api/lifecycles/foo",
            [{"status_code": 404}, {"json": {"name": "foo", "id": "L1"}}],
        )
        requests_mock.post(f"{BASE}/api/lifecycles", status_code=409, json={"error": "conflict"})
        assert hook.ensure_lifecycle("foo") == {"name": "foo", "id": "L1"}

    def test_other_create_error_raises(self, hook, requests_mock):
        requests_mock.get(f"{BASE}/api/lifecycles/foo", status_code=404)
        requests_mock.post(f"{BASE}/api/lifecycles", status_code=500)
        with pytest.raises(TelomereApiError):
            hook.ensure_lifecycle("foo")


class TestRuns:
    def test_start_run(self, hook, requests_mock):
        post = requests_mock.post(f"{BASE}/api/lifecycles/foo/runs", json={"id": "r1"})
        run = hook.start_run("foo", timeout_seconds=120, tags={"k": "v"}, url="http://x")
        assert run == {"id": "r1"}
        assert post.last_request.json() == {
            "timeoutSeconds": 120,
            "tags": {"k": "v"},
            "url": "http://x",
        }

    @pytest.mark.parametrize("action", ["end", "fail"])
    def test_resolve_run(self, hook, requests_mock, action):
        post = requests_mock.post(f"{BASE}/api/runs/r1/{action}", json={"id": "r1"})
        method = hook.end_run if action == "end" else hook.fail_run
        assert method("r1", message="msg") == {"id": "r1"}
        assert post.last_request.json() == {"message": "msg"}

    @pytest.mark.parametrize("action", ["end", "fail"])
    def test_resolve_run_already_ended_409_is_success(self, hook, requests_mock, action):
        # Finalize retries may call end/fail twice; the second must not raise.
        requests_mock.post(
            f"{BASE}/api/runs/r1/{action}", status_code=409, json={"error": "already ended"}
        )
        method = hook.end_run if action == "end" else hook.fail_run
        assert method("r1") == {}

    @pytest.mark.parametrize("action", ["end", "fail"])
    def test_resolve_run_other_error_raises(self, hook, requests_mock, action):
        requests_mock.post(f"{BASE}/api/runs/r1/{action}", status_code=500)
        method = hook.end_run if action == "end" else hook.fail_run
        with pytest.raises(TelomereApiError):
            method("r1")

    def test_get_run(self, hook, requests_mock):
        requests_mock.get(f"{BASE}/api/runs/r1", json={"id": "r1", "state": "running"})
        assert hook.get_run("r1")["state"] == "running"

    def test_respawn(self, hook, requests_mock):
        post = requests_mock.post(f"{BASE}/api/lifecycles/foo/respawn", json={"id": "r2"})
        hook.respawn("foo", timeout_seconds=60, tags={"t": "1"}, previous_run_resolution="fail")
        assert post.last_request.json() == {
            "previousRunResolution": "fail",
            "timeoutSeconds": 60,
            "tags": {"t": "1"},
        }


class TestUnspawn:
    def test_missing_lifecycle_is_noop(self, hook, requests_mock):
        requests_mock.get(f"{BASE}/api/lifecycles/foo", status_code=404)
        post = requests_mock.post(f"{BASE}/api/lifecycles/foo/unspawn", json={})
        assert hook.unspawn("foo") == {"endedRuns": []}
        assert not post.called

    def test_unspawn_existing(self, hook, requests_mock):
        requests_mock.get(f"{BASE}/api/lifecycles/foo", json={"name": "foo"})
        post = requests_mock.post(f"{BASE}/api/lifecycles/foo/unspawn", json={"endedRuns": [1]})
        assert hook.unspawn("foo", resolution="fail") == {"endedRuns": [1]}
        assert post.last_request.json() == {"resolution": "fail"}


class TestConnection:
    def test_test_connection_success(self, hook, requests_mock):
        requests_mock.get(f"{BASE}/api/lifecycles", json={"lifecycles": []})
        ok, msg = hook.test_connection()
        assert ok
        assert msg == "Connection successful"

    def test_test_connection_failure(self, hook, requests_mock):
        requests_mock.get(f"{BASE}/api/lifecycles", status_code=401, json={"error": "no auth"})
        ok, msg = hook.test_connection()
        assert not ok
        assert "no auth" in msg

    def test_ui_field_behaviour(self):
        behaviour = TelomereHook.get_ui_field_behaviour()
        assert behaviour["relabeling"]["password"] == "API Key"
