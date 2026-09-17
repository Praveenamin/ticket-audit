"""Closed-set tests for the dump-import pipeline.

derive_author_type/_safe_dt/_extract_subset are pure functions tested as
plain unit tests -- no MySQL/staging DB needed. ProcessDumpUploadIntegration
Tests below is different: it runs the real pipeline (extract -> staging_db
import -> transform -> apply_sla_evaluation -> cleanup) against a small
synthetic dump, formalizing the manual dry-run that was done once by hand
before trusting this for a real production audit. It needs the real
staging_db service reachable (true whenever this suite is run the way this
project always runs it: `docker compose exec web python manage.py test
audit`) and skips itself with a clear reason if it isn't.
"""

import os
import shutil
import tempfile
import unittest
from datetime import datetime, timezone as dt_timezone
from unittest.mock import patch

import pymysql
from django.core.files.base import ContentFile
from django.test import SimpleTestCase, TestCase

from .dump_import import (
    STAGING_HOST, STAGING_PASSWORD, STAGING_PORT, STAGING_USER,
    _extract_subset, _safe_dt, client_display_name, derive_author_type, process_dump_upload,
)
from .models import DumpUpload, Project, TicketNote, TicketReply, TicketSnapshot


class DeriveAuthorTypeTests(SimpleTestCase):
    def test_admin_present_is_operator(self):
        self.assertEqual(derive_author_type("Jane Agent", 0), TicketReply.OPERATOR)

    def test_admin_present_beats_nonzero_contactid(self):
        # A staff reply should never be misread as a contact reply even if
        # contactid happens to be populated too.
        self.assertEqual(derive_author_type("Jane Agent", 5), TicketReply.OPERATOR)

    def test_admin_blank_with_nonzero_contactid_is_contact(self):
        self.assertEqual(derive_author_type("", 5), TicketReply.CONTACT)

    def test_admin_blank_with_none_contactid_and_zero_is_owner(self):
        self.assertEqual(derive_author_type("", 0), TicketReply.OWNER)

    def test_admin_none_and_contactid_none_is_owner(self):
        self.assertEqual(derive_author_type(None, None), TicketReply.OWNER)


class ClientDisplayNameTests(SimpleTestCase):
    def test_companyname_wins_over_personal_name(self):
        self.assertEqual(client_display_name("Acme Ltd", "Jane", "Doe", 1), "Acme Ltd")

    def test_falls_back_to_full_name_when_no_company(self):
        self.assertEqual(client_display_name("", "Jane", "Doe", 1), "Jane Doe")

    def test_falls_back_to_client_id_when_nothing_at_all(self):
        self.assertEqual(client_display_name("", "", "", 42), "Client 42")

    def test_none_values_treated_like_blank(self):
        self.assertEqual(client_display_name(None, None, None, 7), "Client 7")

    def test_whitespace_only_company_falls_through(self):
        self.assertEqual(client_display_name("   ", "Jane", "", 1), "Jane")

    def test_truncated_to_max_length(self):
        """Confirmed against real data: a companyname field over 1200 chars
        (garbage, not a real name) must never overflow Client.name."""
        huge = "A" * 900
        result = client_display_name(huge, "", "", 1)
        self.assertEqual(len(result), 500)
        self.assertEqual(result, huge[:500])


class SafeDtTests(SimpleTestCase):
    def test_none_stays_none(self):
        self.assertIsNone(_safe_dt(None))

    def test_zero_date_sentinel_becomes_none(self):
        # Python's datetime has no year 0, so year 1 is the practical floor
        # for MySQL's "0000-00-00"-style sentinel once pymysql hands it back.
        self.assertIsNone(_safe_dt(datetime(1, 1, 1, 0, 0, 0)))

    def test_valid_datetime_converted_from_ist_to_utc(self):
        result = _safe_dt(datetime(2026, 7, 20, 9, 0, 0))
        self.assertEqual(result, datetime(2026, 7, 20, 3, 30, 0, tzinfo=dt_timezone.utc))


