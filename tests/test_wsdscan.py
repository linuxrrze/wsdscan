"""Regression tests for wsdscan.py. Standard library only.

Run from the repository root:  python3 -m unittest discover -s tests -v
"""

import io
import os
import re
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from types import SimpleNamespace
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Never read the user's real config; subprocesses inherit this too.
_CONFIG_DIR = tempfile.mkdtemp(prefix="wsdscan-test-")
os.environ["WSDSCAN_CONFIG"] = os.path.join(_CONFIG_DIR, "config.ini")
SCRIPT = os.path.join(ROOT, "wsdscan.py")
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import wsdscan as tool  # noqa: E402
from fake_ocr import make_ocr_bin, read_log  # noqa: E402
from fake_wsd import (JPEG_EXIF, JPEG_GRAY, JPEG_RGB, JPEG_SIZE,  # noqa: E402
                      FakeScanner, make_jpeg, make_tiff, pack_bw, test_page, tiff_pixels)
import zlib  # noqa: E402

# Profile of the real ES-580W, as observed on the device. Keep in sync with
# reality: every field here was a bug or a surprise at some point.
ES580W_PROFILE = {
    "model": "ES-580W",
    "service_path": "/WDP/SCAN",
    "formats": ["exif", "tiff-single-uncompressed"],
}


def caps(**overrides):
    base = {
        "state": "Idle", "reasons": [], "formats": ["jfif"], "has_adf": True,
        "has_platen": False, "duplex": True, "colors": ["Grayscale8", "RGB24"],
        "resolutions": [150, 300, 600], "back_colors": [], "back_resolutions": [],
        "min_size": None, "max_size": None,
    }
    base.update(overrides)
    return base


def args(**overrides):
    base = {"source": "duplex", "mode": "color", "resolution": 300, "paper": "a4",
            "lossless": False, "brightness": None, "contrast": None,
            "ocr": False, "ocr_engine": "auto", "ocr_lang": None, "skip_blank": False}
    base.update(overrides)
    return SimpleNamespace(**base)


def parse_pdf(data):
    """Minimal structural check of a PDF written by write_pdf.

    Verifies the xref table points at every object and returns a summary.
    """
    xref = int(re.search(rb"startxref\n(\d+)\n%%EOF\n$", data).group(1))
    assert data[xref:].startswith(b"xref\n"), "startxref does not point at xref"
    count = int(re.match(rb"xref\n0 (\d+)\n", data[xref:]).group(1))
    offsets = [int(m) for m in re.findall(rb"(\d{10}) 00000 n ", data[xref:])]
    assert len(offsets) == count - 1, "xref entry count mismatch"
    for num, off in enumerate(offsets, 1):
        assert data[off:].startswith(f"{num} 0 obj\n".encode()), f"bad offset for obj {num}"
    return {
        "pages": int(re.search(rb"/Type /Pages /Kids \[[^\]]*\] /Count (\d+)", data).group(1)),
        "mediaboxes": [tuple(float(v) for v in m.split())
                       for m in re.findall(rb"/MediaBox \[0 0 ([\d. ]+)\]", data)],
        "colorspaces": re.findall(rb"/ColorSpace /(\w+)", data),
        "streams": re.findall(rb"/Filter /DCTDecode /Length (\d+) >>\nstream\n", data),
    }


# --- Unit tests -------------------------------------------------------------

class JpegInfoTest(unittest.TestCase):
    def test_rgb(self):
        self.assertEqual(tool.jpeg_info(JPEG_RGB), (*JPEG_SIZE, 3))

    def test_gray(self):
        self.assertEqual(tool.jpeg_info(JPEG_GRAY), (*JPEG_SIZE, 1))

    def test_exif_header_is_skipped(self):
        # Regression: the ES-580W only delivers "exif" JPEGs (APP1 before SOF).
        self.assertIn(b"Exif", JPEG_EXIF[:64])
        self.assertEqual(tool.jpeg_info(JPEG_EXIF), (*JPEG_SIZE, 3))

    def test_garbage_dies(self):
        with self.assertRaises(tool.ScanError):
            tool.jpeg_info(b"\xff\xd8not a jpeg at all")


class TiffTest(unittest.TestCase):
    def decoded(self, color, **opts):
        return tool.tiff_image(make_tiff(color, **opts))

    def test_bw_white_is_zero(self):
        img = self.decoded("BlackAndWhite1", photometric_bw=0)
        self.assertEqual((img["width"], img["height"], img["bits"]), (*JPEG_SIZE, 1))
        self.assertEqual(img["colorspace"], "DeviceGray")
        self.assertTrue(img["invert"])
        self.assertEqual(img["pixels"], tiff_pixels("BlackAndWhite1"))

    def test_bw_black_is_zero(self):
        self.assertFalse(self.decoded("BlackAndWhite1", photometric_bw=1)["invert"])

    def test_gray_and_rgb(self):
        gray = self.decoded("Grayscale8")
        self.assertEqual((gray["bits"], gray["colorspace"], gray["invert"]), (8, "DeviceGray", False))
        self.assertEqual(gray["pixels"], tiff_pixels("Grayscale8"))
        rgb = self.decoded("RGB24")
        self.assertEqual(rgb["colorspace"], "DeviceRGB")
        self.assertEqual(len(rgb["pixels"]), JPEG_SIZE[0] * JPEG_SIZE[1] * 3)
        self.assertEqual(rgb["pixels"], tiff_pixels("RGB24"))

    def test_big_endian_multi_strip_and_fill_order(self):
        for opts in ({"order": ">"}, {"rows_per_strip": 1}, {"rows_per_strip": 100},
                     {"fill_order": 2}):
            with self.subTest(**opts):
                img = self.decoded("BlackAndWhite1", **opts)
                self.assertEqual(img["pixels"], tiff_pixels("BlackAndWhite1"))

    def test_rejects_compressed_and_truncated(self):
        data = bytearray(make_tiff("Grayscale8"))
        with self.assertRaises(tool.ScanError):
            tool.tiff_image(bytes(data[:200]))  # strips cut off
        # Compression tag (259) -> 5 (LZW)
        ifd = int.from_bytes(data[4:8], "little")
        for i in range(int.from_bytes(data[ifd:ifd + 2], "little")):
            entry = ifd + 2 + 12 * i
            if int.from_bytes(data[entry:entry + 2], "little") == 259:
                data[entry + 8:entry + 10] = (5).to_bytes(2, "little")
        with self.assertRaisesRegex(tool.ScanError, "compression 5"):
            tool.tiff_image(bytes(data))

    def test_image_kind(self):
        self.assertEqual(tool.image_kind(JPEG_RGB), "jpeg")
        self.assertEqual(tool.image_kind(make_tiff("RGB24")), "tiff")
        self.assertEqual(tool.image_kind(make_tiff("RGB24", order=">")), "tiff")
        self.assertIsNone(tool.image_kind(b"GIF89a"))


class WritePdfTest(unittest.TestCase):
    def test_tiff_pages_lossless(self):
        pages = [make_tiff("BlackAndWhite1"), make_tiff("Grayscale8"), make_tiff("RGB24"), JPEG_RGB]
        data = tool.pdf_bytes(pages, 72)
        pdf = parse_pdf(data)
        self.assertEqual(pdf["pages"], 4)
        self.assertEqual(pdf["mediaboxes"], [(24.0, 16.0)] * 4)
        self.assertEqual(pdf["colorspaces"], [b"DeviceGray", b"DeviceGray", b"DeviceRGB", b"DeviceRGB"])
        self.assertEqual(re.findall(rb"/BitsPerComponent (\d+)", data), [b"1", b"8", b"8", b"8"])
        self.assertEqual(data.count(b"/Decode [1 0]"), 1)  # only the WhiteIsZero b/w page
        # Lossless: the Flate streams decompress to exactly the scanned pixels.
        streams = re.findall(rb"/FlateDecode /Length (\d+) >>\nstream\n", data)
        self.assertEqual(len(streams), 3)
        pos = 0
        for color, length in zip(("BlackAndWhite1", "Grayscale8", "RGB24"), streams):
            pos = data.index(b"/FlateDecode /Length " + length, pos)
            start = data.index(b"stream\n", pos) + 7
            raw = zlib.decompress(data[start:start + int(length)])
            self.assertEqual(raw, tiff_pixels(color))
            pos = start

    def write(self, jpegs, dpi):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "out.pdf")
            tool.write_pdf(path, jpegs, dpi)
            with open(path, "rb") as f:
                return f.read()

    def test_structure_and_page_size(self):
        data = self.write([JPEG_RGB, JPEG_GRAY, JPEG_EXIF], 72)
        self.assertTrue(data.startswith(b"%PDF-1.4\n"))
        pdf = parse_pdf(data)
        self.assertEqual(pdf["pages"], 3)
        # At 72 dpi one pixel is one point.
        self.assertEqual(pdf["mediaboxes"], [(24.0, 16.0)] * 3)
        self.assertEqual(pdf["colorspaces"], [b"DeviceRGB", b"DeviceGray", b"DeviceRGB"])

    def test_jpegs_embedded_unchanged(self):
        data = self.write([JPEG_EXIF], 300)
        self.assertIn(JPEG_EXIF, data)
        self.assertEqual(parse_pdf(data)["streams"], [str(len(JPEG_EXIF)).encode()])

    def test_dpi_scales_page(self):
        pdf = parse_pdf(self.write([JPEG_RGB], 300))
        self.assertEqual(pdf["mediaboxes"], [(5.76, 3.84)])  # 24px, 16px at 300 dpi


