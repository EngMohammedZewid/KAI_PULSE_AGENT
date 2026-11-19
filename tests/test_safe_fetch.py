"""The server never downloads internal addresses, and S3 has no fallback bucket."""
from __future__ import annotations

import os
import socket
import unittest
import urllib.request
from unittest import mock

from tools import _safe_fetch
from tools._safe_fetch import UnsafeURL, check_url, fetch


def resolves_to(ip: str):
    family = socket.AF_INET6 if ":" in ip else socket.AF_INET
    return mock.patch.object(_safe_fetch.socket, "getaddrinfo",
                             return_value=[(family, socket.SOCK_STREAM, 6, "", (ip, 443))])


def no_download(*_a, **_k):
    raise AssertionError("a download was attempted")


class CheckUrlTests(unittest.TestCase):
    def test_internal_addresses_are_refused(self):
        for url in ["http://169.254.169.254/latest/meta-data/", "http://127.0.0.1:8000/api/me",
                    "http://localhost/", "http://10.0.0.5/", "http://192.168.1.10/", "http://172.17.0.1/",
                    "http://[::1]/", "http://[::ffff:127.0.0.1]/", "http://0.0.0.0/", "http://100.64.0.1/"]:
            with self.subTest(url=url), self.assertRaises(UnsafeURL):
                check_url(url)

    def test_only_http_and_https(self):
        for url in ["file:///etc/passwd", "ftp://example.com/x", "gopher://x", "quickchart.io/chart"]:
            with self.subTest(url=url), self.assertRaises(UnsafeURL):
                check_url(url)

    def test_public_host_allowed_for_general_fetch(self):
        with resolves_to("93.184.216.34"):
            check_url("https://example.org/data.csv")

    def test_trusted_sources_only_for_images_and_attachments(self):
        with resolves_to("93.184.216.34"):
            check_url("https://quickchart.io/chart?c={}", trusted_only=True)
            check_url("https://my-bucket.s3.us-east-2.amazonaws.com/reports/a.pdf?X-Amz=1", trusted_only=True)
            check_url("https://b.s3.eu-de.cloud-object-storage.appdomain.cloud/c.png", trusted_only=True)
            for url in ["https://example.org/a.png", "https://quickchart.io.evil.com/a.png",
                        "https://notquickchart.io/a.png"]:
                with self.subTest(url=url), self.assertRaises(UnsafeURL):
                    check_url(url, trusted_only=True)

    def test_extra_trusted_hosts_from_settings(self):
        with resolves_to("93.184.216.34"), mock.patch.dict(os.environ, {"KAI_FETCH_ALLOWED_HOSTS": "charts.example.com"}):
            check_url("https://charts.example.com/a.png", trusted_only=True)
            check_url("https://eu.charts.example.com/a.png", trusted_only=True)

    def test_trusted_name_pointing_inside_is_refused(self):
        with resolves_to("10.1.2.3"), self.assertRaises(UnsafeURL):
            check_url("https://quickchart.io/chart", trusted_only=True)

    def test_redirect_to_internal_address_is_refused(self):
        handler = _safe_fetch._CheckRedirects(trusted_only=False)
        req = urllib.request.Request("https://example.org/a")
        with self.assertRaises(UnsafeURL):
            handler.redirect_request(req, None, 302, "Found", {}, "http://169.254.169.254/latest/meta-data/")

    def test_size_limit(self):
        class Resp:
            headers = {"Content-Type": "image/png"}
            def read(self, n): return b"x" * min(n, 500)  # a 500-byte file
            def __enter__(self): return self
            def __exit__(self, *a): return False

        opener = mock.Mock(open=mock.Mock(return_value=Resp()))
        with resolves_to("93.184.216.34"), mock.patch.object(_safe_fetch.urllib.request, "build_opener",
                                                             return_value=opener):
            with self.assertRaises(UnsafeURL):
                fetch("https://quickchart.io/c", max_bytes=100)
            body, ctype = fetch("https://quickchart.io/c", max_bytes=1000)
            self.assertEqual((len(body), ctype), (500, "image/png"))


class ToolsUseSafeFetchTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(_safe_fetch.urllib.request, "build_opener", side_effect=no_download)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_report_image_from_internal_address_is_not_downloaded(self):
        from tools import pdf_tool

        pdf = pdf_tool._build_pdf("Report", "![x](http://169.254.169.254/latest/meta-data/iam)")
        self.assertTrue(pdf.startswith(b"%PDF"))

    def test_email_attachment_must_come_from_trusted_storage(self):
        from tools.sendgrid_email_tool import _download_file

        with resolves_to("93.184.216.34"), self.assertRaises(UnsafeURL):
            _download_file("https://attacker.example/steal")
        with self.assertRaises(UnsafeURL):
            _download_file("http://127.0.0.1:5432/")

    def test_fetch_text_refuses_internal_address(self):
        from tools.fetch_text_from_url_tool import fetch_text_from_url

        out = fetch_text_from_url.invoke({"url": "http://169.254.169.254/latest/meta-data/"})
        self.assertTrue(out.startswith("Cannot fetch this URL"), out)


class BucketSettingTests(unittest.TestCase):
    def test_no_fallback_bucket(self):
        from tools import chart_s3_tool, export_table_tool, file_upload_s3_tool, pdf_tool

        with mock.patch.dict(os.environ, {}, clear=True):
            for module in (pdf_tool, export_table_tool, chart_s3_tool, file_upload_s3_tool):
                with self.subTest(module=module.__name__), self.assertRaises(RuntimeError) as cm:
                    module._s3_env()
                self.assertIn("AWS_S3_BUCKET", str(cm.exception))

    def test_configured_bucket_is_used(self):
        from tools import pdf_tool

        with mock.patch.dict(os.environ, {"AWS_S3_BUCKET": "kai-reports"}, clear=True):
            self.assertEqual(pdf_tool._s3_env()[2], "kai-reports")

    def test_export_without_bucket_says_so(self):
        from tools.export_table_tool import export_table_to_file

        with mock.patch.dict(os.environ, {}, clear=True):
            out = export_table_to_file.invoke({"data": "a,b\n1,2", "file_format": "csv"})
        self.assertIn("not uploaded", out)
        self.assertIn("AWS_S3_BUCKET", out)


if __name__ == "__main__":
    unittest.main()
