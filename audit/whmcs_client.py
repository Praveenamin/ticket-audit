"""Thin wrapper around the WHMCS action API (POST {base_url}/includes/api.php).

Mirrors the outbound-HTTP conventions used elsewhere in the StackSense stack
(synthetic.py / llm_analyzer.py): explicit timeout, transport-level failures
raised as a single typed exception, never left to bubble up as a raw requests
exception. WHMCS's own {"result": "error"} payloads (e.g. permission denied for
a given API action) are NOT exceptions here -- they're returned as normal JSON
so each caller can decide how to handle that specific action's failure.
"""

import logging

import requests
from django.conf import settings

logger = logging.getLogger("audit")


class WHMCSAPIError(Exception):
    """Raised only for transport-level failures (timeout, connection error, non-2xx,
    non-JSON body) -- not for WHMCS's own in-payload error responses."""


class WHMCSClient:
    def __init__(self, base_url=None, identifier=None, secret=None, timeout=None):
        self.base_url = (base_url or settings.WHMCS_BASE_URL).rstrip("/")
        self.identifier = identifier or settings.WHMCS_API_IDENTIFIER
        self.secret = secret or settings.WHMCS_API_SECRET
        self.timeout = timeout or settings.WHMCS_API_TIMEOUT
        self.endpoint = f"{self.base_url}/includes/api.php"

    def call(self, action, **params):
        payload = {
            "identifier": self.identifier,
            "secret": self.secret,
            "action": action,
            "responsetype": "json",
            **params,
        }
        try:
            response = requests.post(self.endpoint, data=payload, timeout=self.timeout)
            response.raise_for_status()
        except requests.exceptions.Timeout as exc:
            raise WHMCSAPIError(f"WHMCS API timeout calling {action}") from exc
        except requests.exceptions.RequestException as exc:
            raise WHMCSAPIError(f"WHMCS API error calling {action}: {exc}") from exc
        try:
            return response.json()
        except ValueError as exc:
            raise WHMCSAPIError(f"WHMCS API returned non-JSON response for {action}") from exc

    def get_tickets_page(self, limitstart=0, limitnum=25, **extra):
        return self.call("GetTickets", limitstart=limitstart, limitnum=limitnum, **extra)

    def iter_tickets(self, page_size=25, **extra):
        """Yields ticket summary dicts across all pages of GetTickets. Stops (with a
        logged warning) if a page comes back as an error rather than raising, since a
        mid-pass permission/transient issue shouldn't lose tickets already synced."""
        start = 0
        while True:
            try:
                result = self.get_tickets_page(limitstart=start, limitnum=page_size, **extra)
            except WHMCSAPIError as exc:
                logger.warning("GetTickets transport error at offset %s: %s", start, exc)
                return
            if result.get("result") != "success":
                logger.warning("GetTickets failed at offset %s: %s", start, result.get("message"))
                return
            tickets = result.get("tickets", {}).get("ticket", [])
            if isinstance(tickets, dict):
                # WHMCS's XML->JSON conversion sometimes collapses a single-item
                # array down to a bare object.
                tickets = [tickets]
            if not tickets:
                return
            for ticket in tickets:
                yield ticket
            start += len(tickets)
            if start >= int(result.get("totalresults", 0) or 0):
                return

    def get_ticket(self, ticket_id):
        return self.call("GetTicket", ticketid=ticket_id)
