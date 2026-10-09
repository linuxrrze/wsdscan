"""Tests for the toolkit-independent GUI logic and the desktop files.

Run from the repository root:  python3 -m unittest discover -s gui/tests -v
"""

import configparser
import os
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
GUI = os.path.dirname(HERE)
ROOT = os.path.dirname(GUI)
sys.path[:0] = [GUI, ROOT, os.path.join(ROOT, "tests")]
os.environ["WSDSCAN_CONFIG"] = os.path.join(tempfile.mkdtemp(prefix="wsdscan-gui-test-"),
                                            "config.ini")

import scanform  # noqa: E402
import wsdscan  # noqa: E402
from fake_wsd import FakeScanner  # noqa: E402

# Capabilities as reported by the real ES-580W.
ES580W_CAPS = {
    "formats": ["exif", "tiff-single-uncompressed"],
    "colors": ["RGB24", "Grayscale8", "BlackAndWhite1"],
    "resolutions": [100, 300], "duplex": True,
    "device_settings": {"brightness": True, "contrast": True},
}


def values(choices):
    return [v for v, _label in choices]


class ChoicesTest(unittest.TestCase):
    def test_unknown_offers_everything(self):
        c = scanform.Choices.unknown()
        self.assertEqual(values(c.modes), ["color", "gray", "bw"])
        self.assertEqual(values(c.sources), ["duplex", "adf"])
        self.assertEqual(c.lossless, "free")
        self.assertTrue(c.exposure)

    def test_es580w(self):
        c = scanform.Choices.from_capabilities(ES580W_CAPS)
        self.assertEqual(values(c.modes), ["color", "gray", "bw"])
        self.assertEqual(c.resolutions, [100, 300])
        self.assertEqual(c.lossless, "free")
        self.assertTrue(c.exposure)

    def test_jpeg_only_simplex_scanner(self):
        c = scanform.Choices.from_capabilities({
            "formats": ["jfif"], "colors": ["RGB24", "Grayscale8", "BlackAndWhite1"],
            "resolutions": [200], "duplex": False,
            "device_settings": {"brightness": False, "contrast": False}})
        self.assertEqual(values(c.modes), ["color", "gray"], "b/w needs TIFF")
        self.assertEqual(values(c.sources), ["adf"])
        self.assertEqual(c.lossless, "off")
        self.assertFalse(c.exposure)

    def test_tiff_only_scanner_forces_lossless(self):
        c = scanform.Choices.from_capabilities({"formats": ["tiff-single-uncompressed"],
                                                "colors": [], "resolutions": []})
        self.assertEqual(c.lossless, "on")
        self.assertEqual(c.resolutions, scanform.DEFAULT_RESOLUTIONS)
        self.assertEqual(c.lossless_state("color", False)[:2], (True, False))

    def test_lossless_state(self):
        c = scanform.Choices.from_capabilities(ES580W_CAPS)
        self.assertEqual(c.lossless_state("bw", False)[:2], (True, False))
        self.assertEqual(c.lossless_state("color", False)[:2], (False, True))
        self.assertEqual(c.lossless_state("color", True)[:2], (True, True))

    def test_pick(self):
        c = scanform.Choices.from_capabilities(ES580W_CAPS)
        self.assertEqual(c.pick_resolution(300), 300)
        self.assertEqual(c.pick_resolution(600), 300)
        self.assertEqual(c.pick_resolution(150), 100)  # tie goes to the lower value
        self.assertEqual(c.pick(c.sources, "adf"), "adf")
        self.assertEqual(c.pick([("adf", "x")], "duplex"), "adf")

    def test_real_capabilities_from_fake_scanner(self):
        with FakeScanner(model="ES-580W", formats=["exif", "tiff-single-uncompressed"],
                         resolutions=[100, 300]) as fake:
            device = wsdscan.find_scanner(fake.host)
            caps = wsdscan.get_capabilities(device["service"])
        c = scanform.Choices.from_capabilities(caps)
        self.assertEqual(c.resolutions, [100, 300])
        title, subtitle = scanform.describe_scanner(device, caps)
        self.assertEqual(title, "EPSON ES-580W")
        self.assertEqual(subtitle, "127.0.0.1 · Idle")
        # Configured name: same title as in the preferences, device in the subtitle.
        self.assertEqual(scanform.describe_scanner(device, caps, "Office"),
                         ("Office", "EPSON ES-580W · 127.0.0.1 · Idle"))
        self.assertEqual(scanform.describe_scanner(device, caps, "EPSON ES-580W"),
                         ("EPSON ES-580W", "127.0.0.1 · Idle"))
        self.assertEqual(scanform.device_label(device), "EPSON ES-580W · 127.0.0.1")


