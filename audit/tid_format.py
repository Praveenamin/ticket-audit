"""Some WHMCS instances mask their ticket IDs (WHMCS's own "Ticket ID Masking"
Support Setting) as "XXX-NNNNNN" (3 letters, dash, 6 digits) -- e.g. Az-whmcs
and Dev both have this enabled, so their real tid values already look like
that. Others hand back a real, distinct, non-masked ticket number instead
(confirmed on Stackbill-Whmcs: ticket #143966 internally has real tid
"453611" -- not letter-masked, and not equal to the internal id either) --
generating our own replacement for those would throw away a real ticket
number the client/support system already knows the ticket by. Whether to do
that generation at all is a per-project setting (Project.use_synthetic_tid)
precisely because "doesn't look letter-masked" turned out not to reliably
mean "not a real ticket number" across different WHMCS installs.

Shared by both ingestion paths (dump_import.py and sync.py) so a ticket's
displayed ID looks the same regardless of which WHMCS instance's own masking
setting happened to be on, for this project and any future one.
"""

import hashlib
import re

_MASKED_TID_RE = re.compile(r"^[A-Z]{3}-\d{6}$")


def format_tid(raw_tid, whmcs_ticket_id, synthesize=True):
    """If `synthesize` is False (Project.use_synthetic_tid off), always use
    `raw_tid` exactly as WHMCS provided it -- falling back to the internal
    ticket id only if WHMCS genuinely gave us nothing at all. Otherwise
    (the default): keep `raw_tid` as-is if it already looks like WHMCS's own
    masked format, else generate the same visual shape ourselves -- a
    3-letter prefix, deterministically derived from `whmcs_ticket_id`
    (stable across re-syncs -- the same ticket always gets the same
    generated tid, not a fresh random one each import), plus the real
    ticket id zero-padded to 6 digits (kept as the literal id, not further
    obscured, so it's always traceable back to the real WHMCS ticket --
    unlike WHMCS's own masking, which isn't reversible)."""
    raw_tid = (raw_tid or "").strip()

    if not synthesize:
        return raw_tid or str(whmcs_ticket_id)

    if _MASKED_TID_RE.match(raw_tid):
        return raw_tid

    digest = hashlib.md5(str(whmcs_ticket_id).encode()).hexdigest()
    letters = "".join(chr(65 + int(digest[i * 2:i * 2 + 2], 16) % 26) for i in range(3))
    return f"{letters}-{int(whmcs_ticket_id):06d}"
