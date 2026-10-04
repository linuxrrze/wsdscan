"""Smoke test of the real GTK window against a fake WSD scanner.

Skipped unless PyGObject with GTK 4.12+ / libadwaita 1.5+ and a display are
available (run it on a desktop, or under `xvfb-run`).
"""

import os
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
GUI = os.path.dirname(HERE)
ROOT = os.path.dirname(GUI)
sys.path[:0] = [GUI, ROOT, os.path.join(ROOT, "tests")]
CONFIG_DIR = tempfile.mkdtemp(prefix="wsdscan-gtk-test-")
os.environ["WSDSCAN_CONFIG"] = os.path.join(CONFIG_DIR, "config.ini")

try:
    import gi

    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Adw, GLib, Gtk

    GTK_OK = (Gtk.init_check() and (Gtk.get_major_version(), Gtk.get_minor_version()) >= (4, 12)
              and (Adw.get_major_version(), Adw.get_minor_version()) >= (1, 5))
except (ImportError, ValueError):
    GTK_OK = False

if GTK_OK:
    import wsdscan_gui

import scanform  # noqa: E402
import wsdscan  # noqa: E402
from fake_wsd import FakeScanner  # noqa: E402


def wait_for(check, timeout=20.0):
    context = GLib.MainContext.default()
    deadline = time.monotonic() + timeout
    while not check():
        if time.monotonic() > deadline:
            raise AssertionError("timed out")
        context.iteration(False)
        time.sleep(0.01)


