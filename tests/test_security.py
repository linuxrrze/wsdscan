"""Security tests: a malicious device on the network, crafted images, file
writes and command-line input. Standard library only.

Run from the repository root:  python3 -m unittest discover -s tests -v
"""

import io
import os
import socket
import struct
import subprocess
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "wsdscan.py")
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ["WSDSCAN_CONFIG"] = os.path.join(tempfile.mkdtemp(prefix="wsdscan-sec-"), "config.ini")

import wsdscan as tool  # noqa: E402
from fake_wsd import JPEG_RGB, FakeScanner, envelope, make_tiff  # noqa: E402

ES580W = {"model": "ES-580W", "service_path": "/WDP/SCAN",
          "formats": ["exif", "tiff-single-uncompressed"]}


def args(**overrides):
    base = {"source": "adf", "mode": "color", "resolution": 300, "paper": "a4",
            "lossless": False, "brightness": None, "contrast": None, "ocr": False,
            "ocr_engine": "auto", "ocr_lang": None, "host": None, "model": None}
    base.update(overrides)
    return SimpleNamespace(**base)


def pages_in(path):
    with open(path, "rb") as f:
        return f.read().count(b"/Type /Page ")


class FakeUdp:
    """Stands in for the discovery socket: scripted answers from given sources."""

    def __init__(self, answers):
        self.answers = list(answers)  # [(bytes, source_ip)]

    def __call__(self, *_args):
        return self

    def setsockopt(self, *_a):
        pass

    def settimeout(self, *_a):
        pass

    def sendto(self, *_a):
        pass

    def recvfrom(self, _size):
        if not self.answers:
            raise socket.timeout()
        data, source = self.answers.pop(0)
        return data, (source, 3702)

    def close(self):
        pass


def probe_match(endpoint, xaddrs):
    return envelope(
        "<wsd:ProbeMatches><wsd:ProbeMatch><wsa:EndpointReference>"
        f"<wsa:Address>{endpoint}</wsa:Address></wsa:EndpointReference>"
        "<wsd:Types>wsdp:Device wscn:ScanDeviceType</wsd:Types>"
        f"<wsd:XAddrs>{xaddrs}</wsd:XAddrs></wsd:ProbeMatch></wsd:ProbeMatches>")


class DiscoveryPinningTest(unittest.TestCase):
    """M3: only trust device URLs that point to the host that answered."""

    def probe(self, answers, host=None):
        with mock.patch.object(tool.socket, "socket", FakeUdp(answers)):
            return tool.probe(host, timeout=0.5)

    def test_host_mode_ignores_other_responders(self):
        found = self.probe([(probe_match("urn:evil", "http://192.0.2.66/WSD"), "192.0.2.66"),
                            (probe_match("urn:real", "http://192.0.2.5/WSD"), "192.0.2.5")],
                           host="192.0.2.5")
        self.assertEqual(found, [("urn:real", ["http://192.0.2.5/WSD"])])

    def test_device_urls_must_point_to_the_responder(self):
        found = self.probe([
            (probe_match("urn:a", "http://192.0.2.99/WSD http://192.0.2.5/WSD"), "192.0.2.5"),
            (probe_match("urn:b", "http://127.0.0.1:631/x"), "192.0.2.6"),
        ])
        self.assertEqual(found, [("urn:a", ["http://192.0.2.5/WSD"])])

    def test_only_http_and_https(self):
        found = self.probe([(probe_match("urn:a", "file:///etc/passwd ftp://192.0.2.5/x "
                                                  "data:text/plain,x"), "192.0.2.5")])
        self.assertEqual(found, [])

    def test_first_answer_per_device_wins(self):
        found = self.probe([(probe_match("urn:a", "http://192.0.2.5/WSD"), "192.0.2.5"),
                            (probe_match("urn:a", "http://192.0.2.66/WSD"), "192.0.2.66")])
        self.assertEqual(found, [("urn:a", ["http://192.0.2.5/WSD"])])

    def test_dtd_in_discovery_answer_ignored(self):
        evil = probe_match("urn:a", "http://192.0.2.5/WSD").replace(
            b"<soap:Envelope", b'<!DOCTYPE x [<!ENTITY a "a">]><soap:Envelope', 1)
        self.assertEqual(self.probe([(evil, "192.0.2.5")]), [])

    def test_service_on_another_host_is_ignored(self):
        with FakeScanner(hosted_address="http://192.0.2.66:80/WSD/SCANNER") as fake:
            with self.assertRaisesRegex(tool.ScanError, "offers a scan service"):
                tool.find_scanner(fake.host)

    def test_announced_url_to_other_host_is_ignored(self):
        with FakeScanner(xaddrs="http://127.0.0.2:1/WSD/DEVICE") as fake:
            with self.assertRaisesRegex(tool.ScanError, "no WSD scanner answered"):
                tool.find_scanner(fake.host)

    def test_broken_responder_does_not_break_discovery(self):
        """M4: an unreachable or garbage device is skipped, the real one found."""
        with FakeScanner(**ES580W) as fake:
            devices = [("urn:broken", ["http://127.0.0.1:9/WSD/DEVICE"]),
                       ("urn:garbage", ["http://[::1/x"]),
                       (fake.endpoint, [fake.device_url])]
            with mock.patch.object(tool, "probe", return_value=devices):
                found = tool.discover(None)
        self.assertEqual([d["model"] for d in found], ["ES-580W"])


