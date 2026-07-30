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

import pymysql
from django.core.files.base import ContentFile
from django.test import SimpleTestCase, TestCase

from .dump_import import (
    STAGING_HOST, STAGING_PASSWORD, STAGING_PORT, STAGING_USER,
    _extract_subset, _safe_dt, derive_author_type, process_dump_upload,
)
from .models import DumpUpload, Project, TicketReply, TicketSnapshot


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
        dump_path = self._write_dump(["tbltickets", "tblticketdepartments"])  # no tblticketreplies
        subset_path = os.path.join(self.tmpdir, "subset.sql")
        with self.assertRaises(ValueError) as ctx:
            _extract_subset(dump_path, subset_path)
        self.assertIn("tblticketreplies", str(ctx.exception))

    def test_keeps_only_wanted_tables(self):
        dump_path = self._write_dump(
            ["tblclients", "tbltickets", "tblticketreplies", "tblticketdepartments"],
        )
        subset_path = os.path.join(self.tmpdir, "subset.sql")
        _extract_subset(dump_path, subset_path)
        with open(subset_path) as f:
            content = f.read()
        self.assertIn("tbltickets", content)
        self.assertIn("tblticketreplies", content)
        self.assertIn("tblticketdepartments", content)
        self.assertNotIn("tblclients", content)


SYNTHETIC_DUMP = b"""-- Table structure for table `tblticketdepartments`
DROP TABLE IF EXISTS `tblticketdepartments`;
CREATE TABLE `tblticketdepartments` (
  `id` int(10) unsigned NOT NULL AUTO_INCREMENT,
  `name` varchar(200) NOT NULL DEFAULT '',
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8;
INSERT INTO `tblticketdepartments` VALUES (1,'Technical Support');

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
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8;
INSERT INTO `tblticketreplies` VALUES (5001,101,'','','2026-07-20 09:20:00','Please try clearing your browser cache and reconnecting.','Bob Agent',0);
INSERT INTO `tblticketreplies` VALUES (5002,101,'Carol Contact','carol@example.com','2026-07-20 09:40:00','That worked, thank you!','',7);
INSERT INTO `tblticketreplies` VALUES (6001,102,'','','2026-07-21 10:30:00','Checking your account disk usage now.','Bob Agent',0);
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
        replies = {r.whmcs_reply_id: r for r in ticket.ticket_replies.all()}
        self.assertEqual(replies["0"].author_type, TicketReply.OWNER)
        self.assertEqual(replies["5001"].author_type, TicketReply.OPERATOR)
        self.assertEqual(replies["5002"].author_type, TicketReply.CONTACT)

        still_open = TicketSnapshot.objects.get(project=project, whmcs_ticket_id=102)
        self.assertEqual(still_open.status, "Open")
        self.assertIsNone(still_open.closed_at)

    def test_missing_table_marks_upload_failed_and_keeps_file_for_debugging(self):
        project = Project.objects.create(name="Bad Dump Project", source_type=Project.SOURCE_DUMP)
        upload = _make_upload(project, "bad.sql", BAD_DUMP)

        with self.assertRaises(ValueError):
            process_dump_upload(upload.id)

        upload.refresh_from_db()
        self.assertEqual(upload.status, DumpUpload.STATUS_FAILED)
        self.assertIn("tbltickets", upload.error_message)
        self.assertTrue(upload.file)  # kept, not deleted, since processing failed