@unittest.skipUnless(GTK_OK, "needs GTK 4.12+, libadwaita 1.5+ and a display")
class GtkTest(unittest.TestCase):
    def setUp(self):
        # Every test starts with an empty config file: scanners saved by one
        # test must not leak into the next.
        if os.path.exists(os.environ["WSDSCAN_CONFIG"]):
            os.remove(os.environ["WSDSCAN_CONFIG"])

    def test_scan_settings_rows(self):
        Adw.init()
        settings = wsdscan_gui.ScanSettings(dict(wsdscan.CONFIG_DEFAULTS),
                                            scanform.Choices.unknown(),
                                            scanform.OcrStatus(["tesseract"], ["eng"]))
        settings.apply_choices(scanform.Choices.from_capabilities({
            "formats": ["exif", "tiff-single-uncompressed"],
            "colors": ["RGB24", "Grayscale8", "BlackAndWhite1"], "resolutions": [100, 300],
            "duplex": True, "device_settings": {"brightness": True, "contrast": True}}))
        self.assertEqual(settings.resolution.values, [100, 300])
        settings.mode.set_value("bw")
        self.assertTrue(settings.lossless.get_active())
        self.assertFalse(settings.lossless.get_sensitive())
        settings.mode.set_value("color")
        self.assertFalse(settings.lossless.get_active(), "user choice restored")
        tiff_only = scanform.Choices.from_capabilities({"formats": ["tiff-single-uncompressed"]})
        settings.apply_choices(tiff_only)
        self.assertFalse(settings.values()["lossless"], "saved preference unchanged")
        self.assertTrue(settings.scan_values()["lossless"], "scanner forces lossless")
        settings.exposure.set_enable_expansion(True)
        settings.brightness.set_value(-250)
        self.assertEqual(settings.values()["brightness"], -250)
        self.assertEqual(settings.values()["contrast"], 0)
        settings.ocr.set_active(True)
        self.assertTrue(settings.scan_values()["ocr"])
        self.assertEqual(settings.ocr.get_subtitle(), "Tesseract · eng")

    def test_open_file_only_for_saved_files(self):
        from unittest import mock
        app = wsdscan_gui.ScanApp()
        with mock.patch.object(wsdscan_gui.Gtk, "FileLauncher") as launcher:
            app.open_file("/etc/passwd")  # e.g. via D-Bus from another program
            launcher.assert_not_called()
            app.saved_files.add("/home/x/Scan.pdf")
            app.open_file("/home/x/Scan.pdf")
            launcher.assert_called_once()

    def test_thumbnail_of_crafted_image(self):
        from fake_wsd import JPEG_RGB
        self.assertIsNotNone(wsdscan_gui.make_thumbnail(JPEG_RGB))
        bad = bytearray(JPEG_RGB)
        sof = bad.index(b"\xff\xc0")
        bad[sof + 5:sof + 9] = (40000).to_bytes(2, "big") * 2  # claims 40000 x 40000
        self.assertIsNone(wsdscan_gui.make_thumbnail(bytes(bad)))
        self.assertIsNone(wsdscan_gui.make_thumbnail(b"not an image"))

    def test_ocr_switch_disabled_without_engines(self):
        Adw.init()
        settings = wsdscan_gui.ScanSettings(dict(wsdscan.CONFIG_DEFAULTS, ocr=True),
                                            scanform.Choices.unknown(), scanform.OcrStatus([], []))
        self.assertFalse(settings.ocr.get_sensitive())
        self.assertFalse(settings.ocr.get_active(), "configured, but not installed")
        self.assertIn("Install OCRmyPDF or Tesseract", settings.ocr.get_subtitle() or "")

    def test_ocr_languages_per_scanner(self):
        from fake_ocr import make_ocr_bin
        bin_dir = make_ocr_bin(tempfile.mkdtemp(prefix="wsdscan-gtk-ocr-"), ["tesseract"])
        old_path = os.environ["PATH"]
        os.environ["PATH"] = bin_dir
        try:
            wsdscan.save_config({"scanner": "A"}, scanners={"A": {"host": "192.0.2.1"},
                                                            "B": {"host": "192.0.2.1"}})
            app = wsdscan_gui.ScanApp()
            app.register(None)
            app.activate()
            window = app.window
            assert window is not None
            prefs = wsdscan_gui.PreferencesDialog(window)
            prefs.present(window)
            page = wsdscan_gui.ScannerPage(prefs, "B")
            self.assertTrue(page.ocr_rows.auto.get_active())
            self.assertFalse(page.ocr_rows.lang_rows["deu"].get_sensitive())
            page.ocr_rows.auto.set_active(False)
            self.assertTrue(page.ocr_rows.lang_rows["deu"].get_sensitive())
            for code, row in page.ocr_rows.lang_rows.items():
                row.set_active(code == "deu")
            page.apply()
            prefs.close()
            wait_for(lambda: "B" in app.scanners and "ocr_lang" in app.scanners["B"])
            self.assertEqual(wsdscan.load_config(scanner="B")["ocr_lang"], "deu")
            self.assertEqual(wsdscan.load_config(scanner="A")["ocr_lang"], "", "per scanner")
            window.close()
        finally:
            os.environ["PATH"] = old_path

    def test_scanner_page_settings_and_name(self):
        with FakeScanner(model="ES-580W", formats=["exif", "tiff-single-uncompressed"],
                         resolutions=[100, 300]) as fake:
            wsdscan.save_config({"scanner": ""}, scanners={})
            app = wsdscan_gui.ScanApp()
            app.register(None)
            app.activate()
            window = app.window
            assert window is not None
            prefs = wsdscan_gui.PreferencesDialog(window)
            prefs.present(window)
            page = wsdscan_gui.ScannerPage(prefs, None)  # "Add Scanner"
            page.LOOKUP_DELAY_MS = 10
            page.host_row.set_text(fake.host)
            # The device answers: name proposed, choices narrowed to the scanner.
            wait_for(lambda: page.name_row.get_text() == "EPSON ES-580W")
            self.assertEqual(page.settings.resolution.values, [100, 300])
            page.settings.mode.set_value("bw")
            page.apply()
            self.assertEqual(list(prefs.profiles), ["EPSON ES-580W"])
            self.assertEqual(prefs.profiles["EPSON ES-580W"]["mode"], "bw")
            # Same device again with other settings, via Duplicate.
            page = wsdscan_gui.ScannerPage(prefs, "EPSON ES-580W")
            prefs.push_subpage(page)
            page._duplicate()
            self.assertEqual(list(prefs.profiles), ["EPSON ES-580W", "EPSON ES-580W 2"])
            prefs.close()
            wait_for(lambda: len(app.scanners) == 2)
            self.assertEqual(wsdscan.load_config(scanner="EPSON ES-580W 2")["mode"], "bw")
            window.close()

    def test_add_scanner_by_address(self):
        """Regression: adding by IP address was only a small unlabeled "+" icon."""
        with FakeScanner(model="ES-580W") as fake:
            wsdscan.save_config({"scanner": ""}, scanners={})
            app = wsdscan_gui.ScanApp()
            app.register(None)
            app.activate()
            window = app.window
            assert window is not None
            prefs = wsdscan_gui.PreferencesDialog(window)
            prefs.present(window)
            self.assertEqual(prefs.add_row.get_title(), "Add Scanner by Address…")
            self.assertIs(prefs.scanner_rows[-1], prefs.add_row, "last row of the list")
            prefs.add_row.emit("activated")
            page = prefs.open_page
            assert page is not None
            self.assertEqual(page.get_title(), "New Scanner")
            self.assertEqual(page.host_row.get_title(), "IP address or host name")
            page.LOOKUP_DELAY_MS = 10
            page.host_row.set_text(fake.host)
            wait_for(lambda: page.name_row.get_text() == "EPSON ES-580W")
            prefs.close()
            wait_for(lambda: list(app.scanners) == ["EPSON ES-580W"])
            self.assertEqual(app.scanners["EPSON ES-580W"]["host"], fake.host)
            window.close()

    def test_typed_name_is_kept(self):
        with FakeScanner(model="ES-580W") as fake:
            wsdscan.save_config({"scanner": ""}, scanners={})
            app = wsdscan_gui.ScanApp()
            app.register(None)
            app.activate()
            window = app.window
            assert window is not None
            prefs = wsdscan_gui.PreferencesDialog(window)
            page = wsdscan_gui.ScannerPage(prefs, None)
            page.LOOKUP_DELAY_MS = 10
            page.name_row.set_text("Office")
            page.host_row.set_text(fake.host)
            wait_for(lambda: "EPSON" in (page.device_row.get_subtitle() or ""))
            self.assertEqual(page.name_row.get_text(), "Office")
            window.close()

    def test_multiple_scanners(self):
        with FakeScanner(model="ES-580W") as office, \
                FakeScanner(model="ADS-1700W", manufacturer="Brother",
                            formats=["jfif", "tiff-single-uncompressed"]) as home:  # b/w needs TIFF
            wsdscan.save_config({"scanner": "Office"}, scanners={
                "Office": {"host": office.host}, "Home": {"host": home.host, "mode": "bw"}})
            app = wsdscan_gui.ScanApp()
            app.register(None)
            app.activate()
            window = app.window
            assert window is not None
            wait_for(lambda: window.device is not None)
            self.assertTrue(window.scanner_choice.get_visible())
            self.assertEqual(window.scanner_choice.values, ["Office", "Home"])
            # Same name as in the preferences; the device in the subtitle.
            self.assertEqual(window.scanner_row.get_title(), "Office")
            self.assertTrue((window.scanner_row.get_subtitle() or "").startswith("EPSON ES-580W · "))
            window.scanner_choice.set_value("Home")
            wait_for(lambda: window.device is not None and "ADS" in window.device["model"])
            self.assertEqual(window.settings.mode.get_value(), "bw", "the scanner's own default")
            # Regression: "&" was parsed as markup, so the summary stayed empty.
            self.assertIn("Black & white", window.settings_row.get_subtitle() or "")
            self.assertEqual(scanform.load_gui_config()["last_scanner"], "Home")
            window.close()
        # Next start: the scanner used last.
        self.assertEqual(wsdscan.load_config(scanner=scanform.load_gui_config()["last_scanner"])
                         ["mode"], "bw")

    def test_preferences_scanner_list(self):
        wsdscan.save_config({"host": "192.0.2.7", "scanner": ""}, scanners={})
        app = wsdscan_gui.ScanApp()
        app.register(None)
        app.activate()
        window = app.window
        assert window is not None
        prefs = wsdscan_gui.PreferencesDialog(window)
        prefs.present(window)
        self.assertEqual(list(prefs.profiles), ["Scanner"])
        self.assertEqual(prefs.profiles["Scanner"]["host"], "192.0.2.7")
        prefs._add_found(Gtk.Button(), {"manufacturer": "Brother", "model": "ADS-1700W",
                                         "device_url": "http://192.0.2.8:80/WSD/DEVICE"})
        self.assertEqual(list(prefs.profiles), ["Scanner", "Brother ADS-1700W"])
        self.assertEqual(len([r for r in prefs.scanner_rows if r is not prefs.add_row]), 2)
        prefs.default_name = "Brother ADS-1700W"
        prefs.close()
        wait_for(lambda: list(app.scanners) == ["Scanner", "Brother ADS-1700W"])
        shared = wsdscan.load_config(apply_profile=False)
        self.assertEqual((shared["host"], shared["scanner"]), ("", "Brother ADS-1700W"))
        window.close()

    def test_scan_settings_dialog(self):
        with FakeScanner(model="ES-580W", formats=["exif", "tiff-single-uncompressed"],
                         resolutions=[100, 300]) as fake:
            wsdscan.save_config({"host": fake.host})
            app = wsdscan_gui.ScanApp()
            app.register(None)
            app.activate()
            window = app.window
            assert window is not None
            wait_for(lambda: window.device is not None)
            self.assertEqual(window.settings_row.get_subtitle(), "Both sides · Color · 300 dpi · A4")
            self.assertIsNone(window.settings.mode.get_parent(), "rows only live in the dialog")
            for _round in range(2):  # the dialog can be opened again
                dialog = wsdscan_gui.ScanSettingsDialog(window)
                dialog.present(window)
                self.assertIsNotNone(window.settings.mode.get_parent())
                window.settings.mode.set_value("bw")
                window.settings.resolution.set_value(100)
                dialog.close()
                wait_for(lambda: window.settings.mode.get_parent() is None)
            self.assertEqual(window.settings_row.get_subtitle(),
                             "Both sides · Black & white · 100 dpi · A4")
            self.assertEqual(app.config["mode"], "color", "not saved as default")
            dialog = wsdscan_gui.ScanSettingsDialog(window)
            dialog.present(window)
            dialog._save_defaults()
            self.assertEqual(wsdscan.load_config()["mode"], "bw")
            dialog.close()
            window.close()

    def start_app(self, fake, **config):
        out_dir = tempfile.mkdtemp(prefix="wsdscan-gtk-out-")
        wsdscan.save_config(dict({"host": fake.host, "outdir": out_dir}, **config))
        app = wsdscan_gui.ScanApp()
        app.register(None)
        app.activate()
        window = app.window
        assert window is not None
        wait_for(lambda: window.device is not None)
        return app, window, out_dir

    def test_page_preview(self):
        with FakeScanner(model="ES-580W", sheets=2) as fake:
            _app, window, _out = self.start_app(fake)
            window.on_scan()
            wait_for(lambda: window.cancel_event is None)
            self.assertEqual(len(window.tiles), 4)
            self.assertTrue(window.pages_group.get_visible())
            self.assertEqual(window.page_count.get_text(), "4 pages")
            self.assertIsNotNone(window.tiles[0].picture.get_paintable(), "thumbnail decoded")
            window.close()

    def test_review_removes_pages_before_saving(self):
        with FakeScanner(model="ES-580W", sheets=2) as fake:
            _app, window, out_dir = self.start_app(fake)
            window.settings.review.set_active(True)
            window.on_scan()
            wait_for(lambda: window.review is not None)
            self.assertTrue(window.review_bar.get_visible())
            window.tiles[1].keep.set_active(False)
            window.tiles[2].keep.set_active(False)
            self.assertEqual(window.page_count.get_text(), "2 of 4 pages kept")
            window.finish_review(True)
            wait_for(lambda: window.cancel_event is None)
            window.close()
        files = os.listdir(out_dir)
        with open(os.path.join(out_dir, files[0]), "rb") as f:
            self.assertEqual(f.read().count(b"/Type /Page "), 2)

    def test_review_discard_saves_nothing(self):
        with FakeScanner(model="ES-580W", sheets=1) as fake:
            _app, window, out_dir = self.start_app(fake)
            window.settings.review.set_active(True)
            window.on_scan()
            wait_for(lambda: window.review is not None)
            window.finish_review(False)
            wait_for(lambda: window.cancel_event is None)
            window.close()
        self.assertEqual(os.listdir(out_dir), [])

    def test_saving_preferences_reconnects_and_hides_banner(self):
        with FakeScanner(model="ES-580W") as fake:
            closed = FakeScanner()
            closed.start()
            unreachable = closed.host
            closed.stop()
            wsdscan.save_config({"host": unreachable})
            app = wsdscan_gui.ScanApp()
            app.register(None)
            app.activate()
            window = app.window
            assert window is not None
            wait_for(lambda: window.banner.get_revealed())
            self.assertIsNone(window.device)

            prefs = wsdscan_gui.PreferencesDialog(window)
            prefs.present(window)
            # The old [scan] host became the first scanner; point it at the fake.
            name = next(iter(prefs.profiles))
            self.assertTrue(prefs.migrated)
            prefs.profiles[name]["host"] = fake.host
            prefs.close()  # saves on "closed"
            wait_for(lambda: window.device is not None)
            self.assertFalse(window.banner.get_revealed())
            self.assertEqual(app.config["host"], fake.host)
            window.close()

    def test_window_scans_into_folder(self):
        out_dir = tempfile.mkdtemp(prefix="wsdscan-gtk-out-")
        with FakeScanner(model="ES-580W", formats=["exif", "tiff-single-uncompressed"],
                         resolutions=[100, 300], sheets=2) as fake:
            wsdscan.save_config({"host": fake.host, "outdir": out_dir, "mode": "bw",
                                 "filename": "Test {date}"})
            app = wsdscan_gui.ScanApp()
            app.register(None)
            app.activate()
            window = app.window
            assert window is not None
            wait_for(lambda: window.device is not None)
            self.assertEqual(window.scanner_row.get_title(), "EPSON ES-580W")
            self.assertEqual(window.settings.resolution.values, [100, 300])
            self.assertTrue(window.name_row.get_text().startswith("Test "))
            window.on_scan()
            wait_for(lambda: window.cancel_event is None)
            window.close()
        files = os.listdir(out_dir)
        self.assertEqual(len(files), 1)
        with open(os.path.join(out_dir, files[0]), "rb") as f:
            data = f.read()
        self.assertEqual(data.count(b"/Type /Page "), 4)
        self.assertIn(b"/BitsPerComponent 1 ", data)
        self.assertIn("<wscn:Format>tiff-single-uncompressed</wscn:Format>", fake.tickets[0])


if __name__ == "__main__":
    unittest.main()