class UrlSafetyTest(unittest.TestCase):
    """M3: no file:/data: URLs, no redirects, no proxy."""

    def test_refuses_non_http_urls(self):
        for url in ("file:///etc/hostname", "data:text/plain,hello", "ftp://192.0.2.5/x",
                    "http:///nohost"):
            with self.subTest(url=url), self.assertRaisesRegex(tool.ScanError, "only http"):
                tool.soap_call(url, "urn:x", tool.ACTION_GET)

    def test_redirects_are_not_followed(self):
        with FakeScanner(redirect_to="http://192.0.2.66/elsewhere") as fake:
            with self.assertRaises(tool.SoapFault) as ctx:
                tool.soap_call(fake.service_url, fake.service_url,
                               tool.SCAN_ACTION + "GetScannerElements")
        self.assertEqual(ctx.exception.code, "HTTP302")

    def test_proxy_settings_are_ignored(self):
        with FakeScanner(**ES580W, sheets=1) as fake, tempfile.TemporaryDirectory() as d, \
                mock.patch.dict(os.environ, {"http_proxy": "http://127.0.0.1:9",
                                             "HTTP_PROXY": "http://127.0.0.1:9"}):
            tool.scan_to_file(args(host=fake.host), os.path.join(d, "a.pdf"))
            self.assertEqual(pages_in(os.path.join(d, "a.pdf")), 1)

    def test_xml_with_dtd_refused(self):
        with FakeScanner(doctype=True) as fake:
            with self.assertRaisesRegex(tool.ScanError, "offers a scan service"):
                tool.find_scanner(fake.host)  # the device's metadata is rejected
            with self.assertRaisesRegex(tool.ScanError, "DTD"):
                tool.soap_call(fake.device_url, fake.endpoint, tool.ACTION_GET)


