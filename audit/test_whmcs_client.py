"""Closed-set tests for WHMCSClient.call()'s transport-vs-payload-error split
-- no real network calls, requests.post is mocked throughout."""

from unittest.mock import Mock, patch

from django.test import SimpleTestCase

import requests

from .whmcs_client import WHMCSAPIError, WHMCSClient


def make_client():
    return WHMCSClient(base_url="https://example.com", identifier="id", secret="secret", timeout=5)


def mock_response(status_code=200, json_data=None, json_error=False):
    response = Mock()
    response.status_code = status_code
    if json_error:
        response.json.side_effect = ValueError("not json")
    else:
        response.json.return_value = json_data
    if status_code >= 400:
        response.raise_for_status.side_effect = requests.exceptions.HTTPError(
            f"{status_code} Client Error: Forbidden for url: ..."
        )
    else:
        response.raise_for_status.return_value = None
    return response


class WHMCSClientCallTests(SimpleTestCase):
    @patch("audit.whmcs_client.requests.post")
    def test_200_with_success_payload_returns_it(self, mock_post):
        mock_post.return_value = mock_response(200, {"result": "success", "tickets": {}})

        result = make_client().call("GetTickets")

        self.assertEqual(result, {"result": "success", "tickets": {}})

    @patch("audit.whmcs_client.requests.post")
    def test_200_with_whmcs_error_payload_returns_it_not_an_exception(self, mock_post):
        mock_post.return_value = mock_response(200, {"result": "error", "message": "Permission Denied"})

        result = make_client().call("GetTickets")

        self.assertEqual(result, {"result": "error", "message": "Permission Denied"})

    @patch("audit.whmcs_client.requests.post")
    def test_non_2xx_with_whmcs_json_error_body_returns_it_not_an_exception(self, mock_post):
        # The exact case this app hit in production: WHMCS answers a bad
        # credential with HTTP 403 but a normal, parseable JSON error body --
        # that message must reach the caller, not get swallowed into a bare
        # "403 Forbidden".
        mock_post.return_value = mock_response(
            403, {"result": "error", "message": "Invalid or missing credentials"},
        )

        result = make_client().call("GetTickets")

        self.assertEqual(result, {"result": "error", "message": "Invalid or missing credentials"})

    @patch("audit.whmcs_client.requests.post")
    def test_non_2xx_with_no_json_body_raises_with_http_detail(self, mock_post):
        mock_post.return_value = mock_response(403, json_error=True)

        with self.assertRaises(WHMCSAPIError) as ctx:
            make_client().call("GetTickets")
        self.assertIn("403", str(ctx.exception))

    @patch("audit.whmcs_client.requests.post")
    def test_200_with_non_json_body_raises(self, mock_post):
        mock_post.return_value = mock_response(200, json_error=True)

        with self.assertRaises(WHMCSAPIError):
            make_client().call("GetTickets")

    @patch("audit.whmcs_client.requests.post")
    def test_timeout_raises_whmcs_api_error(self, mock_post):
        mock_post.side_effect = requests.exceptions.Timeout("timed out")

        with self.assertRaises(WHMCSAPIError):
            make_client().call("GetTickets")

    @patch("audit.whmcs_client.requests.post")
    def test_connection_error_raises_whmcs_api_error(self, mock_post):
        mock_post.side_effect = requests.exceptions.ConnectionError("refused")

        with self.assertRaises(WHMCSAPIError):
            make_client().call("GetTickets")
