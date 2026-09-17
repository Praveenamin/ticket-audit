"""boto3 wrapper for archiving ticket replies to S3, incrementally, as they arrive.

archive_reply() is called from two places: sync.py's _upsert_replies (the primary,
"archive it the sync pass it shows up" path) and retry_unarchived_replies() below (the
backstop for the one gap that path can't close on its own -- see its docstring). Both
share this one upload function rather than duplicating boto3 calls in two places.

Bucket/credentials are per-project (Project.s3_*, edited on that project's own admin
page) -- no Django settings/.env involved at all, matching how WHMCSClient already
takes base_url/identifier/secret as explicit constructor args rather than reading
global settings.

Each reply archives as a single .md object: a `---`-fenced YAML frontmatter block
(metadata) plus a Markdown body (the message, HTML-stripped with entities decoded and
paragraph breaks preserved for readability -- the raw HTML also stays in frontmatter as
message_html, so nothing is actually lost). Key scheme:
{project-slug}/{YYYY}/{MM}/T{tid}/{YYYYMMDD}-{HHMM}-{whmcs_reply_id}.md, where the
year/month is the TICKET's own opening month (every reply on a ticket lives under that
one fixed folder for the ticket's whole lifecycle, however much later a given reply
happens), and the filename's date+time is that individual reply's own posted_at --
both in Asia/Kolkata (IST), matching sync.py's own s3_archive_eligible calendar-day
convention.
"""

import html
import logging
import re

import boto3
import yaml
from botocore.exceptions import BotoCoreError, ClientError
from django.utils import timezone
from django.utils.text import slugify

from .models import TicketReply
from .sla import _TAG_RE

logger = logging.getLogger("audit")

FRONTMATTER_FENCE = "---"


def _yaml_multiline_str_presenter(dumper, data):
    # Renders any multi-line value (message_html, most notably) as a readable YAML
    # block literal ("|") instead of one long escaped line. PyYAML itself silently
    # falls back to an escaped double-quoted scalar when a value contains a bare \r
    # (block literals can't represent \r cleanly) -- safe to request "|"
    # unconditionally and let PyYAML downgrade it when it has to.
    style = "|" if "\n" in data else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style=style)


yaml.add_representer(str, _yaml_multiline_str_presenter)


def _message_to_markdown(text):
    """Render a WHMCS reply's raw message as clean, readable Markdown -- distinct
    from llm_audit._strip_html (which deliberately collapses everything to one line
    for a compact LLM prompt): an archived document should read like the original,
    not like a run-on sentence. Two real bugs this fixes, found from a real archived
    reply's output: (1) WHMCS stores some punctuation as HTML entities even in
    otherwise-plain-text messages (e.g. "platform&#039;s" for "platform's") -- never
    decoded before, so it showed up literally in the archived body; (2) collapsing
    ALL whitespace (the old approach) destroys the \\r\\n\\r\\n paragraph breaks a real
    multi-paragraph message relies on to read sensibly."""
    text = text or ""
    # Turn common block-level HTML into paragraph/line breaks *before* stripping
    # remaining tags, so structure from real HTML markup survives too, not just
    # from plain-text \r\n sequences.
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</p\s*>", "\n\n", text)
    text = re.sub(r"(?i)<p[^>]*>", "", text)
    text = _TAG_RE.sub("", text)
    text = html.unescape(text)

    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.split("\n")]
    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text)  # collapse 3+ blank lines to exactly one
    return text.strip()


def _client(project):
    return boto3.client(
        "s3",
        region_name=project.s3_region,
        aws_access_key_id=project.s3_access_key_id,
        aws_secret_access_key=project.s3_secret_access_key,
    )


def _reply_key(project, tid, ticket_opened_at, posted_at, whmcs_reply_id):
    # e.g. "stackbill-whmcs/2026/08/T453611/20260812-1516-0.md" for the real ticket
    # 453611's own opening message. Both halves localized via timezone.localtime()
    # (Asia/Kolkata) -- matching sync.py's own s3_archive_eligible calendar-day check.
    opened_ist = timezone.localtime(ticket_opened_at)
    posted_ist = timezone.localtime(posted_at)
    return (
        f"{slugify(project.name)}/{opened_ist:%Y}/{opened_ist:%m}/T{tid}/"
        f"{posted_ist:%Y%m%d}-{posted_ist:%H%M}-{whmcs_reply_id}.md"
    )


def _render_reply_markdown(frontmatter, body):
    """Exact on-disk shape for one archived reply's .md object: a `---`-fenced YAML
    block, a blank line, then the Markdown body -- the same frontmatter convention
    Jekyll/Hugo/Obsidian and friends use, so the file is directly readable by a human
    or any of those tools, not just by parse_reply_markdown below."""
    frontmatter_yaml = yaml.dump(frontmatter, sort_keys=False, allow_unicode=True, default_flow_style=False)
    return f"{FRONTMATTER_FENCE}\n{frontmatter_yaml}{FRONTMATTER_FENCE}\n\n{body}\n".encode("utf-8")