class SoapParsingTest(unittest.TestCase):
    def test_nested_subcode_wins(self):
        xml = ('<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"><s:Body>'
               "<s:Fault><s:Code><s:Value>s:Sender</s:Value><s:Subcode>"
               "<s:Value>wscn:ClientErrorNoImagesAvailable</s:Value></s:Subcode></s:Code>"
               "<s:Reason><s:Text>empty</s:Text></s:Reason></s:Fault></s:Body></s:Envelope>")
        fault = tool.parse_fault(ET.fromstring(xml))
        self.assertEqual(fault.code, "ClientErrorNoImagesAvailable")

    def test_no_fault(self):
        xml = '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"><s:Body/></s:Envelope>'
        self.assertIsNone(tool.parse_fault(ET.fromstring(xml)))

    def test_split_multipart_binary_intact(self):
        payload = b"\xff\xd8\r\n--not-the-boundary\r\n\x00\xff\xd9"
        body = (b"--B1\r\nContent-Type: application/xop+xml\r\n\r\n<x/>\r\n"
                b"--B1\r\nContent-Type: application/binary\r\n\r\n" + payload + b"\r\n--B1--\r\n")
        xml, binary = tool.split_multipart('multipart/related; boundary="B1"; type="x"', body)
        self.assertEqual(xml, b"<x/>")
        self.assertEqual(binary, payload)


class CapabilityTest(unittest.TestCase):
    def test_min_size_not_counted_as_resolution(self):
        # Regression: ADFMinimumSize/Width (2000) used to show up as a resolution.
        with FakeScanner(min_size=(2000, 2000)) as fake:
            c = tool.get_capabilities(fake.service_url)
        self.assertEqual(c["resolutions"], [100, 150, 200, 300, 600])
        self.assertEqual(c["min_size"], (2000, 2000))
        self.assertEqual(c["max_size"], (8500, 14000))

    def test_es580w_profile(self):
        with FakeScanner(**ES580W_PROFILE) as fake:
            c = tool.get_capabilities(fake.service_url)
        self.assertEqual(c["formats"], ["exif", "tiff-single-uncompressed"])
        self.assertTrue(c["duplex"])
        self.assertEqual(tool.pick_format(c), "exif")


class DeviceSettingsTest(unittest.TestCase):
    def test_full_device_settings(self):
        with FakeScanner(description={"name": "Office scanner", "location": "Room 2"},
                         conditions=[("InputTrayEmpty", "ADF", "Informational")]) as fake:
            c = tool.get_capabilities(fake.service_url)
        ds = c["device_settings"]
        self.assertEqual(ds["content_types"], ["Auto", "Text", "Photo"])
        self.assertIs(ds["auto_size"], True)
        self.assertIs(ds["auto_exposure"], False)
        self.assertIs(ds["brightness"], True)
        self.assertIs(ds["contrast"], True)
        self.assertEqual(ds["quality"], (0, 100))
        self.assertEqual(ds["scaling"], ((1, 1000), (1, 1000)))
        self.assertEqual(ds["rotations"], ["0", "180"])
        # Unknown vendor elements are listed, not dropped.
        self.assertEqual(ds["other"], [("BlankPageSkip", "Level=1/2")])
        self.assertEqual(c["optical_resolution"], (600, 600))
        self.assertEqual(c["description"],
                         {"name": "Office scanner", "info": None, "location": "Room 2"})
        self.assertEqual(c["conditions"], ["InputTrayEmpty (ADF, Informational)"])

    def test_minimal_scanner(self):
        # Like the ES-580W profile: only formats reported, no optional settings.
        with FakeScanner(**ES580W_PROFILE, device_settings="", optical=None) as fake:
            c = tool.get_capabilities(fake.service_url)
        ds = c["device_settings"]
        self.assertEqual(ds["content_types"], [])
        self.assertIsNone(ds["auto_size"])
        self.assertIsNone(ds["quality"])
        self.assertIsNone(ds["scaling"])
        self.assertEqual(ds["other"], [])
        self.assertIsNone(c["optical_resolution"])
        self.assertEqual(c["conditions"], [])

    def test_no_device_settings_element(self):
        root = ET.fromstring(
            f'<x xmlns:wscn="{tool.NS["wscn"]}"><wscn:ADF/></x>')
        self.assertIsNone(tool.parse_capabilities(root)["device_settings"])

    def test_describe_element(self):
        el = ET.fromstring('<a xmlns="urn:v"><b>1</b><c><d>x</d><d>y</d></c></a>')
        self.assertEqual(tool.describe_element(el), "b=1, d=x/y")
        self.assertEqual(tool.describe_element(ET.fromstring("<a> v </a>")), "v")
        self.assertEqual(tool.describe_element(ET.fromstring("<a/>")), "(empty)")


class ExposureTicketTest(unittest.TestCase):
    def test_bw_and_lossless_use_tiff(self):
        c = caps(formats=["exif", "tiff-single-uncompressed"])
        t = tool.scan_ticket("duplex", "bw", 300, "a4", c)
        self.assertIn("<wscn:Format>tiff-single-uncompressed</wscn:Format>", t)
        self.assertIn("<wscn:ColorProcessing>BlackAndWhite1</wscn:ColorProcessing>", t)
        t = tool.scan_ticket("adf", "gray", 300, "a4", c, lossless=True)
        self.assertIn("<wscn:Format>tiff-single-uncompressed</wscn:Format>", t)
        t = tool.scan_ticket("adf", "gray", 300, "a4", c)
        self.assertIn("<wscn:Format>exif</wscn:Format>", t)

    def test_exposure_element(self):
        t = tool.scan_ticket("adf", "color", 300, "a4", caps(), brightness=-200, contrast=300)
        self.assertIn("<wscn:Exposure><wscn:ExposureSettings><wscn:Contrast>300</wscn:Contrast>"
                      "<wscn:Brightness>-200</wscn:Brightness></wscn:ExposureSettings>"
                      "</wscn:Exposure>", t)
        # WS-Scan order: InputSize, Exposure, MediaSides
        self.assertLess(t.index("InputSize"), t.index("Exposure"))
        self.assertLess(t.index("Exposure"), t.index("MediaSides"))
        only = tool.scan_ticket("adf", "color", 300, "a4", caps(), brightness=0)
        self.assertIn("<wscn:ExposureSettings><wscn:Brightness>0</wscn:Brightness>"
                      "</wscn:ExposureSettings>", only)
        self.assertNotIn("Exposure", tool.scan_ticket("adf", "color", 300, "a4", caps()))

    def test_validation_of_new_settings(self):
        tiff_caps = caps(formats=["exif", "tiff-single-uncompressed"],
                         colors=["RGB24", "Grayscale8", "BlackAndWhite1"])
        tool.validate(args(mode="bw"), tiff_caps)
        tool.validate(args(lossless=True), tiff_caps)
        tool.validate(args(brightness=500), dict(tiff_caps, device_settings={"brightness": True}))
        cases = [
            (args(mode="bw"), caps(colors=["RGB24"]), "BlackAndWhite1"),
            (args(mode="bw"), caps(colors=["BlackAndWhite1"], formats=["exif"]), "needs the tiff"),
            (args(lossless=True), caps(formats=["exif"]), "--lossless needs"),
            (args(contrast=10), dict(caps(), device_settings={"contrast": False}), "no contrast"),
        ]
        for a, c, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(tool.ScanError, message):
                tool.validate(a, c)

    def test_jpeg_missing_suggests_lossless(self):
        with self.assertRaisesRegex(tool.ScanError, "try --lossless"):
            tool.validate(args(), caps(formats=["tiff-single-uncompressed"]))

    def test_exposure_argument_range(self):
        self.assertEqual(tool.parse_args(["--brightness", "-1000", "--contrast", "1000"]).contrast, 1000)
        for bad in ("1001", "-1001", "abc"):
            with self.subTest(bad=bad), self.assertRaises(SystemExit), \
                    open(os.devnull, "w") as devnull:
                stderr, sys.stderr = sys.stderr, devnull
                try:
                    tool.parse_args(["--brightness", bad])
                finally:
                    sys.stderr = stderr