class ExtractSubsetTests(SimpleTestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)

    def _write_dump(self, tables):
        dump_path = os.path.join(self.tmpdir, "full.sql")
        with open(dump_path, "w") as f:
            for table in tables:
                f.write(f"-- Table structure for table `{table}`\n")
                f.write(f"CREATE TABLE `{table}` (id INT);\n")
                f.write(f"INSERT INTO `{table}` VALUES (1);\n\n")
        return dump_path

    def test_missing_table_raises_naming_it(self):
        dump_path = self._write_dump(
            ["tbltickets", "tblticketnotes", "tblticketdepartments", "tblclients"]
        )  # no tblticketreplies
        subset_path = os.path.join(self.tmpdir, "subset.sql")
        with self.assertRaises(ValueError) as ctx:
            _extract_subset(dump_path, subset_path)
        self.assertIn("tblticketreplies", str(ctx.exception))

    def test_keeps_only_wanted_tables(self):
        dump_path = self._write_dump(
            [
                "tblcontacts", "tbltickets", "tblticketreplies", "tblticketnotes",
                "tblticketdepartments", "tblclients",
            ],
        )
        subset_path = os.path.join(self.tmpdir, "subset.sql")
        _extract_subset(dump_path, subset_path)
        with open(subset_path) as f:
            content = f.read()
        self.assertIn("tbltickets", content)
        self.assertIn("tblticketreplies", content)
        self.assertIn("tblticketnotes", content)
        self.assertIn("tblticketdepartments", content)
        self.assertIn("tblclients", content)
        self.assertNotIn("tblcontacts", content)


SYNTHETIC_DUMP = b"""-- Table structure for table `tblticketdepartments`
DROP TABLE IF EXISTS `tblticketdepartments`;
CREATE TABLE `tblticketdepartments` (
  `id` int(10) unsigned NOT NULL AUTO_INCREMENT,
  `name` varchar(200) NOT NULL DEFAULT '',
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8;
INSERT INTO `tblticketdepartments` VALUES (1,'Technical Support');

-- Table structure for table `tblclients`
DROP TABLE IF EXISTS `tblclients`;
CREATE TABLE `tblclients` (
  `id` int(10) unsigned NOT NULL AUTO_INCREMENT,
  `firstname` text NOT NULL,
  `lastname` text NOT NULL,
  `companyname` text NOT NULL,
  `email` text NOT NULL,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8;
INSERT INTO `tblclients` VALUES (55,'Alice','Client','Acme Hosting Ltd','alice@example.com');

-- Table structure for table `tbltickets`
DROP TABLE IF EXISTS `tbltickets`;
CREATE TABLE `tbltickets` (
  `id` int(10) unsigned NOT NULL AUTO_INCREMENT,
  `tid` varchar(50) NOT NULL DEFAULT '',
  `did` int(10) unsigned NOT NULL DEFAULT 0,
  `userid` int(10) unsigned NOT NULL DEFAULT 0,
  `date` datetime NOT NULL,
  `title` varchar(255) NOT NULL DEFAULT '',
  `message` mediumtext,
  `name` varchar(200) NOT NULL DEFAULT '',
  `email` varchar(200) NOT NULL DEFAULT '',
  `status` varchar(100) NOT NULL DEFAULT '',
  `urgency` varchar(50) NOT NULL DEFAULT '',
  `lastreply` datetime DEFAULT NULL,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8;
INSERT INTO `tbltickets` VALUES (101,'TICKET-1',1,55,'2026-07-20 09:00:00','cPanel login failing','I cannot log into cPanel, getting a 500 error.','Alice Client','alice@example.com','Closed','Medium','2026-07-20 09:45:00');
INSERT INTO `tbltickets` VALUES (102,'TICKET-2',1,55,'2026-07-21 10:00:00','Disk quota exceeded','My site says disk quota exceeded, please help.','Alice Client','alice@example.com','Open','High','2026-07-21 10:30:00');

-- Table structure for table `tblticketreplies`
DROP TABLE IF EXISTS `tblticketreplies`;
CREATE TABLE `tblticketreplies` (
  `id` int(10) unsigned NOT NULL AUTO_INCREMENT,
  `tid` int(10) unsigned NOT NULL DEFAULT 0,
  `name` varchar(200) NOT NULL DEFAULT '',
  `email` varchar(200) NOT NULL DEFAULT '',
  `date` datetime NOT NULL,
  `message` mediumtext,
  `admin` varchar(200) NOT NULL DEFAULT '',
  `contactid` int(10) unsigned NOT NULL DEFAULT 0,
  `rating` int(10) NOT NULL DEFAULT 0,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8;
INSERT INTO `tblticketreplies` VALUES (5001,101,'','','2026-07-20 09:20:00','Please try clearing your browser cache and reconnecting.','Bob Agent',0,0);
INSERT INTO `tblticketreplies` VALUES (5002,101,'Carol Contact','carol@example.com','2026-07-20 09:40:00','That worked, thank you!','',7,5);
INSERT INTO `tblticketreplies` VALUES (6001,102,'','','2026-07-21 10:30:00','Checking your account disk usage now.','Bob Agent',0,0);

-- Table structure for table `tblticketnotes`
DROP TABLE IF EXISTS `tblticketnotes`;
CREATE TABLE `tblticketnotes` (
  `id` int(10) unsigned NOT NULL AUTO_INCREMENT,
  `ticketid` int(10) unsigned NOT NULL DEFAULT 0,
  `date` datetime NOT NULL,
  `admin` varchar(200) NOT NULL DEFAULT '',
  `message` mediumtext,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8;
INSERT INTO `tblticketnotes` VALUES (9001,101,'2026-07-20 09:25:00','Bob Agent','Root cause was a stale session cookie -- cleared it server-side.');
"""