def parse_reply_markdown(text):
    """Inverse of _render_reply_markdown -- (frontmatter_dict, body_str). Not called
    anywhere in the ongoing archival pipeline itself (write-only, matching this
    feature's original design) -- exists so this exact shape has one correct,
    independently-tested implementation for a future reader (a debugging script, an
    admin "preview archived reply" view) to import rather than re-deriving the split.
    Unambiguous even if the body itself contains a literal "---" line: every value in
    the dumped frontmatter is either a quoted scalar or an indented block-literal
    continuation line (see _yaml_multiline_str_presenter above) -- yaml.dump() never
    emits a bare, unindented "---" line of its own, so the first "\\n---\\n" after the
    opening fence is always the real closing one, never a false match inside a value."""
    if isinstance(text, bytes):
        text = text.decode("utf-8")
    if not text.startswith(f"{FRONTMATTER_FENCE}\n"):
        raise ValueError("Missing opening frontmatter fence.")
    closing_marker = f"\n{FRONTMATTER_FENCE}\n"
    closing_index = text.index(closing_marker, len(FRONTMATTER_FENCE) + 1)
    frontmatter = yaml.safe_load(text[len(FRONTMATTER_FENCE) + 1 : closing_index])
    body = text[closing_index + len(closing_marker):].strip("\n")
    return frontmatter, body


def archive_reply(reply, project, tid):
    """Upload one TicketReply as a single .md object (YAML frontmatter + a clean-text
    Markdown body), then stamp archived_to_s3_at. Returns True on confirmed success,
    False on any boto3-level failure or missing config -- never raises for those
    expected cases (no dedicated exception type: there's no S3 equivalent of WHMCS's
    in-payload "result: error" case to distinguish from a transport failure, so one
    outcome category is enough)."""
    if not project.s3_bucket_name:
        logger.warning(
            "S3 archive skipped for reply %s: project %s has no bucket configured.",
            reply.id, project.name,
        )
        return False

    ticket = reply.ticket
    key = _reply_key(project, tid, ticket.opened_at, reply.posted_at, reply.whmcs_reply_id)
    frontmatter = {
        "whmcs_reply_id": reply.whmcs_reply_id,
        "whmcs_ticket_id": ticket.whmcs_ticket_id,
        "tid": tid,
        "ticket_subject": ticket.subject,
        "ticket_opened_at": ticket.opened_at.isoformat() if ticket.opened_at else None,
        "author_name": reply.author_name,
        "author_type": reply.author_type,
        "admin_name": reply.admin_name,
        "message_html": reply.message,
        "posted_at": reply.posted_at.isoformat() if reply.posted_at else None,
        "archived_at": timezone.now().isoformat(),
    }
    body = _message_to_markdown(reply.message)
    content = _render_reply_markdown(frontmatter, body)

    try:
        _client(project).put_object(
            Bucket=project.s3_bucket_name, Key=key, Body=content, ContentType="text/markdown",
        )
    except (BotoCoreError, ClientError) as exc:
        logger.warning("S3 archive upload failed for reply %s (key %s): %s", reply.id, key, exc)
        return False

    reply.archived_to_s3_at = timezone.now()
    reply.save(update_fields=["archived_to_s3_at"])
    return True


def retry_unarchived_replies(limit=50):
    """Backstop for the one gap the primary path can't close on its own: _upsert_replies
    only runs when a ticket's lastreply timestamp changes, so a reply whose upload
    fails on the exact pass it arrives is never revisited if that ticket then goes
    permanently dormant. Structurally identical to every other 'pick the not-yet-done
    rows, process a bounded batch' job in this app, just keyed off archived_to_s3_at
    IS NULL instead of a status field. Self-limiting: a reply drops out of this filter
    for good the moment it succeeds, so the query never grows with total ticket
    volume, only with the rare, real failure count."""
    replies = list(
        TicketReply.objects.filter(
            archived_to_s3_at__isnull=True,
            ticket__s3_archive_eligible=True,
            ticket__project__s3_archive_enabled=True,
        ).select_related("ticket", "ticket__project").order_by("posted_at")[:limit]
    )
    archived = 0
    for reply in replies:
        ticket = reply.ticket
        try:
            if archive_reply(reply, ticket.project, ticket.tid or ticket.whmcs_ticket_id):
                archived += 1
        except Exception:
            logger.exception("Unexpected error retrying S3 archive for reply %s", reply.id)
    return archived