def device(manufacturer, model, ip):
    return {"manufacturer": manufacturer, "model": model, "firmware": "1",
            "serial": "?", "device_url": f"http://{ip}:80/WSD/DEVICE",
            "service": f"http://{ip}:80/WSD/SCAN"}


class DiscoveryTest(unittest.TestCase):
    """Scanner selection, independent of the network."""

    DEVICES = {
        "uuid:epson": device("EPSON", "ES-580W", "192.168.2.13"),
        "uuid:brother": device("Brother", "ADS-1700W", "192.168.2.20"),
        "uuid:printer": dict(device("HP", "LaserJet", "192.168.2.30"), service=None),
    }

    def find(self, host=None, model=None, devices=None):
        devices = list(devices or self.DEVICES)
        with mock.patch.object(tool, "probe", return_value=[(d, ["x"]) for d in devices]), \
                mock.patch.object(tool, "get_device", lambda e, x: self.DEVICES[e]):
            return tool.find_scanner(host, model)

    def test_single_scanner_needs_no_model(self):
        self.assertEqual(self.find(devices=["uuid:brother", "uuid:printer"])["model"], "ADS-1700W")

    def test_several_scanners_need_a_choice(self):
        with self.assertRaisesRegex(tool.ScanError, r"2 scanners found \(EPSON ES-580W at "
                                    r"192\.168\.2\.13; Brother ADS-1700W at 192\.168\.2\.20\); "
                                    r"pass --host <ip> or --model <name>"):
            self.find()

    def test_model_filter_case_insensitive_on_manufacturer_and_model(self):
        self.assertEqual(self.find(model="es-580")["model"], "ES-580W")
        self.assertEqual(self.find(model="brother")["model"], "ADS-1700W")

    def test_model_filter_without_match_lists_scanners(self):
        with self.assertRaisesRegex(tool.ScanError, r"no scanner matches --model 'canon'; "
                                    r"found: EPSON ES-580W at 192\.168\.2\.13"):
            self.find(model="canon")

    def test_devices_without_scan_service_are_ignored(self):
        with self.assertRaisesRegex(tool.ScanError, "offers a scan service"):
            self.find(devices=["uuid:printer"])

    def test_host_with_model_check(self):
        self.assertEqual(self.find(host="192.168.2.13", model="580",
                                   devices=["uuid:epson"])["model"], "ES-580W")
        with self.assertRaisesRegex(tool.ScanError, "no scanner matches"):
            self.find(host="192.168.2.13", model="ADS", devices=["uuid:epson"])



# A4 at 50 dpi: 8 pixels = 4 mm. Side margins (9 %) are 37 pixels wide.
PAGE_SIZE = (413, 585)
TEXT_LINE = (100, 300, 200, 10, 20)


def page(**kwargs):
    return test_page(*PAGE_SIZE, **kwargs)


def page_formats(rows):
    """The page as the scanner may send it: JPEG gray/color, TIFF gray/RGB/bw."""
    width, height = len(rows[0]), len(rows)
    gray = b"".join(rows)
    return {
        "jpeg gray": make_jpeg(rows),
        "jpeg color": make_jpeg(rows, color=True, tint=(120, 136)),
        "jpeg restart": make_jpeg(rows, color=True, restart=5),
        "tiff gray": make_tiff("Grayscale8", width, height, pixels=gray, rows_per_strip=64),
        "tiff rgb": make_tiff("RGB24", width, height, rows_per_strip=64,
                              pixels=bytes(v for v in gray for _ in range(3))),
        "tiff bw": make_tiff("BlackAndWhite1", width, height, rows_per_strip=64,
                             pixels=pack_bw(rows)),
    }


