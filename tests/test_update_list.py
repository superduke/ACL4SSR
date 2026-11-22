import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from scripts.update_list import convert_adobe_list, update_list, validate_proxy_list, validate_url


class Response(io.BytesIO):
    status = 200


class ListValidationTests(unittest.TestCase):
    def test_accepts_complete_source_urls(self):
        for url in ("https://example.com/ips.txt", "http://example.com:8080/list?format=text"):
            self.assertEqual(validate_url(url), url)

    def test_rejects_missing_or_invalid_source_url(self):
        for url in ("", "ProxyIP.JP.CMLiussss.net", "https://", "file:///tmp/ips", "ftp://host/list", "https://user:password@host/list", "https://host/list#fragment", "https://host:bad/list", "https://host/a b"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                validate_url(url)

    def test_proxy_formats(self):
        text = "# IP endpoints\n192.0.2.1:443#JP\n192.0.2.2\n[2001:db8::1]:8443#US\n2001:db8::2\n"
        self.assertEqual(validate_proxy_list(text), text)

    def test_proxy_rejects_empty_html_json_and_invalid_endpoints(self):
        for text in ("", " \n", "# only comments\n", "<html>Error</html>", '{"ip":"192.0.2.1"}', "proxy.example.com:443", "999.2.3.4:443", "192.0.2.1:0", "192.0.2.1:65536", "192.0.2.1:port", "192.0.2.1:\n", "[192.0.2.1]:443", "fe80::1%eth0", "192.0.2.1:443\ninvalid"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                validate_proxy_list(text)

    def test_adobe_conversion(self):
        text = "# source comment\n\n0.0.0.0 example.com\n0.0.0.0\ttelemetry.example.com\n"
        self.assertEqual(convert_adobe_list(text), "# source comment\n\nDOMAIN,example.com\nDOMAIN,telemetry.example.com\n")

    def test_adobe_rejects_empty_html_and_invalid_hosts(self):
        for text in ("", "# comments only\n", "<html>Success</html>", "0.0.0.0", "127.0.0.1 example.com", "0.0.0.0 -bad.example", "0.0.0.0 example..com", "0.0.0.0 example.com,REJECT", "0.0.0.0 example.com\ninvalid"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                convert_adobe_list(text)


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.output = Path(self.directory.name) / "list.txt"
        self.original = b"previous known-good list\n"
        self.output.write_bytes(self.original)

    def test_valid_changed_list_atomically_replaces_output(self):
        with patch("scripts.update_list.urlopen", return_value=Response(b"192.0.2.1:443#JP\n")) as download:
            self.assertTrue(update_list("baipiao", "https://example.com/ips", self.output))
        download.assert_called_once()
        self.assertEqual(download.call_args.args[0].full_url, "https://example.com/ips")
        self.assertEqual(download.call_args.args[0].get_header("User-agent"), "ACL4SSR-list-updater/1.0")
        self.assertEqual(download.call_args.kwargs, {"timeout": 60})
        self.assertEqual(self.output.read_bytes(), b"192.0.2.1:443#JP\n")
        self.assertEqual(list(self.output.parent.iterdir()), [self.output])

    def test_unchanged_output_is_not_replaced(self):
        self.output.write_bytes(b"DOMAIN,example.com\n")
        with patch("scripts.update_list.urlopen", return_value=Response(b"0.0.0.0 example.com\n")), patch("scripts.update_list.os.replace") as replace:
            self.assertFalse(update_list("adobe", "https://example.com/list", self.output))
        replace.assert_not_called()

    def test_valid_adobe_creates_missing_parent_and_output(self):
        output = self.output.parent / "Clash" / "adobe.list"
        with patch("scripts.update_list.urlopen", return_value=Response(b"0.0.0.0 example.com\n")):
            self.assertTrue(update_list("adobe", "https://example.com/list", output))
        self.assertEqual(output.read_text(), "DOMAIN,example.com\n")

    def test_bad_url_does_not_download_or_replace(self):
        with patch("scripts.update_list.urlopen") as download, self.assertRaises(ValueError):
            update_list("baipiao", "ProxyIP.JP.CMLiussss.net", self.output)
        download.assert_not_called()
        self.assertEqual(self.output.read_bytes(), self.original)

    def test_failed_download_preserves_output(self):
        for error in (HTTPError("https://example.com", 404, "Not Found", {}, None), URLError("unreachable"), TimeoutError("timeout")):
            with self.subTest(error=type(error).__name__), patch("scripts.update_list.urlopen", side_effect=error), self.assertRaises(Exception):
                update_list("baipiao", "https://example.com/ips", self.output)
            self.assertEqual(self.output.read_bytes(), self.original)

    def test_empty_bad_or_partial_content_preserves_outputs(self):
        for kind in ("baipiao", "adobe"):
            for content in (b"", b"# comments only\n", b"<html>200 OK</html>", b"\xff\xff", b"192.0.2.1:443\ninvalid", b"0.0.0.0 example.com\ninvalid"):
                with self.subTest(kind=kind, content=content), patch("scripts.update_list.urlopen", return_value=Response(content)), self.assertRaises((ValueError, UnicodeError)):
                    update_list(kind, "https://example.com/list", self.output)
                self.assertEqual(self.output.read_bytes(), self.original)
                self.assertEqual(list(self.output.parent.iterdir()), [self.output])

    def test_non_success_status_preserves_output(self):
        for status in (204, 206, 304):
            response = Response(b"192.0.2.1:443\n")
            response.status = status
            with self.subTest(status=status), patch("scripts.update_list.urlopen", return_value=response), self.assertRaises(ValueError):
                update_list("baipiao", "https://example.com/list", self.output)
            self.assertEqual(self.output.read_bytes(), self.original)

    def test_failed_replace_cleans_temporary_file(self):
        with patch("scripts.update_list.urlopen", return_value=Response(b"192.0.2.1:443\n")), patch("scripts.update_list.os.replace", side_effect=OSError("failed")), self.assertRaises(OSError):
            update_list("baipiao", "https://example.com/list", self.output)
        self.assertEqual(self.output.read_bytes(), self.original)
        self.assertEqual(list(self.output.parent.iterdir()), [self.output])


if __name__ == "__main__":
    unittest.main()
