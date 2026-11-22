"""Offline regression tests for the Cloudflare CIDR generator."""

from contextlib import redirect_stderr, redirect_stdout
from http.client import IncompleteRead
import importlib.util
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock
from urllib.error import HTTPError, URLError
import zipfile


SCRIPT = Path(__file__).resolve().parents[1] / "CloudflareCIDR-main.py"


def load_generator():
    spec = importlib.util.spec_from_file_location("cloudflare_cidr", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cidr = load_generator()


def source_text(asn, index):
    return f"# AS{asn} (EXAMPLE)\n# Example network\n#\n192.0.{index}.0/24\n"


def archive_bytes(root="as-ip-blocks-master", overrides=None, missing=(), extra=(), reverse=False):
    overrides = overrides or {}
    entries = [
        (f"{root}/as/{asn}/{cidr.SOURCE_FILENAME}", overrides.get(asn, source_text(asn, index)))
        for index, asn in enumerate(cidr.INCLUDED_ASNS)
        if asn not in missing
    ]
    if reverse:
        entries.reverse()
    entries.extend(extra)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_STORED) as archive:
        for name, text in entries:
            archive.writestr(name, text)
    return buffer.getvalue()


class CloudflareCIDRTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.output_dir = Path(self.directory.name)
        self.outputs = [self.output_dir / path for path in cidr.OUTPUT_PATHS]
        self.outputs[0].parent.mkdir()
        for index, path in enumerate(self.outputs):
            path.write_text(f"original output {index}\n", encoding="utf-8")
        self.original = [path.read_bytes() for path in self.outputs]

    def assert_outputs_preserved(self):
        self.assertEqual([path.read_bytes() for path in self.outputs], self.original)
        self.assertEqual(list(self.output_dir.glob(".cloudflare-cidr-*")), [])

    def run_with_download(self, data, status=200):
        response = mock.MagicMock()
        response.status = status
        response.read.return_value = data
        response.__enter__.return_value = response
        with mock.patch.object(cidr, "urlopen", return_value=response) as download:
            changed = cidr.generate(self.output_dir)
        download.assert_called_once()
        args, kwargs = download.call_args
        self.assertEqual(args[0].full_url, cidr.ARCHIVE_URL)
        self.assertEqual(args[0].get_header("User-agent"), "ACL4SSR-list-updater/1.0")
        self.assertEqual(kwargs, {"timeout": cidr.REQUEST_TIMEOUT})
        response.__exit__.assert_called_once()
        return changed

    def assert_invalid_preserves_outputs(self, data, message=None):
        with mock.patch.object(cidr, "download_archive", return_value=data):
            with self.assertRaises((ValueError, zipfile.BadZipFile)) as caught:
                cidr.generate(self.output_dir)
        if message:
            self.assertIn(message, str(caught.exception))
        self.assert_outputs_preserved()

    def test_import_has_no_network_or_output_side_effects(self):
        with mock.patch("urllib.request.urlopen") as download, mock.patch("os.replace") as replace:
            load_generator()
        download.assert_not_called()
        replace.assert_not_called()

    def test_success_preserves_comments_and_both_formats(self):
        self.assertTrue(self.run_with_download(archive_bytes()))
        expected_plain = "".join(f"192.0.{index}.0/24\n" for index in range(len(cidr.INCLUDED_ASNS)))
        expected_clash = "".join(
            source_text(asn, index).replace(f"192.0.{index}.0/24", f"IP-CIDR,192.0.{index}.0/24,no-resolve")
            for index, asn in enumerate(cidr.INCLUDED_ASNS)
        )
        self.assertEqual(self.outputs[0].read_text(), expected_clash)
        self.assertEqual(self.outputs[1].read_text(), expected_plain)
        self.assertEqual(list(self.output_dir.glob(".cloudflare-cidr-*")), [])

    def test_identical_data_does_not_replace_outputs(self):
        data = archive_bytes()
        self.assertTrue(self.run_with_download(data))
        stat_before = [(path.stat().st_ino, path.stat().st_mtime_ns) for path in self.outputs]
        with mock.patch.object(cidr.os, "replace") as replace:
            self.assertFalse(self.run_with_download(data))
        replace.assert_not_called()
        self.assertEqual([(path.stat().st_ino, path.stat().st_mtime_ns) for path in self.outputs], stat_before)

    def test_changed_source_updates_both_outputs(self):
        self.run_with_download(archive_bytes())
        overrides = {cidr.INCLUDED_ASNS[0]: source_text(cidr.INCLUDED_ASNS[0], 10)}
        self.assertTrue(self.run_with_download(archive_bytes(overrides=overrides)))
        self.assertIn("IP-CIDR,192.0.10.0/24,no-resolve", self.outputs[0].read_text())
        self.assertIn("192.0.10.0/24", self.outputs[1].read_text())
        self.assertNotIn("192.0.0.0/24", self.outputs[1].read_text())

    def test_renamed_archive_root_and_member_order_do_not_change_output(self):
        expected = cidr.render_outputs(archive_bytes())
        self.assertEqual(cidr.render_outputs(archive_bytes(root="future-repository-abcdef", reverse=True)), expected)
        self.assertEqual(cidr.render_outputs(archive_bytes(root="asn-ip-master")), expected)

    def test_expected_asn_selection_is_unchanged(self):
        self.assertEqual(cidr.INCLUDED_ASNS, (
            "209242", "13335", "149648", "132892", "139242", "202623", "203898", "394536"
        ))

    def test_unselected_members_are_ignored_and_never_extracted(self):
        extras = [
            ("../../outside.txt", "do not extract"),
            ("as-ip-blocks-master/as/12345/ipv4-aggregated.txt", "not a CIDR"),
        ]
        with mock.patch.object(zipfile.ZipFile, "extractall") as extract_all, \
                mock.patch.object(zipfile.ZipFile, "extract") as extract:
            self.run_with_download(archive_bytes(extra=extras))
        extract_all.assert_not_called()
        extract.assert_not_called()
        self.assertEqual(sorted(path.name for path in self.output_dir.iterdir()), ["Clash", "CloudflareCIDR.txt"])

    def test_failed_downloads_preserve_outputs(self):
        failures = (
            HTTPError(cidr.ARCHIVE_URL, 404, "Not found", {}, None),
            URLError("connection failed"),
            TimeoutError("read timed out"),
            IncompleteRead(b"partial archive", 100),
        )
        for failure in failures:
            with self.subTest(failure=type(failure).__name__):
                with mock.patch.object(cidr, "urlopen", side_effect=failure):
                    with self.assertRaises(type(failure)):
                        cidr.generate(self.output_dir)
                self.assert_outputs_preserved()

    def test_non_success_or_partial_http_status_preserves_outputs(self):
        for status in (204, 206, 304, 403, 500):
            with self.subTest(status=status):
                with self.assertRaisesRegex(ValueError, "Expected HTTP 200"):
                    self.run_with_download(archive_bytes(), status=status)
                self.assert_outputs_preserved()

    def test_empty_download_preserves_outputs(self):
        with self.assertRaisesRegex(ValueError, "archive is empty"):
            self.run_with_download(b"")
        self.assert_outputs_preserved()

    def test_invalid_or_truncated_zip_preserves_outputs(self):
        for data in (b"<html>upstream error</html>", archive_bytes()[:-30]):
            with self.subTest(data_length=len(data)):
                self.assert_invalid_preserves_outputs(data)

    def test_each_missing_source_preserves_outputs(self):
        for asn in cidr.INCLUDED_ASNS:
            with self.subTest(asn=asn):
                self.assert_invalid_preserves_outputs(archive_bytes(missing=(asn,)), f"AS{asn}")

    def test_empty_or_whitespace_source_preserves_outputs(self):
        for text in ("", "\n \t\n"):
            with self.subTest(text=text):
                self.assert_invalid_preserves_outputs(
                    archive_bytes(overrides={cidr.INCLUDED_ASNS[-1]: text}), "is empty"
                )

    def test_comment_only_sources_are_allowed_when_other_sources_have_cidrs(self):
        overrides = {asn: f"# AS{asn} (EXAMPLE)\n# No current IPv4 prefixes\n#\n" for asn in cidr.INCLUDED_ASNS[1:]}
        clash, plain = cidr.render_outputs(archive_bytes(overrides=overrides))
        self.assertEqual(plain, "192.0.0.0/24\n")
        for asn in cidr.INCLUDED_ASNS:
            self.assertIn(f"# AS{asn} ", clash)

    def test_no_cidrs_anywhere_preserves_outputs(self):
        overrides = {asn: f"# AS{asn} (EXAMPLE)\n#\n" for asn in cidr.INCLUDED_ASNS}
        self.assert_invalid_preserves_outputs(archive_bytes(overrides=overrides), "No IPv4 CIDRs")

    def test_corrupt_data_after_valid_cidrs_preserves_outputs(self):
        asn = cidr.INCLUDED_ASNS[-1]
        bad_lines = (
            "999.1.2.0/24", "192.0.2.0/33", "192.0.2.1/24", "192.0.2.1", "2001:db8::/32",
            "192.0.2.0/255.255.255.0", "garbage", "192.0.2.0/24 trailing text",
        )
        for bad_line in bad_lines:
            with self.subTest(line=bad_line):
                text = source_text(asn, 7) + bad_line + "\n"
                self.assert_invalid_preserves_outputs(archive_bytes(overrides={asn: text}), "Invalid IPv4 CIDR")

    def test_missing_or_mismatched_source_header_preserves_outputs(self):
        for text in ("192.0.2.0/24\n", "# AS12345 (WRONG)\n192.0.2.0/24\n", "# unrelated comment\n"):
            with self.subTest(text=text):
                self.assert_invalid_preserves_outputs(
                    archive_bytes(overrides={cidr.INCLUDED_ASNS[0]: text}), "source header"
                )

    def test_invalid_utf8_source_preserves_outputs(self):
        self.assert_invalid_preserves_outputs(archive_bytes(overrides={cidr.INCLUDED_ASNS[-1]: b"\xff\xfe"}))

    def test_corrupt_zip_member_crc_preserves_outputs(self):
        damaged = archive_bytes().replace(b"192.0.0.0/24", b"193.0.0.0/24", 1)
        self.assert_invalid_preserves_outputs(damaged, "CRC")

    def test_duplicate_asn_source_preserves_outputs(self):
        asn = cidr.INCLUDED_ASNS[0]
        extra = [(f"another-root/as/{asn}/{cidr.SOURCE_FILENAME}", source_text(asn, 0))]
        self.assert_invalid_preserves_outputs(archive_bytes(extra=extra), "Duplicate")

    def test_mixed_roots_preserve_outputs(self):
        asn = cidr.INCLUDED_ASNS[0]
        extra = [(f"another-root/as/{asn}/{cidr.SOURCE_FILENAME}", source_text(asn, 0))]
        self.assert_invalid_preserves_outputs(archive_bytes(missing=(asn,), extra=extra), "one archive root")

    def test_unsafe_selected_root_preserves_outputs(self):
        self.assert_invalid_preserves_outputs(archive_bytes(root=".."), "Invalid source path")

    def test_oversized_source_preserves_outputs(self):
        with mock.patch.object(cidr, "MAX_SOURCE_BYTES", 1):
            self.assert_invalid_preserves_outputs(archive_bytes(), "unexpectedly large")

    def test_second_staging_write_failure_preserves_both_outputs(self):
        actual_write = Path.write_bytes
        writes = 0

        def fail_second_write(path, data):
            nonlocal writes
            writes += 1
            if writes == 2:
                raise OSError("disk full while staging second file")
            return actual_write(path, data)

        with mock.patch.object(cidr, "download_archive", return_value=archive_bytes()), \
                mock.patch.object(Path, "write_bytes", autospec=True, side_effect=fail_second_write), \
                mock.patch.object(cidr.os, "replace") as replace:
            with self.assertRaisesRegex(OSError, "disk full"):
                cidr.generate(self.output_dir)
        replace.assert_not_called()
        self.assert_outputs_preserved()

    def test_both_complete_files_are_staged_before_first_replacement(self):
        actual_replace = os.replace
        expected = cidr.render_outputs(archive_bytes())
        replacements = 0

        def inspect_and_replace(source, destination):
            nonlocal replacements
            if replacements == 0:
                staged = sorted(Path(source).parent.iterdir())
                self.assertEqual([path.read_text() for path in staged], list(expected))
                self.assertEqual([path.read_bytes() for path in self.outputs], self.original)
            replacements += 1
            return actual_replace(source, destination)

        with mock.patch.object(cidr.os, "replace", side_effect=inspect_and_replace):
            self.assertTrue(self.run_with_download(archive_bytes()))
        self.assertEqual(replacements, 2)

    def test_main_returns_nonzero_with_useful_error(self):
        with mock.patch.object(cidr, "generate", side_effect=ValueError("Missing IPv4 sources for AS13335")), \
                redirect_stderr(io.StringIO()) as stderr:
            self.assertEqual(cidr.main(), 1)
        self.assertIn("Missing IPv4 sources for AS13335", stderr.getvalue())

    def test_main_reports_changed_and_unchanged_success(self):
        for changed, message in ((True, "updated"), (False, "unchanged")):
            with self.subTest(changed=changed):
                with mock.patch.object(cidr, "generate", return_value=changed), redirect_stdout(io.StringIO()) as stdout:
                    self.assertEqual(cidr.main(), 0)
                self.assertIn(message, stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