class BlankPageTest(unittest.TestCase):
    def test_block_grid_of_real_jpegs(self):
        # libjpeg-made fixtures; block means as decoded by Pillow
        for data, expected in ((JPEG_RGB, 38), (JPEG_EXIF, 38), (JPEG_GRAY, 128)):
            columns, rows, values = tool.jpeg_block_grid(data)
            self.assertEqual((columns, rows), (3, 2))
            for value in values:
                self.assertAlmostEqual(value, expected, delta=1.5)

    def test_block_grid_matches_page(self):
        rows = page(marks=[TEXT_LINE, (50, 40, 3, 40, 0), (296, 400, 37, 23, 90)])
        width, height = PAGE_SIZE
        expected = []
        for by in range(0, height, 8):
            for bx in range(0, width, 8):
                block = [rows[y][x] for y in range(by, min(by + 8, height))
                         for x in range(bx, min(bx + 8, width))]
                expected.append(sum(block) / len(block))
        for name, data in page_formats(rows).items():
            if name == "tiff bw":
                continue  # thresholded: other values
            with self.subTest(name):
                grid = (tool.jpeg_block_grid(data) if name.startswith("jpeg")
                        else tool.tiff_block_grid(data))
                self.assertEqual(grid[:2], (52, 74))
                # JPEG edge blocks repeat the last pixels; compare the inside
                for y in range(73):
                    for x in range(51):
                        self.assertAlmostEqual(grid[2][y * 52 + x], expected[y * 52 + x],
                                               delta=1.5)

    def test_detects_blank_pages(self):
        width, height = PAGE_SIZE
        cases = {
            "empty": (page(), True),
            "show-through": (page(marks=[(80, 100 + 20 * i, 250, 8, 221) for i in range(20)]),
                             True),
            "speck of dust": (page(marks=[(200, 200, 3, 3, 30)]), True),
            "filing holes": (page(marks=[(10, 200, 22, 22, 60), (10, 360, 22, 22, 60)]), True),
            "tinted paper": (page(paper=200), True),
            "one line": (page(marks=[TEXT_LINE]), False),
            "three small marks": (page(marks=[(120, 100, 5, 5, 0), (240, 300, 5, 5, 0),
                                              (300, 500, 5, 5, 0)]), False),
            "at the edge of the margin": (page(marks=[(40, 280, 4, 40, 0)]), False),
            "dark page": (page(paper=60), False),
        }
        for name, (rows, blank) in cases.items():
            for kind, data in page_formats(rows).items():
                if kind == "tiff bw" and name in ("dark page",):
                    continue  # thresholded to black: no "paper" left
                with self.subTest(name, format=kind):
                    self.assertIs(tool.page_is_blank(data), blank)

    def test_unreadable_pages_are_not_blank(self):
        blank = make_jpeg(page())
        sof = blank.index(b"\xff\xc0")
        progressive = blank[:sof + 1] + b"\xc2" + blank[sof + 2:]
        for name, data in (("progressive", progressive), ("truncated", blank[:len(blank) // 2]),
                           ("garbage", blank[:sof] + bytes(range(256)) * 4),
                           ("broken TIFF", make_tiff("Grayscale8")[:20]),
                           ("detailed photo", JPEG_RGB)):  # many bits per pixel
            with self.subTest(name):
                self.assertFalse(tool.page_is_blank(data))
        self.assertIsNone(tool.jpeg_block_grid(progressive))

    def test_scan_removes_blank_backs(self):
        text, blank = make_jpeg(page(marks=[TEXT_LINE])), make_jpeg(page())
        events, flags = [], []
        with tempfile.TemporaryDirectory() as d, FakeScanner(sheets=3, images=[text, blank]) as fake:
            out = os.path.join(d, "out.pdf")
            pages, complete, _err = tool.scan_to_file(
                args(host=fake.host, model=None, skip_blank=True), out,
                on_progress=lambda event, n: events.append((event, n)),
                on_page_image=lambda n, data, empty: flags.append(empty))
            with open(out, "rb") as f:
                self.assertEqual(parse_pdf(f.read())["pages"], 3)
        self.assertEqual((pages, complete), (3, True))
        self.assertEqual(flags, [False, True] * 3)
        self.assertEqual([e for e in events if e[0] == "blank"], [("blank", 2), ("blank", 4),
                                                                   ("blank", 6)])
        self.assertEqual(events[-1], ("saving", 3))

    def test_all_blank_pages_are_kept(self):
        events = []
        with tempfile.TemporaryDirectory() as d, \
                FakeScanner(sheets=1, images=[make_jpeg(page())]) as fake:
            pages, _complete, _err = tool.scan_to_file(
                args(host=fake.host, model=None, skip_blank=True), os.path.join(d, "out.pdf"),
                on_progress=lambda event, n: events.append((event, n)))
        self.assertEqual(pages, 2)
        self.assertIn(("all_blank", 2), events)

    def test_review_decides_and_off_by_default(self):
        text, blank = make_jpeg(page(marks=[TEXT_LINE])), make_jpeg(page())
        offered = []

        def select(images, empty):
            offered.append(empty)
            return list(range(len(images)))  # keep the blank page after all

        with tempfile.TemporaryDirectory() as d, FakeScanner(sheets=1, images=[text, blank]) as fake:
            pages, _c, _e = tool.scan_to_file(
                args(host=fake.host, model=None, skip_blank=True), os.path.join(d, "a.pdf"),
                select_pages=select)
            self.assertEqual(pages, 2)
            with mock.patch.object(tool, "page_is_blank") as detect:
                pages, _c, _e = tool.scan_to_file(args(host=fake.host, model=None),
                                                  os.path.join(d, "b.pdf"), select_pages=select)
            detect.assert_not_called()
        self.assertEqual(offered, [[False, True], [False, False]])


class ConfigTestBase(unittest.TestCase):
    """Isolated config file per test."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = os.path.join(self.dir.name, "sub", "config.ini")
        patcher = mock.patch.dict(os.environ, {"WSDSCAN_CONFIG": self.path})
        patcher.start()
        self.addCleanup(patcher.stop)
        for var in ("WSDSCAN_HOST", "WSDSCAN_MODEL", "WSDSCAN_SCANNER"):
            os.environ.pop(var, None)

    def write(self, text):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w") as f:
            f.write(text)


class ConfigTest(ConfigTestBase):
    def test_missing_file_gives_builtin_defaults(self):
        self.assertEqual(tool.load_config(), tool.CONFIG_DEFAULTS)
        a = tool.parse_args([])
        self.assertEqual((a.source, a.mode, a.resolution, a.paper, a.lossless, a.brightness,
                          a.host, a.outdir), ("duplex", "color", 300, "a4", False, None, None, "."))

    def test_config_values_become_cli_defaults(self):
        self.write("[scan]\nhost = 192.168.2.13\nmodel = epson\nmode = bw\nresolution = 100\n"
                   "lossless = yes\nbrightness = -200\ncontrast = default\noutdir = /tmp/scans\n"
                   "filename = Brief_{date}\n[gui]\nopen_after_scan = true\n")
        a = tool.parse_args([])
        self.assertEqual((a.host, a.model, a.mode, a.resolution, a.lossless, a.brightness,
                          a.contrast, a.outdir, a.filename),
                         ("192.168.2.13", "epson", "bw", 100, True, -200, None, "/tmp/scans",
                          "Brief_{date}"))

    def test_outdir_tilde_is_expanded(self):
        self.write("[scan]\noutdir = ~/Documents/Scans\n")
        self.assertEqual(tool.parse_args([]).outdir,
                         os.path.join(os.path.expanduser("~"), "Documents", "Scans"))

    def test_skip_blank_setting(self):
        self.assertFalse(tool.parse_args([]).skip_blank)
        self.write("[scan]\nskip_blank = yes\n")
        self.assertTrue(tool.load_config()["skip_blank"])
        self.assertTrue(tool.parse_args([]).skip_blank)
        self.assertFalse(tool.parse_args(["--no-skip-blank"]).skip_blank)
        self.write("[scan]\nskip_blank = maybe\n")
        with self.assertRaisesRegex(tool.ScanError, "skip_blank: expected true or false"):
            tool.load_config()

    def test_command_line_and_environment_override_config(self):
        self.write("[scan]\nhost = 10.0.0.1\nmode = bw\nlossless = true\nbrightness = 300\n")
        with mock.patch.dict(os.environ, {"WSDSCAN_HOST": "10.0.0.2"}):
            a = tool.parse_args(["-m", "gray", "--no-lossless", "--brightness", "default"])
        self.assertEqual((a.host, a.mode, a.lossless, a.brightness), ("10.0.0.2", "gray", False, None))
        self.assertEqual(tool.parse_args(["--host", "10.0.0.3"]).host, "10.0.0.3")

    def test_invalid_config_names_file_and_key(self):
        for text, message in (("[scan]\nmode = sepia\n", r"\[scan\] mode: expected one of"),
                              ("[scan]\nresolution = high\n", r"resolution: expected a resolution"),
                              ("[scan]\nbrightness = 5000\n", r"brightness: must be between"),
                              ("[scan]\ncolour = red\n", r"unknown setting 'colour'"),
                              ("not an ini file", r"config\.ini")):
            with self.subTest(text=text):
                self.write(text)
                with self.assertRaisesRegex(tool.ScanError, message):
                    tool.load_config()

    def test_save_keeps_other_sections_and_round_trips(self):
        self.write("[gui]\nopen_after_scan = true\n")
        values = dict(tool.CONFIG_DEFAULTS, host="192.168.2.13", lossless=True, brightness=-50,
                      filename="Scan {date}.pdf")
        tool.save_config(values, sections={"gui": {"notify": False}})
        self.assertEqual(tool.load_config(), values)
        self.assertEqual(tool.load_config_section("gui"), {"open_after_scan": "true", "notify": "false"})
        with open(self.path) as f:
            text = f.read()
        self.assertTrue(text.startswith("# wsdscan settings"))
        self.assertIn("brightness = -50", text)
        self.assertIn("contrast = default", text)

    def test_save_rejects_invalid_values(self):
        with self.assertRaises(ValueError):
            tool.save_config({"mode": "sepia"})
        with self.assertRaises(ValueError):
            tool.save_config({"colour": "red"})
        self.assertFalse(os.path.exists(self.path))

    def test_show_config(self):
        self.write("[scan]\nmode = gray\n")
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            tool.main(["--show-config"])
        self.assertIn(f"config file: {self.path} (exists)", out.getvalue())
        self.assertRegex(out.getvalue(), r"mode\s+gray")
        self.assertRegex(out.getvalue(), r"brightness\s+\(none\)")


class ScannerProfileTest(ConfigTestBase):
    PROFILES = ("[scan]\nmode = gray\nscanner = Office\n"
                "[scanner Office]\nhost = 192.168.2.13\n"
                "[scanner Home Office]\nmodel = ADS-1700W\nmode = bw\noutdir = /tmp/home\n")

    def test_default_profile_applies(self):
        self.write(self.PROFILES)
        cfg = tool.load_config()
        self.assertEqual((cfg["scanner"], cfg["host"], cfg["mode"]), ("Office", "192.168.2.13", "gray"))
        self.assertEqual(list(tool.load_scanners()), ["Office", "Home Office"])

    def test_profile_overrides_shared_defaults(self):
        self.write(self.PROFILES)
        cfg = tool.load_config(scanner="Home Office")
        self.assertEqual((cfg["host"], cfg["model"], cfg["mode"], cfg["outdir"]),
                         ("", "ADS-1700W", "bw", "/tmp/home"))
        self.assertEqual(tool.load_config(apply_profile=False)["mode"], "gray")

    def test_cli_selects_profile(self):
        self.write(self.PROFILES)
        a = tool.parse_args(["--scanner", "Home Office"])
        self.assertEqual((a.scanner, a.host, a.model, a.mode), ("Home Office", None, "ADS-1700W", "bw"))
        a = tool.parse_args(["--scanner", "Home Office", "-m", "color", "--host", "10.0.0.9"])
        self.assertEqual((a.mode, a.host), ("color", "10.0.0.9"), "command line wins")
        with mock.patch.dict(os.environ, {"WSDSCAN_SCANNER": "Home Office"}):
            self.assertEqual(tool.parse_args([]).model, "ADS-1700W")
        self.assertEqual(tool.parse_args([]).host, "192.168.2.13")

    def test_first_profile_without_default(self):
        self.write("[scanner A]\nhost = 10.0.0.1\n[scanner B]\nhost = 10.0.0.2\n")
        self.assertEqual(tool.load_config()["host"], "10.0.0.1")
        # A host in [scan] means "no profile by default".
        self.write("[scan]\nhost = 10.0.0.9\n[scanner A]\nhost = 10.0.0.1\n")
        cfg = tool.load_config()
        self.assertEqual((cfg["scanner"], cfg["host"]), ("", "10.0.0.9"))

    def test_unknown_profile(self):
        self.write(self.PROFILES)
        with self.assertRaisesRegex(tool.ScanError, r"unknown scanner 'Lab'; configured: Office, Home Office"):
            tool.load_config(scanner="Lab")
        self.write("[scan]\nscanner = Gone\n")
        with self.assertRaisesRegex(tool.ScanError, "unknown scanner 'Gone'; configured: none"):
            tool.parse_args([])

    def test_invalid_profile_settings(self):
        self.write("[scanner X]\nmode = sepia\n")
        with self.assertRaisesRegex(tool.ScanError, r"\[scanner X\] mode: expected one of"):
            tool.load_scanners()
        self.write("[scanner X]\nscanner = Y\n")
        with self.assertRaisesRegex(tool.ScanError, "unknown setting 'scanner' in \\[scanner X\\]"):
            tool.load_scanners()

    def test_save_scanners_replaces_profiles(self):
        self.write(self.PROFILES + "[gui]\ntray = true\n")
        tool.save_config({"scanner": "Lab"}, scanners={
            "Lab": {"host": "10.1.1.1"}, "Office": {"host": "192.168.2.13", "lossless": True}})
        self.assertEqual(tool.load_scanners(), {"Lab": {"host": "10.1.1.1"},
                                                "Office": {"host": "192.168.2.13", "lossless": True}})
        self.assertEqual(tool.load_config()["host"], "10.1.1.1")
        self.assertEqual(tool.load_config_section("gui"), {"tray": "true"})
        self.assertEqual(tool.load_config(apply_profile=False)["mode"], "gray", "[scan] kept")
        for bad in ({"": {}}, {" x": {}}, {"a]b": {}}, {"A": {"scanner": "B"}}, {"A": {"mode": "x"}}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                tool.save_config({}, scanners=bad)

    def test_show_config_lists_profiles(self):
        self.write(self.PROFILES)
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            tool.main(["--show-config"])
        text = out.getvalue()
        self.assertIn("configured scanners:\n  Office (default): 192.168.2.13\n", text)
        self.assertIn("  Home Office: automatic; model=ADS-1700W, mode=bw, outdir=/tmp/home", text)


class FilenameTest(unittest.TestCase):
    def test_render_filename(self):
        now = __import__("datetime").datetime(2026, 10, 3, 7, 5, 9)
        self.assertEqual(tool.render_filename("scan_{date}_{time}.pdf", now),
                         "scan_2026-10-03_07-05-09.pdf")
        self.assertEqual(tool.render_filename("Brief {date}", now), "Brief 2026-10-03.pdf")
        self.assertEqual(tool.render_filename("a/b.PDF", now), "a_b.PDF")

    def test_unique_path(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "Scan.pdf")
            self.assertEqual(tool.unique_path(path), path)
            open(path, "w").close()
            self.assertEqual(tool.unique_path(path), os.path.join(d, "Scan (2).pdf"))
            open(os.path.join(d, "Scan (2).pdf"), "w").close()
            self.assertEqual(tool.unique_path(path), os.path.join(d, "Scan (3).pdf"))


class ScanToFileTest(unittest.TestCase):
    def test_progress_events(self):
        events = []
        with tempfile.TemporaryDirectory() as d, FakeScanner(**ES580W_PROFILE, sheets=2) as fake:
            out = os.path.join(d, "out.pdf")
            pages, complete, ocr_error = tool.scan_to_file(
                args(host=fake.host, model=None), out,
                on_progress=lambda event, n: events.append((event, n)))
            with open(out, "rb") as f:
                self.assertEqual(parse_pdf(f.read())["pages"], 4)
        self.assertEqual((pages, complete, ocr_error), (4, True, None))
        self.assertEqual(events, [("connecting", 0), ("scanning", 0), ("page", 1), ("page", 2),
                                  ("page", 3), ("page", 4), ("saving", 4)])

    def test_page_images_and_selection(self):
        images, offered = [], []
        with tempfile.TemporaryDirectory() as d, FakeScanner(**ES580W_PROFILE, sheets=2) as fake:
            out = os.path.join(d, "out.pdf")

            def select(pages, blank):
                offered.append(len(pages))
                return [0, 3]  # remove pages 2 and 3

            pages, _complete, _err = tool.scan_to_file(
                args(host=fake.host, model=None), out,
                on_page_image=lambda n, data, blank: images.append((n, data[:2])),
                select_pages=select)
            with open(out, "rb") as f:
                self.assertEqual(parse_pdf(f.read())["pages"], 2)
        self.assertEqual(pages, 2)
        self.assertEqual(offered, [4])
        self.assertEqual(images, [(n, b"\xff\xd8") for n in (1, 2, 3, 4)])

    def test_output_path_decided_when_saving(self):
        order = []
        with tempfile.TemporaryDirectory() as d, FakeScanner(sheets=1) as fake:
            def path():
                order.append("path")
                return os.path.join(d, "late.pdf")
            tool.scan_to_file(args(host=fake.host, model=None, source="adf"), path,
                              select_pages=lambda pages, blank: order.append("select") or [0],
                              on_progress=lambda event, n: order.append(event))
            self.assertTrue(os.path.exists(os.path.join(d, "late.pdf")))
        self.assertEqual(order[-3:], ["select", "path", "saving"])

    def test_selection_can_discard(self):
        with tempfile.TemporaryDirectory() as d, FakeScanner(sheets=1) as fake:
            out = os.path.join(d, "out.pdf")
            for keep, message in ((None, "scan discarded"), ([], "all pages were removed")):
                with self.subTest(keep=keep), self.assertRaisesRegex(tool.ScanCancelled, message):
                    tool.scan_to_file(args(host=fake.host, model=None), out,
                                      select_pages=lambda pages, blank, k=keep: k)
            self.assertFalse(os.path.exists(out))

    def test_cancel_stops_and_cancels_job(self):
        with tempfile.TemporaryDirectory() as d, FakeScanner(sheets=5) as fake:
            out = os.path.join(d, "out.pdf")
            seen = []
            with self.assertRaises(tool.ScanCancelled):
                tool.scan_to_file(args(host=fake.host, model=None), out,
                                  on_progress=lambda event, n: seen.append(n),
                                  should_stop=lambda: len(seen) >= 3)  # after page 1
            self.assertFalse(os.path.exists(out))
        self.assertIn("CancelJob", fake.actions())
        self.assertEqual(fake.actions().count("RetrieveImage"), 1)

    def test_errors_are_scan_errors(self):
        with tempfile.TemporaryDirectory() as d, FakeScanner(sheets=0) as fake:
            with self.assertRaisesRegex(tool.ScanError, "no pages scanned"):
                tool.scan_to_file(args(host=fake.host, model=None), os.path.join(d, "x.pdf"))


class OcrTest(unittest.TestCase):
    """OCR with fake ocrmypdf/tesseract executables as the only PATH entry."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.log = os.path.join(self.dir.name, "ocr.log")
        self.env = mock.patch.dict(os.environ, {"FAKE_OCR_LOG": self.log, "LANG": "de_DE.UTF-8"})
        self.env.start()
        self.addCleanup(self.env.stop)
        os.environ.pop("FAKE_OCR_FAIL", None)
        for var in ("LC_ALL", "LC_MESSAGES"):
            os.environ.pop(var, None)

    def use_engines(self, *engines):
        directory = make_ocr_bin(os.path.join(self.dir.name, "bin-" + "-".join(engines)), engines)
        os.environ["PATH"] = directory if engines else os.path.join(self.dir.name, "empty")
        return directory

    def scan(self, profile=None, **overrides):
        out = os.path.join(self.dir.name, "out.pdf")
        with FakeScanner(**(profile or ES580W_PROFILE), sheets=1) as fake:
            result = tool.scan_to_file(args(host=fake.host, model=None, ocr=True, **overrides), out)
        with open(out, "rb") as f:
            return result, f.read(), fake

    def test_engine_detection(self):
        self.use_engines("ocrmypdf")
        self.assertEqual([n for n, _p in tool.ocr_engines()], ["ocrmypdf", "tesseract"])
        self.use_engines("tesseract")
        self.assertEqual([n for n, _p in tool.ocr_engines()], ["tesseract"])
        self.use_engines()
        self.assertEqual(tool.ocr_engines(), [])

    def test_languages(self):
        self.use_engines("tesseract")
        self.assertEqual(tool.tesseract_languages(), ["deu", "eng"])
        self.assertEqual(tool.tool_version(tool.shutil.which("tesseract")), "5.3.4")
        self.assertEqual(tool.default_ocr_languages(), "deu+eng")  # LANG=de_DE
        many = ["ara", "deu", "eng", "fra", "ita", "spa"]
        cases = [
            # Regression: an English desktop ignored an installed German language pack.
            ("en_US.UTF-8", ["deu", "eng"], "eng+deu"),
            ("de_DE.UTF-8", ["deu", "eng", "fra"], "deu+eng+fra"),
            ("fr_FR.UTF-8", ["deu", "eng"], "eng+deu"),
            ("en_US", ["eng"], "eng"), ("de_AT", ["deu"], "deu"),
            ("C", [], "eng"), ("ja_JP", [], "jpn+eng"),
            # Many installed: system language + English only.
            ("de_DE", many, "deu+eng"), ("en_GB", many, "eng"), ("pt_BR", many, "eng"),
        ]
        for locale_name, installed, expected in cases:
            with self.subTest(locale=locale_name):
                self.assertEqual(tool.default_ocr_languages(installed, locale_name), expected)

    def test_resolve(self):
        self.use_engines("ocrmypdf")
        self.assertEqual(tool.resolve_ocr("auto", None)[::2], ("ocrmypdf", "deu+eng"))
        self.assertEqual(tool.resolve_ocr("tesseract", "eng")[::2], ("tesseract", "eng"))
        with self.assertRaisesRegex(tool.ScanError, r"not installed: fra; installed: deu, eng "
                                                    r"\(e\.g\. 'sudo apt install tesseract-ocr-fra'\)"):
            tool.resolve_ocr("auto", "deu+fra")
        self.use_engines("tesseract")
        self.assertEqual(tool.resolve_ocr("auto", None)[0], "tesseract")
        with self.assertRaisesRegex(tool.ScanError, "'ocrmypdf' is not installed; available: tesseract"):
            tool.resolve_ocr("ocrmypdf", None)
        self.use_engines()
        with self.assertRaisesRegex(tool.ScanError, "needs OCRmyPDF or Tesseract"):
            tool.resolve_ocr("auto", None)

    def test_ocrmypdf_keeps_images(self):
        self.use_engines("ocrmypdf")
        (pages, complete, error), data, _fake = self.scan()
        self.assertEqual((pages, error), (2, None))
        self.assertTrue(data.endswith(b"%fake ocrmypdf text layer\n"))
        self.assertEqual(parse_pdf(data[:data.rindex(b"%fake")])["pages"], 2, "original PDF inside")
        call = read_log(self.log)[0]
        self.assertEqual(call[:5], ["ocrmypdf", "-l", "deu+eng", "--output-type", "pdf"])

    def test_tesseract_uses_page_images(self):
        self.use_engines("tesseract")
        (pages, _c, error), data, _fake = self.scan(mode="bw", ocr_lang="eng")
        self.assertIsNone(error)
        self.assertEqual(data, b"%PDF-1.4 fake tesseract output lang=eng pages=2\n")
        call = read_log(self.log)[0]
        files = call[call.index("PAGES") + 1:]
        self.assertEqual([os.path.basename(f) for f in files], ["page001.tif", "page002.tif"])
        self.assertEqual(call[3:8], ["-l", "eng", "--dpi", "300", "pdf"])

    def test_ocr_page_progress_with_tesseract(self):
        self.use_engines("tesseract")
        events = []
        out = os.path.join(self.dir.name, "out.pdf")
        with FakeScanner(**ES580W_PROFILE, sheets=2) as fake:
            tool.scan_to_file(args(host=fake.host, model=None, ocr=True), out,
                              on_progress=lambda event, n: events.append((event, n)),
                              select_pages=lambda pages, blank: [0, 1, 3])
        self.assertEqual(events[-5:], [("saving", 3), ("ocr", 3), ("ocr_page", 1),
                                       ("ocr_page", 2), ("ocr_page", 3)])
        call = read_log(self.log)[0]
        self.assertEqual(len(call[call.index("PAGES") + 1:]), 3, "removed page not recognized")

    def test_no_page_progress_with_ocrmypdf(self):
        self.use_engines("ocrmypdf")
        events = []
        with FakeScanner(**ES580W_PROFILE, sheets=1) as fake:
            tool.scan_to_file(args(host=fake.host, model=None, ocr=True),
                              os.path.join(self.dir.name, "out.pdf"),
                              on_progress=lambda event, n: events.append(event))
        self.assertEqual(events[-2:], ["saving", "ocr"])

    def test_tesseract_jpeg_pages(self):
        self.use_engines("tesseract")
        self.scan()
        call = read_log(self.log)[0]
        self.assertTrue(call[call.index("PAGES") + 1].endswith("page001.jpg"))

    def test_failure_keeps_scan_without_text(self):
        self.use_engines("ocrmypdf")
        os.environ["FAKE_OCR_FAIL"] = "1"
        (pages, _c, error), data, _fake = self.scan()
        self.assertRegex(error, r"text recognition with ocrmypdf failed \(exit 15\): "
                                r"ERROR - tesseract failed")
        self.assertEqual(parse_pdf(data)["pages"], 2, "scanned PDF kept")

    def test_missing_engine_fails_before_scanning(self):
        self.use_engines()
        with FakeScanner(sheets=1) as fake:
            with self.assertRaisesRegex(tool.ScanError, "needs OCRmyPDF or Tesseract"):
                tool.scan_to_file(args(host=fake.host, model=None, ocr=True),
                                  os.path.join(self.dir.name, "x.pdf"))
        self.assertNotIn("CreateScanJob", fake.actions())

    def run_cli(self, fake, *argv, config=None, fail=False):
        env = dict(os.environ, WSDSCAN_CONFIG=os.path.join(self.dir.name, "config.ini"))
        if config:
            with open(env["WSDSCAN_CONFIG"], "w") as f:
                f.write(config)
        if fail:
            env["FAKE_OCR_FAIL"] = "1"
        return subprocess.run([sys.executable, SCRIPT, "--host", fake.host, *argv],
                              cwd=self.dir.name, capture_output=True, text=True, timeout=60,
                              env=env)

    def test_cli(self):
        self.use_engines("tesseract")
        with FakeScanner(**ES580W_PROFILE, sheets=1) as fake:
            r = self.run_cli(fake, "--ocr", "-s", "adf", "a.pdf")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("recognizing text...", r.stderr)
            r = self.run_cli(fake, "--ocr", "-s", "adf", "b.pdf", fail=True)
            self.assertEqual(r.returncode, 3)
            self.assertIn("the PDF was saved without text", r.stderr)
            self.assertTrue(os.path.exists(os.path.join(self.dir.name, "b.pdf")))
            # From the config file, and switched off on the command line.
            r = self.run_cli(fake, "-s", "adf", "c.pdf", config="[scan]\nocr = true\nocr_lang = eng\n")
            self.assertEqual(r.returncode, 0, r.stderr)
            r = self.run_cli(fake, "--no-ocr", "-s", "adf", "d.pdf")
            self.assertNotIn("recognizing", r.stderr)
            r = self.run_cli(fake, "--show-config")
        self.assertEqual(len(read_log(self.log)), 3)
        self.assertIn("OCR engines: tesseract 5.3.4", r.stdout)
        self.assertIn("OCR languages: deu, eng (automatic: deu+eng)", r.stdout)

    def test_cli_without_engines(self):
        self.use_engines()
        with FakeScanner(sheets=1) as fake:
            r = self.run_cli(fake, "--show-config")
            self.assertIn("OCR engines: none installed", r.stdout)
            r = self.run_cli(fake, "--ocr", "x.pdf")
        self.assertEqual(r.returncode, 1)
        self.assertIn("text recognition needs OCRmyPDF or Tesseract", r.stderr)

    def test_config_validation(self):
        for text, message in (("[scan]\nocr_engine = abbyy\n", "ocr_engine: expected one of"),
                              ("[scan]\nocr_lang = Deutsch\n", "ocr_lang: expected Tesseract")):
            with self.subTest(text=text), \
                    mock.patch.dict(os.environ, {"WSDSCAN_CONFIG": os.path.join(self.dir.name, "c.ini")}):
                with open(os.environ["WSDSCAN_CONFIG"], "w") as f:
                    f.write(text)
                with self.assertRaisesRegex(tool.ScanError, message):
                    tool.load_config()


class TicketTest(unittest.TestCase):
    def test_pick_format_prefers_jfif(self):
        self.assertEqual(tool.pick_format(caps(formats=["exif", "jfif"])), "jfif")

    def test_pick_format_falls_back_to_exif(self):
        # Regression: the ES-580W offers no jfif.
        self.assertEqual(tool.pick_format(caps(formats=["exif", "tiff-single-uncompressed"])),
                         "exif")

    def test_pick_format_unknown_list(self):
        self.assertEqual(tool.pick_format(caps(formats=[])), "jfif")

    def test_duplex_ticket(self):
        t = tool.scan_ticket("duplex", "color", 300, "a4", caps())
        self.assertIn("<wscn:InputSource>ADFDuplex</wscn:InputSource>", t)
        self.assertIn("<wscn:MediaBack>", t)
        self.assertIn("<wscn:ColorProcessing>RGB24</wscn:ColorProcessing>", t)
        self.assertIn("<wscn:Width>8268</wscn:Width><wscn:Height>11693</wscn:Height>", t)
        ET.fromstring(t.replace("wscn:", ""))  # well-formed

    def test_single_sided_gray_ticket(self):
        t = tool.scan_ticket("adf", "gray", 200, "letter", caps())
        self.assertIn("<wscn:InputSource>ADF</wscn:InputSource>", t)
        self.assertNotIn("MediaBack", t)
        self.assertIn("Grayscale8", t)
        self.assertIn("<wscn:Width>200</wscn:Width>", t)

    def test_paper_clamped_to_max_size(self):
        t = tool.scan_ticket("adf", "color", 300, "legal", caps(max_size=(8500, 12000)))
        self.assertIn("<wscn:Width>8500</wscn:Width><wscn:Height>12000</wscn:Height>", t)

    def test_validate_accepts_defaults(self):
        tool.validate(args(), caps())

    def test_validate_rejections(self):
        cases = [
            (args(), caps(has_adf=False)),
            (args(source="duplex"), caps(duplex=False)),
            (args(mode="gray"), caps(colors=["RGB24"])),
            (args(resolution=250), caps()),
            (args(), caps(formats=["tiff-single-uncompressed"])),
        ]
        for a, c in cases:
            with self.subTest(a=a, c=c), self.assertRaises(tool.ScanError):
                tool.validate(a, c)


# --- End-to-end tests against the fake scanner -------------------------------

class CliBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def run_tool(self, fake, *argv):
        env = dict(os.environ)
        for var in ("WSDSCAN_HOST", "WSDSCAN_MODEL", "WSDSCAN_SCANNER"):
            env.pop(var, None)
        return subprocess.run([sys.executable, SCRIPT, "--host", fake.host, *argv],
                              cwd=self.tmp.name, capture_output=True, text=True,
                              timeout=60, env=env)

    def pdf(self, name="out.pdf"):
        with open(os.path.join(self.tmp.name, name), "rb") as f:
            return parse_pdf(f.read())


class CliTest(CliBase):
    def test_duplex_scan(self):
        with FakeScanner(sheets=2) as fake:
            r = self.run_tool(fake, "out.pdf")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("saved 4 page(s)", r.stdout)
        self.assertEqual(self.pdf()["pages"], 4)
        self.assertEqual(fake.actions()[:3], ["Probe", "Get", "GetScannerElements"])
        self.assertIn("<wscn:Format>jfif</wscn:Format>", fake.tickets[0])

    def test_es580w_profile_scan(self):
        # Regression: real device has a /WDP/SCAN service and only exif/tiff.
        with FakeScanner(**ES580W_PROFILE, sheets=1) as fake:
            r = self.run_tool(fake, "out.pdf")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("/WDP/SCAN", r.stderr)
        self.assertIn("<wscn:Format>exif</wscn:Format>", fake.tickets[0])
        self.assertEqual(self.pdf()["pages"], 2)

    def test_bw_scan_es580w(self):
        with FakeScanner(**ES580W_PROFILE, sheets=1) as fake:
            r = self.run_tool(fake, "-m", "bw", "out.pdf")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("<wscn:Format>tiff-single-uncompressed</wscn:Format>", fake.tickets[0])
        self.assertIn("BlackAndWhite1", fake.tickets[0])
        with open(os.path.join(self.tmp.name, "out.pdf"), "rb") as f:
            data = f.read()
        self.assertEqual(parse_pdf(data)["pages"], 2)
        self.assertEqual(data.count(b"/BitsPerComponent 1 /Decode [1 0] /Filter /FlateDecode"), 2)

    def test_skip_blank(self):
        text, blank = make_jpeg(page(marks=[TEXT_LINE])), make_jpeg(page())
        with FakeScanner(**ES580W_PROFILE, sheets=2, images=[text, blank]) as fake:
            r = self.run_tool(fake, "--skip-blank", "out.pdf")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("page 2 is blank", r.stderr)
        self.assertIn("saved 2 page(s) to out.pdf (2 blank page(s) removed)", r.stdout)
        self.assertEqual(self.pdf()["pages"], 2)

    def test_lossless_color(self):
        with FakeScanner(**ES580W_PROFILE, sheets=1) as fake:
            r = self.run_tool(fake, "-s", "adf", "--lossless", "out.pdf")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("<wscn:Format>tiff-single-uncompressed</wscn:Format>", fake.tickets[0])
        with open(os.path.join(self.tmp.name, "out.pdf"), "rb") as f:
            data = f.read()
        self.assertIn(b"/ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /FlateDecode", data)
        self.assertNotIn(b"DCTDecode", data)

    def test_brightness_and_contrast(self):
        with FakeScanner(**ES580W_PROFILE, sheets=1) as fake:
            r = self.run_tool(fake, "-s", "adf", "--brightness", "-300",
                              "--contrast", "200", "out.pdf")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("<wscn:Contrast>200</wscn:Contrast><wscn:Brightness>-300</wscn:Brightness>",
                      fake.tickets[0])

    def test_exposure_rejected_by_scanner(self):
        with FakeScanner(exposure_range=(-500, 500), sheets=1) as fake:
            r = self.run_tool(fake, "--brightness", "800", "out.pdf")
        self.assertEqual(r.returncode, 1)
        self.assertIn("ClientErrorInvalidScanTicket", r.stderr)

    def test_bw_not_possible_without_tiff(self):
        with FakeScanner() as fake:  # default fake: jfif/pdf-a only
            r = self.run_tool(fake, "-m", "bw", "out.pdf")
        self.assertEqual(r.returncode, 1)
        self.assertIn("black & white needs the tiff-single-uncompressed format", r.stderr)
        self.assertNotIn("CreateScanJob", fake.actions())

    def test_single_sided_gray(self):
        with FakeScanner(sheets=3) as fake:
            r = self.run_tool(fake, "-s", "adf", "-m", "gray", "-r", "150", "out.pdf")
        self.assertEqual(r.returncode, 0, r.stderr)
        pdf = self.pdf()
        self.assertEqual(pdf["pages"], 3)
        self.assertEqual(set(pdf["colorspaces"]), {b"DeviceGray"})

    def test_page_size_uses_final_dpi(self):
        # If the scanner reports a different final resolution, trust it.
        with FakeScanner(sheets=1, final_dpi=72) as fake:
            r = self.run_tool(fake, "-s", "adf", "out.pdf")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.pdf()["mediaboxes"], [(24.0, 16.0)])

    def test_default_output_name(self):
        with FakeScanner(sheets=1) as fake:
            r = self.run_tool(fake, "-s", "adf")
        self.assertEqual(r.returncode, 0, r.stderr)
        names = os.listdir(self.tmp.name)
        self.assertEqual(len(names), 1)
        self.assertRegex(names[0], r"^scan_\d{4}-\d\d-\d\d_\d\d-\d\d-\d\d\.pdf$")

    def test_refuses_overwrite(self):
        open(os.path.join(self.tmp.name, "out.pdf"), "w").close()
        with FakeScanner() as fake:
            r = self.run_tool(fake, "out.pdf")
            self.assertEqual(r.returncode, 1)
            self.assertIn("already exists", r.stderr)
            self.assertEqual(fake.actions(), [])  # nothing sent to the scanner
            r = self.run_tool(fake, "--force", "out.pdf")
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_empty_feeder(self):
        with FakeScanner(sheets=0) as fake:
            r = self.run_tool(fake, "out.pdf")
        self.assertEqual(r.returncode, 1)
        self.assertIn("no pages scanned", r.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, "out.pdf")))

    def test_busy_scanner_is_retried(self):
        with FakeScanner(behavior="busy", busy_count=2, sheets=1) as fake:
            r = self.run_tool(fake, "out.pdf")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(fake.actions().count("CreateScanJob"), 3)

    def test_jam_saves_partial_and_exits_2(self):
        with FakeScanner(behavior="jam", jam_after=2, sheets=3) as fake:
            r = self.run_tool(fake, "out.pdf")
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn("ServerErrorJobFailed", r.stderr)
        self.assertEqual(self.pdf()["pages"], 2)

    def test_unsupported_resolution_rejected_before_scanning(self):
        with FakeScanner() as fake:
            r = self.run_tool(fake, "-r", "250", "out.pdf")
        self.assertEqual(r.returncode, 1)
        self.assertIn("100, 150, 200, 300, 600", r.stderr)
        self.assertNotIn("CreateScanJob", fake.actions())

    def test_no_jpeg_format(self):
        with FakeScanner(formats=["tiff-single-uncompressed"]) as fake:
            r = self.run_tool(fake, "out.pdf")
        self.assertEqual(r.returncode, 1)
        self.assertIn("no JPEG format", r.stderr)

    def test_no_scanner_answers(self):
        fake = FakeScanner()
        fake.start()
        host = fake.host
        fake.stop()  # port now closed: probe times out
        r = subprocess.run([sys.executable, SCRIPT, "--host", host, "--info"],
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 1)
        self.assertIn("no WSD scanner answered", r.stderr)

    def test_model_filter_with_host(self):
        with FakeScanner(**ES580W_PROFILE, sheets=1) as fake:
            r = self.run_tool(fake, "--model", "es-580w", "-s", "adf", "out.pdf")
            self.assertEqual(r.returncode, 0, r.stderr)
            r = self.run_tool(fake, "--model", "ADS-1700W", "--info")
        self.assertEqual(r.returncode, 1)
        self.assertIn("no scanner matches --model 'ADS-1700W'; found: EPSON ES-580W at 127.0.0.1",
                      r.stderr)

    def test_list(self):
        with FakeScanner(**ES580W_PROFILE) as fake:
            r = self.run_tool(fake, "--list")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertRegex(r.stdout, r"^127\.0\.0\.1\s+EPSON ES-580W  \(firmware 1\.00, "
                                   r"service http://127\.0\.0\.1:\d+/WDP/SCAN\)$")
        self.assertEqual(fake.actions(), ["Probe", "Get"])  # nothing scanner-side

    def test_list_marks_configured_scanners(self):
        with FakeScanner(**ES580W_PROFILE) as fake:
            config = os.path.join(self.tmp.name, "config.ini")
            with open(config, "w") as f:
                f.write("[scanner Office]\nhost = 127.0.0.1\n")
            env = dict(os.environ, WSDSCAN_CONFIG=config)
            r = subprocess.run([sys.executable, SCRIPT, "--host", fake.host, "--list"],
                               capture_output=True, text=True, timeout=60, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(r.stdout.rstrip().endswith("[Office]"), r.stdout)

    def test_host_and_model_from_environment(self):
        with FakeScanner(**ES580W_PROFILE) as fake:
            env = {k: v for k, v in os.environ.items()
                   if k not in ("WSDSCAN_HOST", "WSDSCAN_MODEL")}
            env.update({"WSDSCAN_HOST": fake.host, "WSDSCAN_MODEL": "epson"})
            r = subprocess.run([sys.executable, SCRIPT, "--list"], cwd=self.tmp.name,
                               capture_output=True, text=True, timeout=60, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("EPSON ES-580W", r.stdout)

    def test_old_host_variable_is_ignored(self):
        with mock.patch.dict(os.environ, {"ES580W_HOST": "192.168.2.13"}):
            os.environ.pop("WSDSCAN_HOST", None)
            self.assertIsNone(tool.parse_args([]).host)

    def test_info(self):
        with FakeScanner(**ES580W_PROFILE) as fake:
            r = self.run_tool(fake, "--info")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertRegex(r.stdout, r"model:\s+ES-580W")
        self.assertRegex(r.stdout, r"formats:\s+exif, tiff-single-uncompressed\n")
        self.assertRegex(r.stdout, r"colors:\s+BlackAndWhite1, Grayscale8, RGB24\n")
        self.assertRegex(r.stdout, r"feeder:\s+yes, duplex")
        self.assertNotIn("CreateScanJob", fake.actions())


class CheckModeTest(CliBase):
    def test_check_all_combinations(self):
        with FakeScanner(**ES580W_PROFILE) as fake:
            r = self.run_tool(fake, "--check")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertRegex(r.stdout, r"serial:\s+X123456")
        rows = re.findall(r"^  (adf|duplex)\s+(color|gray|bw)\s+(jpeg|lossless)\s+(\d+)  ok$",
                          r.stdout, re.M)
        # 2 sources x (color/gray as jpeg and lossless, bw lossless) x 5 dpi
        self.assertEqual(len(rows), 2 * 5 * 5)
        self.assertIn(("duplex", "bw", "lossless", "300"), rows)
        self.assertNotIn(("duplex", "bw", "jpeg", "300"), [r for r in rows])
        self.assertIn("usable, format exif", r.stdout)
        # Check mode must never start a scan.
        self.assertNotIn("CreateScanJob", fake.actions())
        self.assertNotIn("RetrieveImage", fake.actions())
        # table + exposure probe (2 settings x 5 values) + the exact settings
        self.assertEqual(fake.actions().count("ValidateScanTicket"), 50 + 10 + 1)

    def test_check_lists_optional_settings(self):
        with FakeScanner(description={"name": "Office scanner"},
                         conditions=[("InputTrayEmpty", "ADF", "Informational")]) as fake:
            r = self.run_tool(fake, "--check")
        self.assertEqual(r.returncode, 0, r.stderr)
        out = r.stdout
        self.assertRegex(out, r"optical res\.:\s+600 x 600 dpi")
        self.assertRegex(out, r"scanner name:\s+Office scanner")
        self.assertRegex(out, r"conditions:\s+InputTrayEmpty \(ADF, Informational\)")
        self.assertIn("Optional settings reported by the scanner", out)
        self.assertRegex(out, r"content types:\s+Auto, Text, Photo")
        self.assertRegex(out, r"auto paper size:\s+yes")
        self.assertRegex(out, r"auto exposure:\s+no")
        self.assertRegex(out, r"JPEG quality:\s+0-100")
        self.assertRegex(out, r"scaling:\s+width 1-1000 %, height 1-1000 %")
        self.assertRegex(out, r"rotations:\s+0, 180")
        self.assertRegex(out, r"other: BlankPageSkip:\s*Level=1/2")
        # The optional settings come before the ticket validation table.
        self.assertLess(out.index("Optional settings"), out.index("Ticket validation"))

    def test_info_minimal_scanner_says_not_reported(self):
        with FakeScanner(**ES580W_PROFILE, device_settings="", optical=None) as fake:
            r = self.run_tool(fake, "--info")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertRegex(r.stdout, r"conditions:\s+none")
        self.assertRegex(r.stdout, r"content types:\s+not reported")
        self.assertRegex(r.stdout, r"brightness:\s+not reported")
        self.assertNotIn("optical res.", r.stdout)
        self.assertNotIn("other:", r.stdout)

    def test_check_exposure_probe(self):
        with FakeScanner(**ES580W_PROFILE, exposure_range=(-500, 500)) as fake:
            r = self.run_tool(fake, "--check")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertRegex(r.stdout, r"brightness\s+accepted: -500, 0, 500; rejected: -1000, 1000")
        self.assertRegex(r.stdout, r"contrast\s+accepted: -500, 0, 500; rejected: -1000, 1000")

    def test_check_exact_settings(self):
        with FakeScanner(**ES580W_PROFILE, exposure_range=(-500, 500)) as fake:
            r = self.run_tool(fake, "--check", "-m", "bw", "--brightness", "800")
            self.assertEqual(r.returncode, 1)
            self.assertIn("bw, 300 dpi, a4, brightness 800) NOT usable: scanner rejected", r.stdout)
            r = self.run_tool(fake, "--check", "-m", "gray", "--lossless", "--contrast", "300")
            self.assertEqual(r.returncode, 0, r.stdout)
            self.assertIn("gray, 300 dpi, a4, lossless, contrast 300) usable, "
                          "format tiff-single-uncompressed", r.stdout)

    def test_check_skips_exposure_when_unsupported(self):
        ds = "<wscn:BrightnessSupported>false</wscn:BrightnessSupported>" \
             "<wscn:ContrastSupported>false</wscn:ContrastSupported>"
        with FakeScanner(device_settings=ds) as fake:
            r = self.run_tool(fake, "--check")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("Exposure (experimental", r.stdout)

    def test_check_reports_rejected_ticket(self):
        reject = {("duplex", "RGB24", 300)}
        with FakeScanner(reject=reject) as fake:
            r = self.run_tool(fake, "--check")
            self.assertEqual(r.returncode, 1)
            self.assertRegex(r.stdout, r"duplex\s+color\s+jpeg\s+300  REJECTED")
            self.assertRegex(r.stdout, r"adf\s+color\s+jpeg\s+300  ok")
            self.assertIn("scanner rejected the ticket", r.stdout)
            # Other settings that the scanner accepts pass.
            r = self.run_tool(fake, "--check", "-s", "adf")
            self.assertEqual(r.returncode, 0, r.stdout)

    def test_check_without_duplex(self):
        with FakeScanner(duplex=False) as fake:
            r = self.run_tool(fake, "--check")
        self.assertEqual(r.returncode, 1)
        self.assertNotRegex(r.stdout, r"(?m)^  duplex")
        self.assertIn("no duplex", r.stdout)

    def test_check_validation_unsupported(self):
        with FakeScanner(validate=False) as fake:
            r = self.run_tool(fake, "--check")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("does not support ticket validation", r.stdout)
        self.assertEqual(fake.actions().count("ValidateScanTicket"), 1)

    def test_check_flags_unusable_settings(self):
        with FakeScanner(formats=["tiff-single-uncompressed"]) as fake:
            r = self.run_tool(fake, "--check")
        self.assertEqual(r.returncode, 1)
        self.assertIn("no JPEG format", r.stdout)


if __name__ == "__main__":
    unittest.main()
