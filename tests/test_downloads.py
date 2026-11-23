"""Generated files (reports, exports) are saved in the file store and downloaded
through the server: only by the signed-in owner of the conversation."""
from __future__ import annotations

import io
import urllib.parse
import unittest
from unittest import mock

from openpyxl import load_workbook

from kai.server.app import attachment_header
from kai.tools.outputs import file_name
from tests.test_graph import call, route, say
from tests.test_server import ServerTestCase


class NameTests(unittest.TestCase):
    def test_file_names(self):
        self.assertEqual(file_name("Q1 Finances: summary!", "pdf"), "Q1_Finances_summary.pdf")
        self.assertEqual(file_name("تقرير الادخار", "docx"), "تقرير_الادخار.docx")
        self.assertEqual(file_name("", "xlsx", fallback="export"), "export.xlsx")
        self.assertEqual(file_name("../../etc/passwd", "csv"), "etcpasswd.csv")

    def test_attachment_header_keeps_arabic(self):
        header = attachment_header("تقرير_الادخار.pdf")
        self.assertIn('filename="file.pdf"', header)
        self.assertIn("filename*=UTF-8''" + urllib.parse.quote("تقرير_الادخار.pdf"), header)
        self.assertIn('filename="Q1_report.pdf"', attachment_header("Q1_report.pdf"))


class DownloadTests(ServerTestCase):
    def setUp(self):
        super().setUp()
        p = mock.patch("kai.tools.documents._render", return_value=b"%PDF-1.4 the report")
        p.start()
        self.addCleanup(p.stop)

    def make_report(self, title="Savings Plan", fmt="pdf"):
        tid = self.new_thread()
        r = self.post(tid, f"a {fmt} report", script=[
            route("documents"), call("create_report", {"title": title, "content": "## A\n- b", "format": fmt}),
            say("Here is your report.")])  # the model leaves the link out: the card still comes
        self.assertEqual(r.status_code, 200, r.text)
        [download] = r.json()["messages"][-1]["downloads"]
        return tid, download

    def test_report_download(self):
        tid, d = self.make_report()
        self.assertEqual((d["name"], d["format"]), ("Savings_Plan.pdf", "PDF"))
        self.assertRegex(d["url"], rf"^/api/threads/{tid}/files/f_[0-9a-f]{{8}}\.pdf$")

        r = self.client.get(d["url"], headers=self.as_alice)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.content, b"%PDF-1.4 the report")
        self.assertEqual(r.headers["content-type"], "application/pdf")
        self.assertIn('attachment; filename="Savings_Plan.pdf"', r.headers["content-disposition"])
        self.assertIn("no-store", r.headers["cache-control"])

        # History keeps the download card.
        history = self.client.get(f"/api/threads/{tid}", headers=self.as_alice).json()
        self.assertEqual(history["messages"][-1]["downloads"], [d])

    def test_only_the_owner_can_download(self):
        tid, d = self.make_report()
        self.assertEqual(self.client.get(d["url"]).status_code, 401)
        self.assertEqual(self.client.get(d["url"], headers=self.as_bob).status_code, 404)
        self.assertEqual(self.client.get(f"/api/threads/{tid}/files/f_00000000.pdf",
                                         headers=self.as_alice).status_code, 404)
        # Another thread of the same user cannot serve this file either.
        other = self.new_thread()
        moved = d["url"].replace(tid, other)
        self.assertEqual(self.client.get(moved, headers=self.as_alice).status_code, 404)

    def test_uploads_are_not_downloadable(self):
        tid = self.new_thread()
        r = self.post(tid, "here", files={"notes.txt": b"secret"}, script=[route("general"), say("ok")])
        self.assertEqual(r.status_code, 200, r.text)
        state_files = self.client.get(f"/api/threads/{tid}", headers=self.as_alice).json()
        self.assertNotIn("downloads", state_files["messages"][-1])

    def test_arabic_word_report(self):
        _, d = self.make_report("تقرير الادخار", "docx")
        r = self.client.get(d["url"], headers=self.as_alice)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers["content-type"],
                         "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
        self.assertIn(urllib.parse.quote("تقرير_الادخار.docx"), r.headers["content-disposition"])

    def test_missing_file_answers_404(self):
        tid, d = self.make_report()
        with mock.patch("kai.files.LocalFileStore.read_bytes", side_effect=FileNotFoundError):
            self.assertEqual(self.client.get(d["url"], headers=self.as_alice).status_code, 404)

    def test_excel_export(self):
        tid = self.new_thread()
        table = "| Month | Total |\n|---|---|\n| Jan | 1,200 |\n| Feb | 950.5 |"
        r = self.post(tid, "export to excel", script=[
            route("documents"),
            call("export_table_to_file", {"data": table, "file_format": "xlsx", "filename": "monthly totals"}),
            say("Done.")])
        self.assertEqual(r.status_code, 200, r.text)
        [d] = r.json()["messages"][-1]["downloads"]
        self.assertEqual((d["name"], d["format"]), ("monthly_totals.xlsx", "XLSX"))
        got = self.client.get(d["url"], headers=self.as_alice)
        self.assertEqual(got.status_code, 200)
        sheet = load_workbook(io.BytesIO(got.content)).active
        self.assertEqual([c.value for c in sheet[1]], ["Month", "Total"])
        self.assertEqual(sheet["B2"].value, 1200, "numbers stay numbers")


if __name__ == "__main__":
    unittest.main()