class LimitsTest(unittest.TestCase):
    """M2: no endless or oversized responses, page streams or images."""

    def test_response_size_limit(self):
        with FakeScanner(image=b"\xff\xd8" + b"x" * 50000, sheets=1) as fake, \
                mock.patch.object(tool, "MAX_RESPONSE_BYTES", 20000), \
                tempfile.TemporaryDirectory() as d:
            with self.assertRaisesRegex(tool.ScanError, "larger than"):
                tool.scan_to_file(args(host=fake.host), os.path.join(d, "a.pdf"))

    def test_response_deadline(self):
        class SlowResponse:
            def read(self, _n):
                time.sleep(0.02)
                return b"x"
        with self.assertRaisesRegex(tool.ScanError, "took too long"):
            tool.read_limited(SlowResponse(), time.monotonic() + 0.1)

    def test_endless_page_stream_is_cut_off(self):
        with FakeScanner(endless=True) as fake, mock.patch.object(tool, "MAX_PAGES", 5), \
                tempfile.TemporaryDirectory() as d, mock.patch("sys.stderr", io.StringIO()):
            pages, complete, _err = tool.scan_to_file(args(host=fake.host), os.path.join(d, "a.pdf"))
            self.assertEqual((pages, complete), (5, False))
        self.assertIn("CancelJob", fake.actions())

    def test_only_the_requested_image_format(self):
        with FakeScanner(image=make_tiff("RGB24"), sheets=1) as fake, \
                tempfile.TemporaryDirectory() as d:
            with self.assertRaisesRegex(tool.ScanError, "no JPEG image"):
                tool.scan_to_file(args(host=fake.host), os.path.join(d, "a.pdf"))
        self.assertIn("CancelJob", fake.actions())

    def test_zero_resolution_from_device(self):
        """L3: used to crash with ZeroDivisionError after all pages were scanned."""
        with FakeScanner(final_dpi=0, sheets=1) as fake, tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "a.pdf")
            tool.scan_to_file(args(host=fake.host), out)
            with open(out, "rb") as f:
                self.assertIn(b"/MediaBox [0 0 5.76 3.84]", f.read(), "requested 300 dpi used")


def tiff_with_entries(entries, extra=b""):
    """Little-endian TIFF with the given (tag, type, count, 4-byte value) entries."""
    ifd = struct.pack("<H", len(entries)) + b"".join(
        struct.pack("<HHI", tag, typ, count) + value for tag, typ, count, value in entries)
    return b"II*\x00" + struct.pack("<I", 8) + ifd + b"\0\0\0\0" + extra


