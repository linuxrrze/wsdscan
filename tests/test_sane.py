"""Tests for scanning through SANE (scanimage), with a fake scanimage."""

import os
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

import wsdscan as tool  # noqa: E402
from fake_sane import (AIRSCAN_OPTIONS, EPSONDS_OPTIONS, configure,  # noqa: E402
                       make_scanimage_bin, read_log)
from fake_wsd import JPEG_RGB, make_tiff  # noqa: E402
from test_wsdscan import parse_pdf  # noqa: E402

EPSON = "epsonds:net:192.168.2.13"
AIRSCAN = "airscan:w0:ES-580W WSD"
DEVICES = [[EPSON, "Epson", "ES-580W", "ESC/I-2"], [AIRSCAN, "WSD", "ES-580W WSD", "ip=192.168.2.13"]]
OPTIONS = {EPSON: EPSONDS_OPTIONS, AIRSCAN: AIRSCAN_OPTIONS}


def args(**overrides):
    base = {"backend": "sane", "device": EPSON, "host": None, "model": None, "source": "duplex",
            "mode": "color", "resolution": 300, "paper": "a4", "lossless": False,
            "brightness": None, "contrast": None, "ocr": False, "ocr_engine": "auto",
            "ocr_lang": None, "skip_blank": False, "deskew": False, "auto_rotate": False,
            "crop": "sides", "hardware_corrections": True, "sane_options": ""}
    base.update(overrides)
    return SimpleNamespace(**base)


class SaneBase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.bin = os.path.join(self.dir.name, "bin")
        self.page = os.path.join(self.dir.name, "page.jpg")
        with open(self.page, "wb") as f:
            f.write(JPEG_RGB)
        make_scanimage_bin(self.bin, devices=DEVICES, options=OPTIONS, pages=[self.page] * 3)
        env = mock.patch.dict(os.environ, {"PATH": self.bin})
        env.start()
        self.addCleanup(env.stop)

    def configure(self, **config):
        configure(self.bin, **dict({"devices": DEVICES, "options": OPTIONS}, **config))

    def caps(self, name=EPSON):
        return tool.device_capabilities(tool.sane_device(name))

    def scans(self):
        return [call for call in read_log(self.bin) if any(a.startswith("--batch=") for a in call)]


