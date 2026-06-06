"""
tests/test_app.py

Tests are organized into three groups:
  - Pure functions (no I/O, no mocking needed)
  - DO API helpers (mock urllib to avoid real network calls)
  - Flask routes (use test client, mock run_rotation)
"""

import threading
import time
import urllib.error
from unittest.mock import MagicMock, call, patch

import pytest

import app
from app import (
    cleanup_unassigned_reserved_ips,
    do_req,
    split_fqdn,
)


# ---------------------------------------------------------------------------
# split_fqdn — pure function, no mocking needed
# ---------------------------------------------------------------------------

class TestSplitFqdn:
    def test_standard_subdomain(self):
        assert split_fqdn("toronto.jxue.ca") == ("toronto", "jxue.ca")

    def test_multi_label_subdomain(self):
        assert split_fqdn("sub.toronto.jxue.ca") == ("sub.toronto", "jxue.ca")

    def test_www(self):
        assert split_fqdn("www.example.com") == ("www", "example.com")

    def test_too_short_raises(self):
        with pytest.raises(ValueError, match="at least 3 labels"):
            split_fqdn("jxue.ca")

    def test_single_label_raises(self):
        with pytest.raises(ValueError):
            split_fqdn("localhost")


# ---------------------------------------------------------------------------
# do_req — mock urllib so we never make real network calls
# ---------------------------------------------------------------------------

class TestDoReq:
    def test_raises_runtime_error_on_http_error(self):
        http_err = urllib.error.HTTPError(
            url="https://api.digitalocean.com/v2/test",
            code=422,
            msg="Unprocessable Entity",
            hdrs=None,
            fp=None,
        )
        http_err.read = lambda: b'{"id":"unprocessable_entity"}'

        with patch("urllib.request.urlopen", side_effect=http_err):
            with pytest.raises(RuntimeError) as exc:
                do_req("GET", "/reserved_ips")
            assert "422" in str(exc.value)

    def test_raises_runtime_error_on_network_failure(self):
        with patch("urllib.request.urlopen", side_effect=OSError("timed out")):
            with pytest.raises(RuntimeError, match="network error"):
                do_req("GET", "/reserved_ips")

    def test_returns_parsed_json_on_success(self):
        mock_resp = MagicMock()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.read.return_value = b'{"reserved_ips": []}'

        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = do_req("GET", "/reserved_ips")
        assert result == {"reserved_ips": []}

    def test_returns_empty_dict_on_empty_response(self):
        mock_resp = MagicMock()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.read.return_value = b""

        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = do_req("DELETE", "/reserved_ips/1.2.3.4")
        assert result == {}


# ---------------------------------------------------------------------------
# cleanup_unassigned_reserved_ips — mock do_paginate and delete_reserved_ip
# ---------------------------------------------------------------------------

class TestCleanupUnassigned:
    def test_deletes_only_unassigned_ips(self):
        mock_ips = [
            {"ip": "1.2.3.4", "droplet": {"id": 99999999}},  # assigned — skip
            {"ip": "5.6.7.8", "droplet": None},               # unassigned — delete
            {"ip": "9.10.11.12", "droplet": None},            # unassigned — delete
        ]
        with patch("app.do_paginate", return_value=mock_ips):
            with patch("app.delete_reserved_ip") as mock_delete:
                cleanup_unassigned_reserved_ips()

        deleted = [c.args[0] for c in mock_delete.call_args_list]
        assert "5.6.7.8" in deleted
        assert "9.10.11.12" in deleted
        assert "1.2.3.4" not in deleted

    def test_does_nothing_when_all_ips_are_assigned(self):
        mock_ips = [
            {"ip": "1.2.3.4", "droplet": {"id": 99999999}},
        ]
        with patch("app.do_paginate", return_value=mock_ips):
            with patch("app.delete_reserved_ip") as mock_delete:
                cleanup_unassigned_reserved_ips()

        mock_delete.assert_not_called()

    def test_continues_after_individual_delete_failure(self):
        """A single failed delete must not prevent cleanup of remaining IPs."""
        mock_ips = [
            {"ip": "1.1.1.1", "droplet": None},
            {"ip": "2.2.2.2", "droplet": None},
            {"ip": "3.3.3.3", "droplet": None},
        ]
        with patch("app.do_paginate", return_value=mock_ips):
            with patch(
                "app.delete_reserved_ip",
                side_effect=[Exception("DO API error"), None, None],
            ) as mock_delete:
                cleanup_unassigned_reserved_ips()  # must not raise

        assert mock_delete.call_count == 3

    def test_handles_empty_ip_list(self):
        with patch("app.do_paginate", return_value=[]):
            with patch("app.delete_reserved_ip") as mock_delete:
                cleanup_unassigned_reserved_ips()
        mock_delete.assert_not_called()


# ---------------------------------------------------------------------------
# Flask routes
# ---------------------------------------------------------------------------

class TestIndexRoute:
    def test_returns_200(self, client):
        resp = client.get("/")
        assert resp.status_code == 200

    def test_returns_html(self, client):
        resp = client.get("/")
        assert b"REFRESH" in resp.data


class TestRefreshRoute:
    def test_returns_ok_on_successful_rotation(self, client):
        with patch("app.run_rotation", return_value="1.2.3.4"):
            resp = client.post("/refresh")
        assert resp.status_code == 200
        assert resp.get_json() == {"ok": True}

    def test_returns_500_when_rotation_raises(self, client):
        with patch("app.run_rotation", side_effect=RuntimeError("DO API timeout")):
            resp = client.post("/refresh")
        assert resp.status_code == 500
        assert resp.get_json()["ok"] is False

    def test_returns_429_during_cooldown(self, client):
        app._last_rotation = time.time()  # simulate a very recent rotation
        resp = client.post("/refresh")
        assert resp.status_code == 429
        assert resp.get_json()["ok"] is False

    def test_returns_200_after_cooldown_elapses(self, client):
        # Set last rotation to just beyond the cooldown window.
        app._last_rotation = time.time() - app.COOLDOWN - 1
        with patch("app.run_rotation", return_value="1.2.3.4"):
            resp = client.post("/refresh")
        assert resp.status_code == 200

    def test_returns_409_when_rotation_already_in_progress(self, client):
        app._lock.acquire()
        try:
            resp = client.post("/refresh")
        finally:
            app._lock.release()
        assert resp.status_code == 409
        assert resp.get_json()["ok"] is False

    def test_updates_last_rotation_timestamp_on_success(self, client):
        before = time.time()
        with patch("app.run_rotation", return_value="1.2.3.4"):
            client.post("/refresh")
        assert app._last_rotation >= before

    def test_does_not_update_timestamp_on_failure(self, client):
        app._last_rotation = 0.0
        with patch("app.run_rotation", side_effect=RuntimeError("fail")):
            client.post("/refresh")
        assert app._last_rotation == 0.0