class OcrStatusTest(unittest.TestCase):
    def test_unavailable(self):
        status = scanform.OcrStatus([], [])
        self.assertFalse(status.available)
        self.assertEqual(status.engine_options(), [("auto", "Automatic")])
        self.assertIn("sudo apt install ocrmypdf", status.describe("auto", ""))

    def test_available(self):
        with mock.patch.dict(os.environ, {"LANG": "de_DE.UTF-8", "LC_ALL": "", "LC_MESSAGES": ""}):
            status = scanform.OcrStatus(["ocrmypdf", "tesseract"], ["deu", "eng"])
        self.assertEqual(status.default_lang, "deu+eng")
        self.assertEqual([v for v, _l in status.engine_options()], ["auto", "ocrmypdf", "tesseract"])
        self.assertEqual(status.describe("auto", ""), "OCRmyPDF · deu+eng")
        self.assertEqual(status.describe("tesseract", "eng"), "Tesseract · eng")

    def test_configured_engine_missing_falls_back(self):
        status = scanform.OcrStatus(["tesseract"], ["eng"])
        self.assertEqual(status.describe("ocrmypdf", "eng"), "Tesseract · eng")
        self.assertEqual([v for v, _l in status.engine_options()], ["auto", "tesseract"])

    def test_language_choices(self):
        with mock.patch.dict(os.environ, {"LANG": "en_US.UTF-8", "LC_ALL": "", "LC_MESSAGES": ""}):
            status = scanform.OcrStatus(["tesseract"], ["deu", "eng", "fra"])
        self.assertEqual(status.default_lang, "eng+deu+fra")
        choices = status.language_choices("deu")
        self.assertEqual(choices, [("eng", "English (eng)", False, True),
                                   ("deu", "German (deu)", True, True),
                                   ("fra", "French (fra)", False, True)])
        self.assertEqual(scanform.OcrStatus.join_languages(choices, {"fra", "deu"}), "deu+fra")
        self.assertEqual(scanform.OcrStatus.join_languages(choices, set()), "")

    def test_configured_language_not_installed_is_listed(self):
        status = scanform.OcrStatus(["tesseract"], ["eng"])
        choices = status.language_choices("eng+ita")
        self.assertIn(("ita", "Italian (ita)", True, False), choices)
        self.assertEqual(scanform.language_label("xyz"), "xyz")

    def test_detect_uses_path(self):
        from fake_ocr import make_ocr_bin
        with tempfile.TemporaryDirectory() as d:
            make_ocr_bin(d, ["tesseract"])
            with mock.patch.dict(os.environ, {"PATH": d}):
                status = scanform.OcrStatus.detect()
        self.assertEqual((status.engines, status.languages), (["tesseract"], ["deu", "eng"]))


