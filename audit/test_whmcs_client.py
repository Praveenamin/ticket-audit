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


def _tickets_response(tickets, totalresults=None):
    return {
        "result": "success",
        "totalresults": totalresults if totalresults is not None else len(tickets),
        "tickets": {"ticket": tickets},
    }


class IterTicketsTests(SimpleTestCase):
    """iter_tickets' own pagination/early-stop logic -- mocks get_tickets_page
    directly (not requests.post) so each test controls page contents precisely
    without needing a second layer of JSON-shaped fixtures."""

    def test_no_watermark_walks_every_ticket_no_forced_ordering(self):
        client = make_client()
        page1 = _tickets_response(
            [{"id": 1, "lastreply": "2026-01-01 00:00:00"}], totalresults=2,
        )
        page2 = _tickets_response(
            [{"id": 2, "lastreply": "2020-01-01 00:00:00"}], totalresults=2,
        )
        with patch.object(client, "get_tickets_page", side_effect=[page1, page2]) as mock_page:
            tickets = list(client.iter_tickets(page_size=1))

        self.assertEqual([t["id"] for t in tickets], [1, 2])
        for call in mock_page.call_args_list:
            self.assertNotIn("orderby", call.kwargs)
            self.assertNotIn("order", call.kwargs)

    def test_watermark_given_requests_ordering(self):
        client = make_client()
        with patch.object(client, "get_tickets_page", return_value=_tickets_response([])) as mock_page:
            list(client.iter_tickets(stop_at_lastreply="2026-01-01 00:00:00"))

        mock_page.assert_called_once_with(limitstart=0, limitnum=100, orderby="lastreply", order="desc")

    def test_stops_after_yielding_tickets_at_or_above_watermark(self):
        client = make_client()
        page = _tickets_response([
            {"id": 1, "lastreply": "2026-01-03 00:00:00"},  # above watermark
            {"id": 2, "lastreply": "2026-01-02 00:00:00"},  # exactly at watermark -- still yielded
            {"id": 3, "lastreply": "2026-01-01 00:00:00"},  # strictly below -- stop here
            {"id": 4, "lastreply": "2020-01-01 00:00:00"},  # never reached
        ])
        with patch.object(client, "get_tickets_page", return_value=page):
            tickets = list(client.iter_tickets(stop_at_lastreply="2026-01-02 00:00:00"))

        self.assertEqual([t["id"] for t in tickets], [1, 2])

    def test_default_page_size_is_100(self):
        client = make_client()
        with patch.object(client, "get_tickets_page", return_value=_tickets_response([])) as mock_page:
            list(client.iter_tickets())

        self.assertEqual(mock_page.call_args.kwargs["limitnum"], 100)

    def test_explicit_page_size_override_respected(self):
        client = make_client()
        with patch.object(client, "get_tickets_page", return_value=_tickets_response([])) as mock_page:
            list(client.iter_tickets(page_size=10))

        self.assertEqual(mock_page.call_args.kwargs["limitnum"], 10)
