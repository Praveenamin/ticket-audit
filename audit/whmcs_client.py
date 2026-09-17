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
        except requests.exceptions.Timeout as exc:
            raise WHMCSAPIError(f"WHMCS API timeout calling {action}") from exc
        except requests.exceptions.RequestException as exc:
            raise WHMCSAPIError(f"WHMCS API error calling {action}: {exc}") from exc

        # Some WHMCS installs answer an auth failure (e.g. "Invalid or missing
        # credentials") with a non-2xx status *and* a well-formed JSON error
        # body -- checking the body first, before raise_for_status(), means
        # that message reaches the caller instead of being swallowed into a
        # generic "403 Forbidden" with no explanation. Only fall back to the
        # raw HTTP status when there's no JSON body to explain the failure at
        # all (e.g. a firewall/CDN block returning an HTML page).
        try:
            return response.json()
        except ValueError as exc:
            try:
                response.raise_for_status()
            except requests.exceptions.RequestException as http_exc:
                raise WHMCSAPIError(f"WHMCS API error calling {action}: {http_exc}") from http_exc
            raise WHMCSAPIError(f"WHMCS API returned non-JSON response for {action}") from exc

    def get_tickets_page(self, limitstart=0, limitnum=25, **extra):
        return self.call("GetTickets", limitstart=limitstart, limitnum=limitnum, **extra)

    def iter_tickets(self, page_size=100, stop_at_lastreply=None, **extra):
        """Yields ticket summary dicts across all pages of GetTickets. Stops (with a
        logged warning) if a page comes back as an error rather than raising, since a
        mid-pass permission/transient issue shouldn't lose tickets already synced.

        If stop_at_lastreply is given (a WHMCS-format 'YYYY-MM-DD HH:MM:SS' string --
        plain string, not a parsed datetime: this fixed-width format sorts identically
        as strings or as real timestamps, so no timezone-aware parsing needs to live in
        this thin-wrapper module), requests orderby=lastreply&order=desc and stops as
        soon as a ticket's own lastreply is STRICTLY LESS than that watermark -- not
        <=, deliberately: WHMCS's timestamp precision is whole seconds, so two distinct
        tickets can legitimately tie exactly at the watermark, and treating "at the
        watermark" as "already seen" risks silently dropping one that's actually new/
        changed. Re-yielding the handful tied at the boundary every pass is a
        deliberately cheap safety margin. stop_at_lastreply=None (a project's very
        first sync -- no watermark yet) walks every ticket exactly as before, no forced
        ordering."""
        start = 0
        extra = dict(extra)
        if stop_at_lastreply is not None:
            extra.setdefault("orderby", "lastreply")
            extra.setdefault("order", "desc")
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
                if stop_at_lastreply is not None and (ticket.get("lastreply") or "") < stop_at_lastreply:
                    return
                yield ticket
            start += len(tickets)
            if start >= int(result.get("totalresults", 0) or 0):
                return

    def get_ticket(self, ticket_id):
        return self.call("GetTicket", ticketid=ticket_id)