class CraftedImageTest(unittest.TestCase):
    """M1: crafted TIFF/JPEG headers must fail fast, without huge allocations."""

    def assert_fails_fast(self, data, func=None):
        start = time.monotonic()
        with self.assertRaises(tool.ScanError):
            (func or tool.tiff_image)(data)
        self.assertLess(time.monotonic() - start, 1.0)

    def test_huge_value_count(self):
        # BitsPerSample with 2**31 values: used to build a 2 GB format string.
        self.assert_fails_fast(tiff_with_entries([(258, 3, 0x7FFFFFFF, struct.pack("<I", 8))]))

    def test_repeated_strips_cannot_inflate(self):
        # 2000 strips all pointing at the same 1 KB: no more than the image needs is read.
        blob = b"\0" * 1024
        offsets = struct.pack("<2000I", *([8 + 2 + 12 * 11 + 4] * 2000))
        data = tiff_with_entries([
            (256, 4, 1, struct.pack("<I", 100)), (257, 4, 1, struct.pack("<I", 100)),
            (258, 3, 1, struct.pack("<HH", 8, 0)), (259, 3, 1, struct.pack("<HH", 1, 0)),
            (262, 3, 1, struct.pack("<HH", 1, 0)), (273, 4, 2000, b"OFFS"),
            (277, 3, 1, struct.pack("<HH", 1, 0)), (278, 4, 1, struct.pack("<I", 1)),
            (279, 4, 2000, b"CNTS"), (284, 3, 1, struct.pack("<HH", 1, 0)),
            (266, 3, 1, struct.pack("<HH", 1, 0))], blob)
        offs_at = len(data)
        data += offsets
        cnts_at = len(data)
        data += struct.pack("<2000I", *([1024] * 2000))
        data = data.replace(b"OFFS", struct.pack("<I", offs_at)).replace(
            b"CNTS", struct.pack("<I", cnts_at))
        img = tool.tiff_image(data)  # 100x100 gray needs 10 KB: collected from 10 strips
        self.assertEqual(len(img["pixels"]), 100 * 100)

    def test_strip_out_of_range(self):
        tiff = bytearray(make_tiff("Grayscale8", rows_per_strip=100))
        # StripOffsets (tag 273) pointing far outside the file
        ifd = int.from_bytes(tiff[4:8], "little")
        for i in range(int.from_bytes(tiff[ifd:ifd + 2], "little")):
            entry = ifd + 2 + 12 * i
            if int.from_bytes(tiff[entry:entry + 2], "little") == 273:
                tiff[entry + 8:entry + 12] = (10 ** 9).to_bytes(4, "little")
        self.assert_fails_fast(bytes(tiff))

    def test_implausible_sizes(self):
        for width, height in ((0, 10), (10, 0), (100000, 10), (65535, 65535)):
            with self.subTest(size=(width, height)):
                self.assert_fails_fast(make_tiff("RGB24").replace(
                    struct.pack("<HHI", 256, 4, 1) + struct.pack("<I", 24),
                    struct.pack("<HHI", 256, 4, 1) + struct.pack("<I", width)).replace(
                    struct.pack("<HHI", 257, 4, 1) + struct.pack("<I", 16),
                    struct.pack("<HHI", 257, 4, 1) + struct.pack("<I", height)))

    def test_jpeg_with_zero_size(self):
        bad = bytearray(JPEG_RGB)
        sof = bad.index(b"\xff\xc0")
        bad[sof + 5:sof + 9] = b"\0\0\0\0"
        self.assert_fails_fast(bytes(bad), tool.jpeg_info)

    def test_image_size_reads_headers_only(self):
        self.assertEqual(tool.image_size(JPEG_RGB), (24, 16))
        self.assertEqual(tool.image_size(make_tiff("BlackAndWhite1")), (24, 16))


class DeviceTextTest(unittest.TestCase):
    """L1: control characters from the device never reach the terminal."""

    def test_clean(self):
        self.assertEqual(tool.clean("ES\x1b[31m-580W"), "ES?[31m-580W")
        self.assertEqual(tool.clean("a\u009b2Jb"), "a?2Jb")       # C1 CSI
        self.assertEqual(tool.clean("evil‮txt.pdf"), "evil?txt.pdf")  # bidi override
        self.assertEqual(tool.clean("one\ntwo three"), "one?two?three")
        self.assertEqual(tool.clean("Brother ADS-1700W · Büro"), "Brother ADS-1700W · Büro")

    def test_model_name_with_controls_is_cleaned(self):
        with FakeScanner(model="ES-580W\u009b2J‮") as fake:
            device = tool.find_scanner(fake.host)
            r = subprocess.run([sys.executable, SCRIPT, "--host", fake.host, "--list"],
                               capture_output=True, text=True, timeout=60)
        self.assertEqual(device["model"], "ES-580W?2J?")
        self.assertNotIn("\u009b", r.stdout)
        self.assertNotIn("‮", r.stdout)

    def test_http_reason_with_escape_sequence(self):
        fault = None

        class Handler:
            pass
        with FakeScanner() as fake:
            original = fake._fault

            def evil_fault(h, subcode, code=400):
                h.send_response(500, "\x1b[2J\x1b[31mEVIL")
                h.send_header("Content-Length", "0")
                h.end_headers()
            fake._fault = evil_fault
            try:
                with self.assertRaises(tool.SoapFault) as ctx:
                    tool.soap_call(fake.service_url, "urn:wrong-to", tool.ACTION_GET)
                fault = ctx.exception
            finally:
                fake._fault = original
        self.assertNotIn("\x1b", str(fault))