BAD_DUMP = b"-- nothing useful in this file at all\n"


def _make_upload(project, filename, content):
    upload = DumpUpload(project=project)
    upload.file.save(filename, ContentFile(content), save=True)
    return upload


class ProcessDumpUploadIntegrationTests(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        try:
            conn = pymysql.connect(
                host=STAGING_HOST, port=STAGING_PORT, user=STAGING_USER,
                password=STAGING_PASSWORD, connect_timeout=3,
            )
            conn.close()
        except Exception as exc:
            raise unittest.SkipTest(f"staging_db not reachable, skipping: {exc}")

    def test_full_pipeline_against_synthetic_dump(self):
        project = Project.objects.create(name="Integration Test Project", source_type=Project.SOURCE_DUMP)
        upload = _make_upload(project, "synthetic.sql", SYNTHETIC_DUMP)

        process_dump_upload(upload.id)

        upload.refresh_from_db()
        self.assertEqual(upload.status, DumpUpload.STATUS_DONE)
        self.assertEqual(upload.tickets_imported, 2)
        self.assertEqual(upload.error_message, "")
        self.assertFalse(upload.file)  # deleted on success

        ticket = TicketSnapshot.objects.get(project=project, whmcs_ticket_id=101)
        self.assertEqual(ticket.status, "Closed")
        self.assertEqual(ticket.department.name, "Technical Support")
        self.assertEqual(ticket.client.name, "Acme Hosting Ltd")
        replies = {r.whmcs_reply_id: r for r in ticket.ticket_replies.all()}
        self.assertEqual(replies["0"].author_type, TicketReply.OWNER)
        self.assertEqual(replies["5001"].author_type, TicketReply.OPERATOR)
        self.assertEqual(replies["5002"].author_type, TicketReply.CONTACT)

        note = TicketNote.objects.get(ticket=ticket, whmcs_note_id="9001")
        self.assertEqual(note.admin_name, "Bob Agent")
        self.assertIn("stale session cookie", note.message)
        still_open_notes = TicketSnapshot.objects.get(project=project, whmcs_ticket_id=102).ticket_notes.all()
        self.assertEqual(list(still_open_notes), [])

        still_open = TicketSnapshot.objects.get(project=project, whmcs_ticket_id=102)
        self.assertEqual(still_open.status, "Open")
        self.assertIsNone(still_open.closed_at)

    def test_s3_archival_never_touches_a_dump_imported_project(self):
        # s3_archive_enabled=True *deliberately* -- proving the constraint holds
        # even if someone mistakenly flips the toggle on a dump-sourced project,
        # not just that nobody happened to enable it.
        project = Project.objects.create(
            name="Dump S3 Test Project", source_type=Project.SOURCE_DUMP, s3_archive_enabled=True,
        )
        upload = _make_upload(project, "synthetic.sql", SYNTHETIC_DUMP)

        with patch("audit.s3_archive.archive_reply", side_effect=AssertionError("must never be called")):
            process_dump_upload(upload.id)

        ticket = TicketSnapshot.objects.get(project=project, whmcs_ticket_id=101)
        self.assertFalse(ticket.s3_archive_eligible)
        for reply in ticket.ticket_replies.all():
            self.assertIsNone(reply.archived_to_s3_at)

    def test_missing_table_marks_upload_failed_and_keeps_file_for_debugging(self):
        project = Project.objects.create(name="Bad Dump Project", source_type=Project.SOURCE_DUMP)
        upload = _make_upload(project, "bad.sql", BAD_DUMP)

        with self.assertRaises(ValueError):
            process_dump_upload(upload.id)

        upload.refresh_from_db()
        self.assertEqual(upload.status, DumpUpload.STATUS_FAILED)
        self.assertIn("tbltickets", upload.error_message)
        self.assertTrue(upload.file)  # kept, not deleted, since processing failed