class OptionsTest(SaneBase):
    def test_parse_real_options(self):
        options = tool.parse_sane_options(EPSONDS_OPTIONS)
        self.assertEqual(options["source"]["values"], ["ADF Front", "ADF Duplex"])
        self.assertEqual(options["resolution"]["values"][-1], "600")
        self.assertEqual(options["resolution"]["unit"], "dpi")
        self.assertEqual(options["x"]["range"], (0.0, 215.9, 0.0))
        self.assertEqual(options["x"]["unit"], "mm")
        self.assertTrue(options["adf-crp"]["bool"])
        self.assertEqual(options["adf-crp"]["default"], "no")
        self.assertFalse(options["load"]["active"])
        self.assertIsNone(options["eject"]["values"], "a button")
        air = tool.parse_sane_options(AIRSCAN_OPTIONS)
        self.assertEqual(air["brightness"]["range"], (-100.0, 100.0, 1.0))
        self.assertEqual(air["brightness"]["unit"], "%")
        self.assertEqual(air["analog-gamma"]["range"][1], 4.0)
        self.assertFalse(air["adf-justification-x"]["active"])

    def test_capabilities(self):
        caps = self.caps()
        self.assertTrue(caps["has_adf"] and caps["duplex"])
        self.assertEqual(caps["colors"], ["RGB24", "Grayscale8", "BlackAndWhite1"])
        self.assertEqual(caps["resolutions"], [50, 75, 100, 150, 200, 240, 300, 360, 400, 600])
        self.assertEqual(caps["max_size"], (8500, 15500))
        sane = caps["sane"]
        self.assertEqual(sane["source"], {"adf": "ADF Front", "duplex": "ADF Duplex"})
        self.assertEqual(sane["mode"]["bw"], ["--mode", "Lineart", "--depth", "1"])
        self.assertEqual((sane["crop"], sane["skew"]), ("adf-crp", "adf-skew"))
        self.assertFalse(caps["device_settings"]["brightness"])
        air = self.caps(AIRSCAN)
        self.assertEqual(air["colors"], ["RGB24", "Grayscale8"], "no black & white")
        self.assertEqual(air["sane"]["source"]["adf"], "ADF")
        self.assertIsNone(air["sane"]["crop"])
        self.assertTrue(air["device_settings"]["brightness"])
        self.assertIn("mode bw (BlackAndWhite1) not supported",
                      " ".join(tool.settings_problems(args(mode="bw"), air)))

    def test_gray_with_one_bit_is_black_and_white(self):
        self.configure(options={"test:0": """
    --mode Gray|Color [Gray]
    --depth 1|8|16 [8]
    --resolution 1..1200dpi (in steps of 1) [50]
    --source Flatbed|Automatic Document Feeder [Flatbed]
    -x 0..200mm [80]
    -y 0..200mm [100]
"""})
        caps = self.caps("test:0")
        self.assertEqual(caps["sane"]["mode"]["bw"], ["--mode", "Gray", "--depth", "1"])
        self.assertEqual(caps["sane"]["source"]["adf"], "Automatic Document Feeder")
        self.assertEqual(caps["resolutions"], [75, 100, 150, 200, 300, 400, 600, 1200])
        self.assertFalse(caps["duplex"])
        self.assertTrue(caps["has_platen"])

    def test_command(self):
        cmd = tool.sane_command(tool.sane_device(AIRSCAN), args(device=AIRSCAN, mode="gray",
                                                                brightness=500, contrast=-1000),
                                self.caps(AIRSCAN), "/x/page%04d.jpg", "jpeg")
        self.assertEqual(cmd, ["-d", AIRSCAN, "--source", "ADF Duplex", "--mode", "Gray",
                               "--resolution", "300", "-l", "0", "-t", "0", "-x", "210",
                               "-y", "297", "--brightness", "50", "--contrast", "-100",
                               "--format=jpeg", "--batch=/x/page%04d.jpg", "--batch-print"])

    def test_scanner_corrections(self):
        caps, device = self.caps(), tool.sane_device(EPSON)

        def extra(**overrides):
            cmd = tool.sane_command(device, args(**overrides), caps, "p%d.jpg", "jpeg")
            return [a for a in cmd if a.startswith("--adf-")], cmd[cmd.index("-x") + 1:
                                                                  cmd.index("-y") + 2]

        self.assertEqual(extra(paper="auto", deskew=True),
                         (["--adf-crp=yes", "--adf-skew=yes"], ["215.9", "-y", "393.7"]))
        self.assertEqual(extra(paper="auto")[0], ["--adf-crp=yes"])
        self.assertEqual(extra(deskew=True)[0], ["--adf-skew=yes"])
        self.assertEqual(extra(paper="auto", deskew=True, hardware_corrections=False)[0], [])
        # No own straightening: cropping would remove the edges this tool straightens by.
        caps["sane"]["skew"] = None
        self.assertEqual(extra(paper="auto", deskew=True)[0], [])
        self.assertEqual(extra(paper="auto")[0], ["--adf-crp=yes"])

    def test_extra_options(self):
        self.assertEqual(tool.parse_sane_extra("--adf-justification-x=center --negative"),
                         ["--adf-justification-x=center", "--negative"])
        self.assertEqual(tool.parse_sane_extra("--test-picture='Color pattern'"),
                         ["--test-picture=Color pattern"])
        for bad in ("-x 10", "--batch=/tmp/x", "--resolution=600", "foo", "--format=png", "'"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                tool.parse_sane_extra(bad)
        cmd = tool.sane_command(tool.sane_device(EPSON), args(sane_options="--eject"),
                                self.caps(), "p%d.jpg", "jpeg")
        self.assertEqual(cmd[-4:], ["--eject", "--format=jpeg", "--batch=p%d.jpg", "--batch-print"])


class DevicesTest(SaneBase):
    def test_find(self):
        self.assertEqual(tool.find_device("sane", device=EPSON)["model"], "ES-580W")
        self.assertEqual(tool.find_device("sane", model="ES-580W WSD")["name"], AIRSCAN)
        with self.assertRaises(tool.ScannerChoiceNeeded):
            tool.find_device("sane")
        self.configure(devices=DEVICES[:1])
        self.assertEqual(tool.find_device("sane")["name"], EPSON)
        self.configure(devices=[])
        with self.assertRaisesRegex(tool.ScannerNotFound, "scanimage -L"):
            tool.find_device("sane")
        # A network device SANE does not list (yet) can still be named.
        self.assertEqual(tool.find_device("sane", device=EPSON)["model"], EPSON)

    def test_unavailable(self):
        with self.assertRaisesRegex(tool.ScannerNotFound, "open of device nope:0 failed"):
            tool.device_capabilities(tool.sane_device("nope:0"))

    def test_scanimage_missing(self):
        with mock.patch.dict(os.environ, {"PATH": os.path.join(self.dir.name, "empty")}), \
                self.assertRaisesRegex(tool.ScanError, "sane-utils"):
            tool.find_device("sane", device=EPSON)

    def test_address(self):
        self.assertEqual(tool.device_address(tool.sane_device(EPSON)), EPSON)
        self.assertEqual(tool.device_address({"device_url": "http://192.168.2.13:80/WSD/DEVICE"}),
                         "192.168.2.13")


class ScanTest(SaneBase):
    def scan(self, **overrides):
        out = os.path.join(self.dir.name, "out.pdf")
        events, infos = [], []
        result = tool.scan_to_file(args(**overrides), out,
                                   on_progress=lambda e, n: events.append((e, n)),
                                   on_page_image=lambda n, image, info: infos.append(info))
        with open(out, "rb") as f:
            return result, parse_pdf(f.read()), events, infos

    def test_scan_to_pdf(self):
        (pages, complete, ocr_error), pdf, events, infos = self.scan()
        self.assertEqual((pages, complete, ocr_error), (3, True, None))
        self.assertEqual(pdf["pages"], 3)
        self.assertEqual([e for e in events if e[0] == "page"], [("page", 1), ("page", 2),
                                                                  ("page", 3)])
        call = self.scans()[0]
        self.assertEqual(call[:4], ["-d", EPSON, "--source", "ADF Duplex"])
        self.assertIn("--format=jpeg", call)
        pattern = call[call.index("--batch-print") - 1].split("=", 1)[1]
        self.assertFalse(os.path.exists(os.path.dirname(pattern)), "page files removed")

    def test_lossless_and_bw_as_tiff(self):
        tiff = os.path.join(self.dir.name, "page.tif")
        with open(tiff, "wb") as f:
            f.write(make_tiff("BlackAndWhite1"))
        self.configure(pages=[tiff])
        (pages, _complete, _ocr), pdf, _events, _infos = self.scan(mode="bw")
        self.assertEqual(pdf["pages"], 1)
        self.assertIn("--format=tiff", self.scans()[0])
        self.assertIn("Lineart", self.scans()[0])

    def test_scanner_cropped_pages_not_cut_again(self):
        _result, _pdf, _events, infos = self.scan(paper="auto", deskew=True, skip_blank=True)
        self.assertIn("--adf-crp=yes", self.scans()[0])
        self.assertFalse(infos[0]["crop"], "the scanner cut the page already")
        self.assertEqual(infos[0]["skew"], 0)

    def test_scanner_cropping_switched_off(self):
        # epsonds switches its cropping off when scanimage sets the scan area:
        # then this tool cuts the pages itself.
        self.configure(pages=[self.page], switched_off=["adf-crp"])
        self.assertEqual((self.caps()["sane"]["crop"], self.caps()["sane"]["skew"]),
                         (None, "adf-skew"))
        with mock.patch.object(tool, "analyze_page", wraps=tool.analyze_page) as analyze:
            self.scan(paper="auto", deskew=True)
        self.assertNotIn("--adf-crp=yes", self.scans()[0])
        self.assertIn("--adf-skew=yes", self.scans()[0])
        analysis = analyze.call_args.args[1]
        self.assertEqual((analysis.paper, analysis.deskew), ("auto", False),
                         "cut by this tool, straightened by the scanner")

    def test_empty_feeder(self):
        self.configure(pages=[], exit=7,
                       stderr="scanimage: sane_start: Document feeder out of documents")
        with self.assertRaisesRegex(tool.ScanError, "is paper loaded"):
            self.scan()

    def test_end_of_feeder_after_pages_is_complete(self):
        self.configure(pages=[self.page], exit=7)
        (pages, complete, _ocr), _pdf, _events, _infos = self.scan()
        self.assertEqual((pages, complete), (1, True))

    def test_jam_after_pages(self):
        self.configure(pages=[self.page] * 2, exit=1,
                       stderr="scanimage: sane_read: Document feeder jammed")
        with mock.patch("sys.stderr") as err:
            (pages, complete, _ocr), _pdf, _events, _infos = self.scan()
        self.assertEqual((pages, complete), (2, False))
        self.assertIn("jammed", "".join(str(c) for c in err.write.call_args_list))

    def test_scanner_error_cause(self):
        # epsonds names the cause only in its debug output, which this tool asks for.
        self.assertEqual(tool.sane_environment(tool.sane_device(EPSON))["SANE_DEBUG_EPSONDS"], "1")
        self.assertNotIn("SANE_DEBUG_EPSONDS", tool.sane_environment(tool.sane_device(AIRSCAN)))
        self.configure(pages=[self.page] * 2, exit=9, stderr=(
            "[epsonds] esci2_img: error on option ADF, cause DFED\n"
            "scanimage: sane_start: Error during device I/O"))
        with mock.patch("sys.stderr") as err:
            (pages, complete, _ocr), _pdf, _events, _infos = self.scan()
        self.assertEqual((pages, complete), (2, False))
        self.assertIn("double feed", "".join(str(c) for c in err.write.call_args_list))
        self.assertEqual(tool.sane_error("x: error on option ADF, cause LTF \n"), "scanner error LTF")

    def test_failure_without_pages(self):
        self.configure(pages=[], exit=9, stderr="scanimage: sane_start: Error during device I/O")
        with self.assertRaisesRegex(tool.ScanError, r"exit 9\): .*device I/O"):
            self.scan()

    def test_not_a_page_file(self):
        secret = os.path.join(self.dir.name, "secret.jpg")
        with open(secret, "wb") as f:
            f.write(JPEG_RGB)
        self.configure(pages=[self.page], extra_lines=[secret, "../secret.jpg", ""])
        (pages, _complete, _ocr), _pdf, _events, _infos = self.scan()
        self.assertEqual(pages, 1, "only files in the scan's own folder")
        self.assertTrue(os.path.exists(secret))

    def test_wrong_format(self):
        self.configure(pages=[os.path.join(ROOT, "README.md")])
        with self.assertRaisesRegex(tool.ScanError, "no JPEG image"):
            self.scan()

    def test_cancel(self):
        self.configure(pages=[self.page] * 5, delay=0.5)
        seen = []
        start = time.monotonic()
        with self.assertRaises(tool.ScanCancelled):
            tool.scan_to_file(args(), os.path.join(self.dir.name, "out.pdf"),
                              on_page_image=lambda n, image, info: seen.append(n),
                              should_stop=lambda: len(seen) >= 1)
        self.assertLess(time.monotonic() - start, 2.5, "stopped right away")
        self.assertEqual(seen, [1])
        self.assertFalse(os.path.exists(os.path.join(self.dir.name, "out.pdf")))


class ConfigAndCliTest(SaneBase):
    def run_tool(self, *argv):
        env = dict(os.environ, WSDSCAN_CONFIG=os.path.join(self.dir.name, "config.ini"))
        for var in ("WSDSCAN_HOST", "WSDSCAN_MODEL", "WSDSCAN_SCANNER"):
            env.pop(var, None)
        return subprocess.run([sys.executable, SCRIPT, *argv], cwd=self.dir.name,
                              capture_output=True, text=True, timeout=60, env=env)

    def test_config_values(self):
        self.assertEqual(tool.CONFIG_DEFAULTS["backend"], "wsd")
        self.assertEqual(tool.parse_config_value("backend", "sane"), "sane")
        self.assertEqual(tool.parse_config_value("device", EPSON), EPSON)
        self.assertIs(tool.parse_config_value("hardware_corrections", "off"), False)
        self.assertEqual(tool.parse_config_value("sane_options", "--eject"), "--eject")
        for key, bad in (("backend", "usb"), ("device", "-d x"), ("sane_options", "--batch=x")):
            with self.subTest(key=key), self.assertRaises(ValueError):
                tool.parse_config_value(key, bad)

    def test_list_info_and_scan(self):
        r = self.run_tool("--backend", "sane", "-L")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(f"{EPSON}", r.stdout)
        self.assertIn("Epson ES-580W (ESC/I-2)", r.stdout)
        r = self.run_tool("--backend", "sane", "--device", EPSON, "-i")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("own cropping:     yes (--adf-crp)", r.stdout)
        self.assertIn("--adf-skew yes|no [no]", r.stdout)
        r = self.run_tool("--backend", "sane", "--device", EPSON, "-p", "auto",
                          "--sane-options=--eject", "out.pdf")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("saved 3 page(s)", r.stdout)
        self.assertIn("--eject", self.scans()[-1])
        r = self.run_tool("--backend", "sane", "--device", EPSON, "-c")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("with SANE, use --info", r.stderr)
        r = self.run_tool("--backend", "sane", "--sane-options=--batch=x", "out2.pdf")
        self.assertIn("--batch is set by this tool", r.stderr)

    def test_profile(self):
        with open(os.path.join(self.dir.name, "config.ini"), "w") as f:
            f.write(f"[scan]\nscanner = Office\n[scanner Office]\nbackend = sane\n"
                    f"device = {EPSON}\nsource = adf\n")
        r = self.run_tool("out.pdf")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.scans()[-1][:4], ["-d", EPSON, "--source", "ADF Front"])


if __name__ == "__main__":
    unittest.main()