class FileWriteTest(unittest.TestCase):
    """L2: output files are created exclusively, never through symlinks."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.d = self.dir.name

    def test_dangling_symlink_is_not_followed(self):
        victim = os.path.join(self.d, "victim")
        os.symlink(victim, os.path.join(self.d, "scan.pdf"))
        with self.assertRaisesRegex(tool.ScanError, "already exists"):
            tool.write_file(os.path.join(self.d, "scan.pdf"), b"%PDF")
        self.assertFalse(os.path.exists(victim))

    def test_overwrite_replaces_the_link_not_its_target(self):
        victim = os.path.join(self.d, "victim")
        with open(victim, "w") as f:
            f.write("keep")
        os.symlink(victim, os.path.join(self.d, "scan.pdf"))
        tool.write_file(os.path.join(self.d, "scan.pdf"), b"%PDF", overwrite=True)
        with open(victim) as f:
            self.assertEqual(f.read(), "keep")
        self.assertFalse(os.path.islink(os.path.join(self.d, "scan.pdf")))

    def test_existing_file_kept_and_no_leftovers(self):
        path = os.path.join(self.d, "scan.pdf")
        with open(path, "w") as f:
            f.write("old")
        with self.assertRaises(tool.ScanError):
            tool.write_file(path, b"%PDF new")
        with open(path) as f:
            self.assertEqual(f.read(), "old")
        self.assertEqual(os.listdir(self.d), ["scan.pdf"], "no temporary files left")

    def test_permissions_follow_umask(self):
        old = os.umask(0o027)
        try:
            tool.write_file(os.path.join(self.d, "a.pdf"), b"%PDF")
        finally:
            os.umask(old)
        self.assertEqual(os.stat(os.path.join(self.d, "a.pdf")).st_mode & 0o777, 0o640)

    def test_unwritable_folder_is_a_clean_error(self):
        with self.assertRaisesRegex(tool.ScanError, "cannot write"):
            tool.write_file(os.path.join(self.d, "missing", "a.pdf"), b"%PDF")

    def test_without_hard_links(self):
        with mock.patch.object(tool.os, "link", side_effect=PermissionError("no links")):
            tool.write_file(os.path.join(self.d, "a.pdf"), b"%PDF")
            with self.assertRaisesRegex(tool.ScanError, "already exists"):
                tool.write_file(os.path.join(self.d, "a.pdf"), b"%PDF")
        with open(os.path.join(self.d, "a.pdf"), "rb") as f:
            self.assertEqual(f.read(), b"%PDF")


class CommandLineTest(unittest.TestCase):
    """L3/L4/L11: bad input gives clean errors, never tracebacks."""

    def run_cli(self, *argv):
        return subprocess.run([sys.executable, SCRIPT, *argv], capture_output=True, text=True,
                              timeout=60)

    def test_invalid_values(self):
        for argv, message in ((["-r", "0", "--show-config"], "from 50 to 2400"),
                              (["-r", "-300", "--show-config"], "from 50 to 2400"),
                              (["-r", "²", "--show-config"], "from 50 to 2400"),
                              (["--host", "scanner:abc", "--info"], "invalid scanner address"),
                              (["--host", "a b", "--info"], "invalid scanner address"),
                              (["--host", "no-such-host.invalid", "--info"], "cannot resolve")):
            with self.subTest(argv=argv):
                r = self.run_cli(*argv)
                self.assertNotEqual(r.returncode, 0)
                self.assertIn(message, r.stderr)
                self.assertNotIn("Traceback", r.stderr)

    def test_invalid_ocr_languages(self):
        with mock.patch.object(tool, "ocr_engines", return_value=[("tesseract", "/x")]), \
                mock.patch.object(tool, "tesseract_languages", return_value=[]):
            for lang in ("--version", "deu;rm", "eng+-c"):
                with self.subTest(lang=lang), self.assertRaisesRegex(tool.ScanError, "invalid OCR"):
                    tool.resolve_ocr("auto", lang)


if __name__ == "__main__":
    unittest.main()