class FormTest(unittest.TestCase):
    def test_scan_args(self):
        cfg = dict(wsdscan.CONFIG_DEFAULTS, host="192.168.2.13", mode="bw", resolution=100,
                   brightness=-200)
        a = scanform.scan_args(cfg)
        self.assertEqual((a.host, a.model, a.mode, a.resolution, a.lossless, a.brightness,
                          a.contrast), ("192.168.2.13", None, "bw", 100, False, -200, None))
        self.assertEqual((a.ocr, a.ocr_engine, a.ocr_lang), (False, "auto", None))
        a = scanform.scan_args(dict(cfg, ocr=True, ocr_engine="tesseract", ocr_lang="deu"))
        self.assertEqual((a.ocr, a.ocr_engine, a.ocr_lang), (True, "tesseract", "deu"))
        self.assertFalse(a.skip_blank)
        self.assertTrue(scanform.scan_args(dict(cfg, skip_blank=True)).skip_blank)

    def test_settings_summary(self):
        cfg = dict(wsdscan.CONFIG_DEFAULTS)
        self.assertEqual(scanform.settings_summary(cfg), "Both sides · Color · 300 dpi · A4")
        cfg.update(source="adf", mode="gray", resolution=100, paper="letter", lossless=True,
                   brightness=-200, contrast=0)
        self.assertEqual(scanform.settings_summary(cfg),
                         "One side · Grayscale · 100 dpi · Letter · lossless · brightness -200 · "
                         "contrast 0")
        cfg.update(mode="bw")
        self.assertNotIn("lossless", scanform.settings_summary(cfg), "implied for b/w")
        self.assertTrue(scanform.settings_summary(dict(cfg, ocr=True)).endswith(" · OCR"))
        self.assertTrue(scanform.settings_summary(dict(cfg, skip_blank=True), review=True)
                        .endswith(" · remove blank pages · review pages"))

    def test_needs_reconnect(self):
        old = dict(wsdscan.CONFIG_DEFAULTS)
        new = dict(old, host="192.168.2.13")
        self.assertTrue(scanform.needs_reconnect(old, new, connected=True), "address changed")
        self.assertTrue(scanform.needs_reconnect(old, dict(old, model="epson"), connected=True))
        self.assertFalse(scanform.needs_reconnect(old, dict(old, mode="bw"), connected=True))
        # Regression: the banner stayed because nothing reconnected after saving.
        self.assertTrue(scanform.needs_reconnect(old, dict(old), connected=False),
                        "not connected: always try again")

    def test_output_path(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(scanform.output_path(d, "Invoice"), os.path.join(d, "Invoice.pdf"))
            open(os.path.join(d, "Invoice.pdf"), "w").close()
            self.assertEqual(scanform.output_path(d, "Invoice.pdf"),
                             os.path.join(d, "Invoice (2).pdf"))
            self.assertEqual(scanform.output_path(d, "a/b"), os.path.join(d, "a_b.pdf"))
            for empty in ("", "  ", ".pdf"):
                self.assertIsNone(scanform.output_path(d, empty))

    def test_progress_text(self):
        self.assertEqual(scanform.progress_text("page", 1), "Scanned 1 page")
        self.assertEqual(scanform.progress_text("page", 3), "Scanned 3 pages")
        self.assertIn("Saving", scanform.progress_text("saving", 3))
        self.assertEqual(scanform.progress_text("ocr", 3), "Recognizing text…")

    def test_gui_config(self):
        self.assertEqual(scanform.load_gui_config(), scanform.GUI_DEFAULTS)
        wsdscan.save_config({}, sections={"gui": {"open_after_scan": True, "width": 600}})
        cfg = scanform.load_gui_config()
        self.assertEqual((cfg["open_after_scan"], cfg["width"], cfg["notify"]), (True, 600, True))
        os.remove(wsdscan.config_path())

    def test_display_path(self):
        with mock.patch.dict(os.environ, {"HOME": "/home/anna"}):
            self.assertEqual(scanform.display_path("/home/anna/Documents/Scans"), "~/Documents/Scans")
            self.assertEqual(scanform.display_path("/home/anna"), "~")
            self.assertEqual(scanform.display_path("/home/annabel/x"), "/home/annabel/x")
            self.assertEqual(scanform.display_path("/srv/scans"), "/srv/scans")

    def test_documents_dir(self):
        with tempfile.TemporaryDirectory() as home, mock.patch.dict(os.environ, {"HOME": home}):
            with mock.patch("subprocess.run", side_effect=OSError):
                self.assertEqual(scanform.documents_dir(), home)
                os.mkdir(os.path.join(home, "Documents"))
                self.assertEqual(scanform.documents_dir(), os.path.join(home, "Documents"))
            docs = os.path.join(home, "Dokumente")
            os.mkdir(docs)
            run = mock.Mock(return_value=mock.Mock(stdout=docs + "\n"))
            with mock.patch("subprocess.run", run):
                self.assertEqual(scanform.documents_dir(), docs)


class ScannerProfilesTest(unittest.TestCase):
    def test_subtitle(self):
        self.assertEqual(scanform.scanner_subtitle({"host": "192.168.2.13"}), "192.168.2.13")
        self.assertEqual(scanform.scanner_subtitle({"model": "ADS"}),
                         "Found automatically · model “ADS”")
        self.assertEqual(scanform.scanner_subtitle({"host": "h", "mode": "bw", "outdir": "/x"}),
                         "h")

    def test_unique_name(self):
        self.assertEqual(scanform.unique_scanner_name("Office", {}), "Office")
        self.assertEqual(scanform.unique_scanner_name("Office", {"Office": {}, "Office 2": {}}),
                         "Office 3")
        self.assertEqual(scanform.unique_scanner_name("  A [b]  ", {}), "A (b)")
        self.assertEqual(scanform.unique_scanner_name("", {}), "Scanner")
        wsdscan.check_scanner_name(scanform.unique_scanner_name("x]" * 50, {}))

    def test_from_device(self):
        device = {"manufacturer": "EPSON", "model": "ES-580W",
                  "device_url": "http://192.168.2.13:80/WSD/DEVICE"}
        shared = dict(wsdscan.CONFIG_DEFAULTS, mode="gray", host="ignored")
        name, profile = scanform.scanner_from_device(device, {}, shared)
        self.assertEqual(name, "EPSON ES-580W")
        self.assertEqual((profile["host"], profile["model"], profile["mode"]),
                         ("192.168.2.13", "", "gray"))
        self.assertEqual(set(profile), set(scanform.SCANNER_KEYS))
        self.assertEqual(scanform.scanner_from_device(device, {"EPSON ES-580W": {}}, shared)[0],
                         "EPSON ES-580W 2")

    def test_full_profile(self):
        shared = dict(wsdscan.CONFIG_DEFAULTS, mode="gray", host="10.0.0.1", ocr=True)
        profile = scanform.full_profile(shared, {"host": "10.0.0.2", "mode": "bw"})
        self.assertEqual((profile["host"], profile["mode"], profile["ocr"], profile["model"]),
                         ("10.0.0.2", "bw", True, ""))
        self.assertEqual(scanform.full_profile(shared, {})["host"], "", "address not inherited")
        self.assertNotIn("scanner", profile)

    def test_migrate_single_scanner_from_scan_section(self):
        shared = dict(wsdscan.CONFIG_DEFAULTS, host="192.168.2.13", model="ES-580W", mode="bw")
        scanners, default, migrated = scanform.migrate_scanners(shared, {})
        self.assertEqual((list(scanners), default, migrated), (["ES-580W"], "ES-580W", True))
        self.assertEqual((scanners["ES-580W"]["host"], scanners["ES-580W"]["mode"]),
                         ("192.168.2.13", "bw"), "keeps its settings")
        empty = dict(wsdscan.CONFIG_DEFAULTS)
        self.assertEqual(scanform.migrate_scanners(empty, {}), ({}, "", False))

    def test_migrate_names_after_connected_device(self):
        # Regression: an address-only setup became "Scanner" in the preferences
        # while the main window showed "EPSON ES-580W".
        device = {"manufacturer": "EPSON", "model": "ES-580W",
                  "device_url": "http://192.168.2.13:80/WSD/DEVICE"}
        shared = dict(wsdscan.CONFIG_DEFAULTS, host="192.168.2.13")
        scanners, default, _m = scanform.migrate_scanners(shared, {}, device)
        self.assertEqual(default, "EPSON ES-580W")
        self.assertEqual(scanners["EPSON ES-580W"]["host"], "192.168.2.13")
        # Found automatically (no address configured): keep the found address.
        scanners, _d, _m = scanform.migrate_scanners(dict(wsdscan.CONFIG_DEFAULTS), {}, device)
        self.assertEqual(scanners["EPSON ES-580W"]["host"], "192.168.2.13")

    def test_existing_profiles_and_default(self):
        profiles = {"A": {"host": "1"}, "B": {"host": "2"}}
        shared = dict(wsdscan.CONFIG_DEFAULTS, scanner="B")
        scanners, default, migrated = scanform.migrate_scanners(shared, profiles)
        self.assertEqual((list(scanners), default, migrated), (["A", "B"], "B", False))
        self.assertEqual(scanners["B"]["host"], "2")
        self.assertEqual(set(scanners["B"]), set(scanform.SCANNER_KEYS), "complete settings")
        shared["scanner"] = "Gone"
        self.assertEqual(scanform.migrate_scanners(shared, profiles)[1], "")
        self.assertEqual(scanform.migrate_scanners(dict(wsdscan.CONFIG_DEFAULTS), profiles)[1], "A")


class PreviewTextTest(unittest.TestCase):
    def test_pages_summary(self):
        self.assertEqual(scanform.pages_summary(1), "1 page")
        self.assertEqual(scanform.pages_summary(4), "4 pages")
        self.assertEqual(scanform.pages_summary(4, 4), "4 pages")
        self.assertEqual(scanform.pages_summary(4, 3), "3 of 4 pages kept")

    def test_progress_texts(self):
        self.assertEqual(scanform.progress_text("ocr_page", 2, 4), "Recognizing text: page 2 of 4")
        self.assertIn("Save", scanform.progress_text("review", 4))

    def test_summary_mentions_review(self):
        cfg = dict(wsdscan.CONFIG_DEFAULTS)
        self.assertTrue(scanform.settings_summary(cfg, review=True).endswith(" · review pages"))
        self.assertNotIn("review", scanform.settings_summary(cfg))


class TrayMenuTest(unittest.TestCase):
    def test_layout(self):
        root_id, root_props, children = scanform.tray_menu_layout({"scan": False})
        self.assertEqual((root_id, root_props), (0, {"children-display": "submenu"}))
        self.assertEqual([c[0] for c in children], [1, 2, 3, 4])
        self.assertEqual(children[0][1], {"label": "Open Scan to PDF", "enabled": True, "visible": True})
        self.assertFalse(children[1][1]["enabled"], "Scan disabled while scanning")
        self.assertEqual(children[2][1], {"type": "separator"})

    def test_actions(self):
        self.assertEqual([scanform.tray_action(i) for i in (1, 2, 3, 4, 99)],
                         ["open", "scan", None, "quit", None])


class AutostartTest(unittest.TestCase):
    def test_desktop_exec_quoting(self):
        self.assertEqual(scanform.desktop_exec(["/usr/bin/wsdscan-gui", "--background"]),
                         "/usr/bin/wsdscan-gui --background")
        self.assertEqual(scanform.desktop_exec(["/usr/bin/python3", "/home/a b/x$y.py"]),
                         '/usr/bin/python3 "/home/a b/x\\\\$y.py"')  # \\$ in the key file

    def test_desktop_exec_spec_details(self):
        self.assertEqual(scanform.desktop_exec(["/opt/100%/w"]), "/opt/100%%/w")
        # Inside quotes \\ for a backslash, then doubled again for the key file.
        self.assertEqual(scanform.desktop_exec(["/x\\y z"]), '"/x\\\\\\\\y z"')
        for bad in ("/a\nExec=evil", "/a\rb", "/a\x00b", "/a\u202eb"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                scanform.desktop_exec([bad])

    def test_entry_and_switching(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": d}):
            path = scanform.autostart_path()
            self.assertEqual(path, os.path.join(d, "autostart", f"{scanform.APP_ID}.desktop"))
            self.assertFalse(scanform.autostart_enabled())
            scanform.set_autostart(True, ["/opt/bin/wsdscan-gui"])
            self.assertTrue(scanform.autostart_enabled())
            parser = configparser.ConfigParser(interpolation=None)
            parser.optionxform = str
            parser.read(path)
            entry = parser["Desktop Entry"]
            self.assertEqual(entry["Exec"], "/opt/bin/wsdscan-gui --background")
            self.assertEqual(entry["Icon"], scanform.APP_ID)
            self.assertEqual(entry["X-GNOME-Autostart-enabled"], "true")
            scanform.set_autostart(False)
            self.assertFalse(scanform.autostart_enabled())
            scanform.set_autostart(False)  # idempotent

    def test_launch_command(self):
        with mock.patch("shutil.which", return_value="/usr/local/bin/wsdscan-gui"):
            self.assertEqual(scanform.launch_command(), ["/usr/local/bin/wsdscan-gui"])
        with mock.patch("shutil.which", return_value=None):
            cmd = scanform.launch_command()
        self.assertEqual(cmd[0], sys.executable)
        self.assertTrue(cmd[1].endswith("wsdscan_gui.py"))


class DesktopFilesTest(unittest.TestCase):
    """What desktop-file-validate / appstreamcli would catch, without needing them."""

    DATA = os.path.join(GUI, "data")

    def test_desktop_entry(self):
        path = os.path.join(self.DATA, f"{scanform.APP_ID}.desktop")
        parser = configparser.ConfigParser(interpolation=None)
        parser.optionxform = str
        parser.read(path)
        entry = parser["Desktop Entry"]
        self.assertEqual(entry["Type"], "Application")
        self.assertEqual(entry["Icon"], scanform.APP_ID)
        self.assertEqual(entry["StartupWMClass"], scanform.APP_ID)
        self.assertEqual(entry["Exec"], "wsdscan-gui")  # the installer makes it absolute
        self.assertEqual(entry["Name"], scanform.APP_NAME)
        categories = entry["Categories"].rstrip(";").split(";")
        self.assertIn("Graphics", categories)
        self.assertIn("Scanning", categories)  # additional category, requires Graphics
        self.assertTrue(entry["Keywords"].endswith(";"))

    def test_metainfo(self):
        root = ET.parse(os.path.join(self.DATA, f"{scanform.APP_ID}.metainfo.xml")).getroot()
        self.assertEqual(root.get("type"), "desktop-application")
        self.assertEqual(root.findtext("id"), scanform.APP_ID)
        self.assertEqual(root.findtext("launchable"), f"{scanform.APP_ID}.desktop")
        self.assertEqual(root.findtext("metadata_license"), "CC0-1.0")
        self.assertIn("wsdscan-gui", [b.text for b in root.iter("binary")])
        self.assertEqual(root.find("releases/release").get("version"), scanform.VERSION)
        self.assertIsNotNone(root.find("content_rating"))

    def test_metainfo_screenshots_exist(self):
        root = ET.parse(os.path.join(self.DATA, f"{scanform.APP_ID}.metainfo.xml")).getroot()
        images = [img.text for img in root.iter("image")]
        self.assertEqual(len(images), 3)
        for url in images:
            name = url.rsplit("/", 1)[1]
            path = os.path.join(ROOT, "docs", "screenshots", name)
            with self.subTest(name=name), open(path, "rb") as f:
                self.assertEqual(f.read(8), b"\x89PNG\r\n\x1a\n")

    def test_icon(self):
        root = ET.parse(os.path.join(self.DATA, f"{scanform.APP_ID}.svg")).getroot()
        self.assertEqual(root.get("viewBox"), "0 0 128 128")
        symbolic = ET.parse(os.path.join(self.DATA, f"{scanform.APP_ID}-symbolic.svg")).getroot()
        self.assertEqual(symbolic.get("viewBox"), "0 0 16 16")

    def test_installer_copies_all_modules(self):
        with open(os.path.join(GUI, "install.sh")) as f:
            installer = f.read()
        for name in ("wsdscan_gui.py", "scanform.py", "tray.py", "-symbolic.svg"):
            self.assertGreaterEqual(installer.count(name), 2, name)  # install and --dist


if __name__ == "__main__":
    unittest.main()
