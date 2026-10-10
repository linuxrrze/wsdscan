"""Smoke test of the real GTK window against a fake WSD scanner.

Skipped unless PyGObject with GTK 4.12+ / libadwaita 1.5+ and a display are
available (run it on a desktop, or under `xvfb-run`).
"""

import os
import re
import socket
import sys
import tempfile
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
GUI = os.path.dirname(HERE)
ROOT = os.path.dirname(GUI)
sys.path[:0] = [GUI, ROOT, os.path.join(ROOT, "tests")]
os.environ["LANGUAGE"] = "C"  # English texts, whatever the desktop language
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
from fake_wsd import FakeScanner, make_jpeg, scanned_sheet, test_page  # noqa: E402


def iter_children(widget):
    child = widget.get_first_child()
    while child:
        yield child
        child = child.get_next_sibling()


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

    def test_sane_scanner(self):
        from fake_sane import EPSONDS_OPTIONS, make_scanimage_bin, read_log
        from fake_wsd import JPEG_RGB
        work = tempfile.mkdtemp(prefix="wsdscan-gtk-sane-")
        page = os.path.join(work, "page.jpg")
        with open(page, "wb") as f:
            f.write(JPEG_RGB)
        device = "epsonds:net:192.168.2.13"
        bin_dir = make_scanimage_bin(os.path.join(work, "bin"), pages=[page] * 2,
                                     devices=[[device, "Epson", "ES-580W", "ESC/I-2"]],
                                     options={device: EPSONDS_OPTIONS})
        old_path = os.environ["PATH"]
        os.environ["PATH"] = bin_dir
        try:
            out_dir = tempfile.mkdtemp(prefix="wsdscan-gtk-out-")
            wsdscan.save_config({"scanner": "Office", "outdir": out_dir}, scanners={
                "Office": {"backend": "sane", "device": device, "paper": "auto",
                           "deskew": True}})
            app = wsdscan_gui.ScanApp()
            app.register(None)
            app.activate()
            window = app.window
            wait_for(lambda: window.device is not None)
            self.assertEqual(window.scanner_row.get_subtitle(),
                             f"Epson ES-580W · {device} · Idle")
            hardware = window.settings.hardware
            self.assertTrue(hardware.get_visible() and hardware.get_active())
            self.assertEqual(hardware.get_subtitle(),
                             "The scanner cuts and straightens pages itself")
            window.on_scan()
            wait_for(lambda: window.cancel_event is None)
            self.assertEqual(len(window.tiles), 2)
            scan = [c for c in read_log(bin_dir) if "--batch-print" in c][-1]
            self.assertIn("--adf-crp=yes", scan)
            self.assertIn("--adf-skew=yes", scan)
            self.assertEqual(len(os.listdir(out_dir)), 1)

            prefs = wsdscan_gui.PreferencesDialog(window)
            prefs.present(window)
            page_ = wsdscan_gui.ScannerPage(prefs, "Office")
            self.assertEqual(page_.backend_row.get_value(), "sane")
            self.assertTrue(page_.sane_row.get_visible())
            self.assertFalse(page_.host_row.get_visible())
            page_.sane_options.set_text("--batch=/tmp/x")
            self.assertIn("error", page_.sane_options.get_css_classes())
            self.assertEqual(page_.values()["sane_options"], "", "not saved")
            page_.sane_options.set_text("--adf-justification-x=center")
            self.assertEqual(page_.values()["sane_options"], "--adf-justification-x=center")
            page_.backend_row.set_value("wsd")
            self.assertTrue(page_.host_row.get_visible())
            self.assertFalse(page_.sane_options.get_visible())
            page_.backend_row.set_value("sane")
            wait_for(lambda: page_.device_row.get_subtitle() == f"Epson ES-580W · {device}")
            # Find Scanners lists what SANE finds.
            button = Gtk.Button()
            prefs._find_scanners(button)
            wait_for(lambda: button.get_sensitive(), timeout=30)
            titles = [(row.get_title(), row.get_subtitle()) for row in prefs.found_rows]
            self.assertIn(("Epson ES-580W", f"SANE · {device}"), titles)
            prefs.close()
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

    def test_scanner_page_address_first_no_model_filter(self):
        wsdscan.save_config({"scanner": "A"}, scanners={
            "A": {"host": "192.0.2.1"}, "B": {"model": "ADS"}})
        app = wsdscan_gui.ScanApp()
        app.register(None)
        app.activate()
        window = app.window
        assert window is not None
        prefs = wsdscan_gui.PreferencesDialog(window)
        page = wsdscan_gui.ScannerPage(prefs, "A")
        group = page.host_row.get_parent()
        assert group is not None
        self.assertIs(group.get_first_child(), page.backend_row, "the connection first")
        self.assertIs(page.backend_row.get_next_sibling(), page.host_row, "then the address")
        self.assertIs(page.host_row.get_next_sibling(), page.sane_row, "(or the SANE device)")
        self.assertFalse(page.sane_row.get_visible())
        self.assertIs(page.sane_row.get_next_sibling(), page.name_row, "then the name")
        self.assertFalse(hasattr(page, "model_row"), "no model filter in the app")
        self.assertEqual(page.values()["model"], "")
        # A model filter set in the config file (for the CLI) is kept unchanged.
        page_b = wsdscan_gui.ScannerPage(prefs, "B")
        self.assertEqual(page_b.values()["model"], "ADS")
        page_b.apply()
        prefs.close()
        wait_for(lambda: app.scanners.get("B", {}).get("model") == "ADS")
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

    def test_blank_pages_start_unticked_in_review(self):
        text = make_jpeg(test_page(413, 585, marks=[(100, 300, 200, 10, 20)]))
        blank = make_jpeg(test_page(413, 585))
        with FakeScanner(model="ES-580W", sheets=2, images=[text, blank]) as fake:
            _app, window, out_dir = self.start_app(fake)
            self.assertIn(window.settings.skip_blank, window.settings.rows())
            window.settings.skip_blank.set_active(True)
            self.assertTrue(window.settings.values()["skip_blank"])
            window.settings.review.set_active(True)
            window.on_scan()
            wait_for(lambda: window.review is not None)
            self.assertEqual([tile.kept() for tile in window.tiles], [True, False, True, False])
            self.assertEqual(window.page_count.get_text(), "2 of 4 pages kept")
            window.tiles[3].keep.set_active(True)  # keep this one after all
            window.finish_review(True)
            wait_for(lambda: window.cancel_event is None)
            window.close()
        files = os.listdir(out_dir)
        with open(os.path.join(out_dir, files[0]), "rb") as f:
            self.assertEqual(f.read().count(b"/Type /Page "), 3)

    def test_blank_pages_removed_without_review(self):
        text = make_jpeg(test_page(413, 585, marks=[(100, 300, 200, 10, 20)]))
        blank = make_jpeg(test_page(413, 585))
        with FakeScanner(model="ES-580W", sheets=2, images=[text, blank]) as fake:
            _app, window, out_dir = self.start_app(fake)
            window.settings.skip_blank.set_active(True)
            window.on_scan()
            wait_for(lambda: window.cancel_event is None)
            self.assertEqual(window.page_count.get_text(), "2 of 4 pages kept")
            self.assertIn("removed 2 blank pages", window.last_toast.get_title())
            window.close()
        files = os.listdir(out_dir)
        with open(os.path.join(out_dir, files[0]), "rb") as f:
            self.assertEqual(f.read().count(b"/Type /Page "), 2)

    def sheet_jpeg(self):
        """A tilted A6 sheet on gray backing in a 75 dpi scan of the whole scan area."""
        return make_jpeg(scanned_sheet(638, 1163, (300, 330, 438, 620, 3.5), pad_from=720,
                                       marks=[(40, 60 + 30 * i, 300, 12, 40) for i in range(8)]))

    def test_corrections_switched_in_review(self):
        with FakeScanner(model="ES-580W", sheets=1, images=[self.sheet_jpeg()]) as fake:
            _app, window, out_dir = self.start_app(fake, paper="auto", deskew=True,
                                                   review_pages=True, source="duplex")
            self.assertEqual(window.settings.paper.get_value(), "auto")
            self.assertTrue(window.settings.crop.get_visible())
            self.assertEqual(window.settings.values()["crop"], "sides")
            self.assertTrue(window.settings.values()["deskew"])
            window.on_scan()
            wait_for(lambda: window.review is not None)
            first, second = window.tiles
            self.assertEqual(list(first.correction_buttons), ["crop", "skew"])
            crop = first.correction_buttons["crop"]
            self.assertTrue(crop.get_sensitive() and crop.get_active())
            self.assertRegex(crop.get_tooltip_text(), r"^Cut to the sheet: \d+ × \d+ mm$")
            self.assertEqual(first.correction_buttons["skew"].get_tooltip_text(),
                             "Straightened by 3.5°")
            sheet_ratio = first.paintable.do_get_intrinsic_aspect_ratio()
            self.assertAlmostEqual(sheet_ratio, 438 / 620, delta=0.03, msg="preview shows the sheet")
            # Whole scan area for the second page, as scanned.
            second.correction_buttons["crop"].set_active(False)
            second.correction_buttons["skew"].set_active(False)
            self.assertEqual(second.info["use"], {"crop": False, "skew": False, "rotate": True})
            self.assertAlmostEqual(second.paintable.do_get_intrinsic_aspect_ratio(), 638 / 1163,
                                   delta=0.01)
            self.assertTrue(second.correction_buttons["crop"].get_tooltip_text().endswith("(off)"))
            window.finish_review(True)
            wait_for(lambda: window.cancel_event is None)
            self.assertFalse(crop.get_sensitive(), "saved: shown, but no longer switchable")
            window.close()
        with open(os.path.join(out_dir, os.listdir(out_dir)[0]), "rb") as f:
            boxes = [tuple(float(v) for v in m.split()) for m in
                     re.findall(rb"/MediaBox \[0 0 ([\d. ]+)\]", f.read())]
        self.assertAlmostEqual(boxes[0][0], 438 * 72 / 300, delta=3)
        self.assertEqual(boxes[1], (638 * 72 / 300, 1163 * 72 / 300))

    def test_page_adjusted_in_editor(self):
        with FakeScanner(model="ES-580W", sheets=1, images=[self.sheet_jpeg()]) as fake:
            _app, window, out_dir = self.start_app(fake, paper="auto", deskew=True,
                                                   review_pages=True, source="adf")
            window.on_scan()
            wait_for(lambda: window.review is not None)
            tile = window.tiles[0]
            self.assertIn("Double-click", tile.picture.get_tooltip_text())
            editor = tile.open_editor()
            view = editor.view
            wait_for(lambda: view.get_width() > 0)
            # Everything fits the window's narrowest width (360).
            self.assertLessEqual(editor.get_child().measure(Gtk.Orientation.HORIZONTAL, -1)[0],
                                 360)
            self.assertAlmostEqual(editor.angle.get_value(), 3.5, delta=0.05, msg="prefilled")
            self.assertEqual(editor.state, wsdscan.manual_geometry(tile.info))
            # The red frame's right edge is where the page ends, and can be grabbed.
            x0, y0, x1, y1 = view.view_box()
            self.assertEqual(view.hit(x1, (y0 + y1) / 2), "r")
            self.assertEqual(view.hit(x0 + 1, y0), "lt")
            self.assertEqual(view.hit((x0 + x1) / 2, (y0 + y1) / 2), "move")
            self.assertIsNone(view.hit(1, 1))
            u0, v0, u1, v1 = view.box()
            view.start_box = view.box()
            view.drag("r", -100, 0)  # frame pixels = scan pixels
            view.start_box = view.box()
            view.drag("b", 0, -50)
            self.assertAlmostEqual(view.box()[2], u1 - 100)
            self.assertAlmostEqual(view.box()[3], v1 - 50)
            view.start_box = view.box()
            view.drag("l", -10000, 0)
            self.assertAlmostEqual(view.box()[0], view.bounds()[0], msg="not beyond the scan")
            editor.angle.set_value(1.0)
            self.assertEqual(editor.state[4], 1.0)
            _cx, _cy, w, h, _angle = editor.state
            editor.apply()
            self.assertEqual(tile.info["manual"], editor.state)
            self.assertEqual(list(tile.correction_buttons), ["manual"])
            self.assertIn("straightened by 1.0°",
                          tile.correction_buttons["manual"].get_tooltip_text())
            self.assertAlmostEqual(tile.paintable.do_get_intrinsic_aspect_ratio(), w / h)
            # Reset in a second visit: back to the automatic corrections.
            again = tile.open_editor()
            again.reset()
            self.assertAlmostEqual(again.angle.get_value(), 3.5, delta=0.05)
            again.turn_by(90)
            again.apply()
            self.assertNotIn("manual", tile.info)
            self.assertEqual(tile.info["turn"], 90)
            self.assertEqual(list(tile.correction_buttons), ["crop", "skew"])
            # Adjusted once more, and saved like that.
            last = tile.open_editor()
            last.angle.set_value(-2.0)
            last.apply()
            page_w, page_h = wsdscan.page_layout(tile.info)[:2]
            window.finish_review(True)
            wait_for(lambda: window.cancel_event is None)
            self.assertIsNone(tile.open_editor(), "saved: no longer editable")
            window.close()
        with open(os.path.join(out_dir, os.listdir(out_dir)[0]), "rb") as f:
            boxes = [tuple(float(v) for v in m.split()) for m in
                     re.findall(rb"/MediaBox \[0 0 ([\d. ]+)\]", f.read())]
        self.assertAlmostEqual(boxes[0][0], page_w * 72 / 300, delta=0.01)
        self.assertAlmostEqual(boxes[0][1], page_h * 72 / 300, delta=0.01)
        self.assertGreater(boxes[0][0], boxes[0][1], "turned right")

    def test_pages_turned_by_hand_in_review(self):
        with FakeScanner(model="ES-580W", sheets=1) as fake:
            _app, window, out_dir = self.start_app(fake, review_pages=True, source="duplex")
            window.on_scan()
            wait_for(lambda: window.review is not None)
            first, second = window.tiles
            self.assertTrue(first.turn_buttons.get_visible())
            left, right = list(iter_children(first.turn_buttons))
            self.assertEqual((left.get_tooltip_text(), right.get_tooltip_text()),
                             ("Turn left", "Turn right"))
            upright = first.paintable.do_get_intrinsic_aspect_ratio()
            right.emit("clicked")
            self.assertEqual(first.info["turn"], 90)
            self.assertAlmostEqual(first.paintable.do_get_intrinsic_aspect_ratio(), 1 / upright)
            second.turn(-90)
            second.turn(-90)
            self.assertEqual(second.info["turn"], 180)
            window.finish_review(True)
            wait_for(lambda: window.cancel_event is None)
            self.assertFalse(first.turn_buttons.get_visible(), "saved: no longer turnable")
            window.close()
        with open(os.path.join(out_dir, os.listdir(out_dir)[0]), "rb") as f:
            data = f.read()
        boxes = [tuple(float(v) for v in m.split()) for m in
                 re.findall(rb"/MediaBox \[0 0 ([\d. ]+)\]", data)]
        self.assertEqual(boxes[1], (boxes[0][1], boxes[0][0]), "the first page turned sideways")
        self.assertRegex(data, rb"q 0 -[\d.]+ [\d.]+ 0 0 [\d.]+ cm /Im0", "turned right")
        self.assertRegex(data, rb"q -[\d.]+ 0 0 -[\d.]+ [\d.]+ [\d.]+ cm /Im0", "upside down")

    def test_corrections_shown_without_review(self):
        with FakeScanner(model="ES-580W", sheets=1, images=[self.sheet_jpeg()]) as fake:
            _app, window, _out = self.start_app(fake, paper="auto", source="adf")
            window.on_scan()
            wait_for(lambda: window.cancel_event is None)
            tile = window.tiles[0]
            self.assertEqual(list(tile.correction_buttons), ["crop"], "not straightened: off")
            self.assertFalse(tile.correction_buttons["crop"].get_sensitive())
            self.assertFalse(tile.turn_buttons.get_visible(), "turning needs the review")
            window.close()

    def test_turn_upright_needs_orientation_data(self):
        Adw.init()
        values = dict(wsdscan.CONFIG_DEFAULTS, auto_rotate=True)
        without = wsdscan_gui.ScanSettings(values, scanform.Choices.unknown(),
                                           scanform.OcrStatus(["tesseract"], ["eng"], osd=False))
        self.assertFalse(without.auto_rotate.get_sensitive())
        self.assertFalse(without.values()["auto_rotate"])
        self.assertIn("tesseract-ocr-osd", without.auto_rotate.get_subtitle())
        with_osd = wsdscan_gui.ScanSettings(values, scanform.Choices.unknown(),
                                            scanform.OcrStatus(["tesseract"], ["eng"], osd=True))
        self.assertTrue(with_osd.auto_rotate.get_sensitive())
        self.assertTrue(with_osd.values()["auto_rotate"])
        self.assertIn(with_osd.auto_rotate, with_osd.rows())

    def test_name_and_folder_changed_before_saving(self):
        with FakeScanner(model="ES-580W", sheets=1) as fake:
            _app, window, out_dir = self.start_app(fake)
            window.settings.review.set_active(True)
            window.on_scan()
            wait_for(lambda: window.review is not None)
            # Editable while the scan waits for the review decision.
            self.assertTrue(window.name_row.is_sensitive())
            self.assertTrue(window.folder_row.is_sensitive())
            self.assertFalse(window.settings_row.is_sensitive(), "scan settings stay locked")
            other = tempfile.mkdtemp(prefix="wsdscan-gtk-other-")
            new_folder = os.path.join(other, "Invoices")  # created when saving
            window.name_row.set_text("ACME invoice")
            window.folder_row.set_folder(new_folder)
            window.name_row.emit("entry-activated")  # Enter saves during the review
            wait_for(lambda: window.cancel_event is None)
            window.close()
        self.assertEqual(os.listdir(new_folder), ["ACME invoice.pdf"])
        self.assertEqual(os.listdir(out_dir), [])

    def test_empty_name_falls_back_to_default(self):
        with FakeScanner(model="ES-580W", sheets=1) as fake:
            _app, window, out_dir = self.start_app(fake, filename="Fallback {date}")
            window.settings.review.set_active(True)
            window.on_scan()
            wait_for(lambda: window.review is not None)
            window.name_row.set_text("   ")
            window.finish_review(True)
            wait_for(lambda: window.cancel_event is None)
            window.close()
        files = os.listdir(out_dir)
        self.assertEqual(len(files), 1)
        self.assertTrue(files[0].startswith("Fallback "))

    def test_scan_more_pages_in_review(self):
        with FakeScanner(model="ES-580W", sheets=2) as fake:
            _app, window, out_dir = self.start_app(fake)
            window.settings.review.set_active(True)
            window.on_scan()
            wait_for(lambda: window.review is not None)
            self.assertTrue(window.scan_button.get_visible())
            self.assertTrue(window.actions["scan"].get_enabled())
            window.tiles[1].keep.set_active(False)
            fake.config["sheets"] = 1
            window.actions["scan"].activate(None)  # Scan again: more pages, same document
            self.assertIsNone(window.review)
            self.assertFalse(window.scan_button.get_visible())
            wait_for(lambda: window.review is not None)
            self.assertEqual([tile.number for tile in window.tiles], [1, 2, 3, 4, 5, 6])
            self.assertEqual(window.page_count.get_text(), "5 of 6 pages kept")
            fake.config["sheets"] = 0  # an empty feeder: the pages so far stay
            with mock.patch.object(window, "show_error") as error:
                window.on_scan()
                wait_for(lambda: window.review is not None)
            error.assert_called_once()
            self.assertEqual(len(window.tiles), 6)
            self.assertEqual(os.listdir(out_dir), [], "nothing saved before Save")
            window.finish_review(True)
            wait_for(lambda: window.cancel_event is None)
            self.assertTrue(window.scan_button.has_css_class("suggested-action"))
            window.close()
        files = os.listdir(out_dir)
        with open(os.path.join(out_dir, files[0]), "rb") as f:
            self.assertEqual(f.read().count(b"/Type /Page "), 5)

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

    def test_scanner_found_later(self):
        """Started before the scanner (or network) was reachable: the window
        keeps looking, and a late failure of an older attempt is ignored."""
        import threading
        from unittest import mock
        with FakeScanner(model="ES-580W") as fake:
            wsdscan.save_config({"host": fake.host})
            real_find = wsdscan.find_scanner
            calls = []
            release = threading.Event()

            def find(host, model=None):
                calls.append(host)
                if len(calls) == 1:
                    raise wsdscan.ScanError("no WSD scanner answered")
                if len(calls) == 3:
                    release.wait(10)  # a slow attempt that fails after a newer one
                    raise wsdscan.ScanError("timed out")
                return real_find(host, model)

            with mock.patch.object(wsdscan, "find_scanner", find), \
                    mock.patch.object(wsdscan_gui.MainWindow, "RETRY_SECONDS", 1):
                app = wsdscan_gui.ScanApp()
                app.register(None)
                app.activate()
                window = app.window
                assert window is not None
                wait_for(lambda: window.banner.get_revealed())
                wait_for(lambda: window.device is not None)  # retried by itself
                self.assertFalse(window.banner.get_revealed())
                self.assertEqual(window.title.get_subtitle(), window.scanner_row.get_title())

                window.connect_scanner()   # attempt 3: hangs, then fails
                window.connect_scanner()   # attempt 4: finds the scanner
                wait_for(lambda: window.device is not None)
                release.set()
                wait_for(lambda: len(calls) >= 4)
                deadline = time.monotonic() + 0.5
                while time.monotonic() < deadline:
                    GLib.MainContext.default().iteration(False)
                    time.sleep(0.01)
                self.assertIsNotNone(window.device)
                self.assertFalse(window.banner.get_revealed(), "stale failure ignored")
                window.close()

    def test_banner_goes_when_scanner_is_switched_on(self):
        from unittest import mock
        free = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        free.bind(("127.0.0.1", 0))
        port = free.getsockname()[1]
        free.close()
        wsdscan.save_config({"scanner": "Office"},
                            scanners={"Office": {"host": f"127.0.0.1:{port}"}})
        with mock.patch.object(wsdscan_gui.MainWindow, "RETRY_SECONDS", 1):
            app = wsdscan_gui.ScanApp()
            app.register(None)
            app.activate()
            window = app.window
            assert window is not None
            wait_for(lambda: window.banner.get_revealed())
            self.assertEqual(window.title.get_subtitle(), "Scanner not reachable")
            with FakeScanner(model="ES-580W", udp_port=port):  # switched on
                wait_for(lambda: window.device is not None)
                self.assertFalse(window.banner.get_revealed())
                self.assertEqual(window.scanner_row.get_title(), "Office")
                self.assertIn("Idle", window.scanner_row.get_subtitle())
            window.close()

    def test_not_set_up_and_not_reachable_differ(self):
        from unittest import mock
        not_found = wsdscan.ScannerNotFound("no WSD scanner answered")
        with mock.patch.object(wsdscan, "find_scanner", side_effect=not_found):
            app = wsdscan_gui.ScanApp()
            app.register(None)
            app.activate()
            window = app.window
            assert window is not None
            wait_for(lambda: window.banner.get_revealed())
            self.assertEqual(window.title.get_subtitle(), "No scanner set up")
            self.assertEqual(window.banner.get_button_label(), "Add Scanner")
            self.assertEqual(window.banner.get_action_name(), "app.preferences")

            wsdscan.save_config({"scanner": "Office"}, scanners={"Office": {"host": "192.0.2.1"}})
            app.select_scanner("Office")
            wait_for(lambda: window.title.get_subtitle() == "Scanner not reachable")
            self.assertEqual(window.scanner_row.get_title(), "Office")
            self.assertEqual(window.banner.get_button_label(), "Retry")
            self.assertEqual(window.banner.get_action_name(), "win.reconnect")
            self.assertTrue(window.banner.get_revealed())
            window.close()

    def test_status_bar_gone_while_locked(self):
        """GNOME removes the status bar host while the screen is locked: the
        hidden window opens only if the host is still missing after unlock."""
        from types import SimpleNamespace
        from unittest import mock
        app = wsdscan_gui.ScanApp()
        app.register(None)
        app.activate()
        window = app.window
        assert window is not None
        window.set_visible(False)  # closed to the status bar
        app.tray = SimpleNamespace(available=False)
        app.session_lock = SimpleNamespace(locked=False)
        timers = []
        with mock.patch.object(wsdscan_gui.GLib, "timeout_add_seconds",
                               lambda _s, callback: timers.append(callback)):
            app._tray_available(False)
            app.session_lock.locked = True  # reported just after the host vanished
            timers.pop()()
            self.assertFalse(window.is_visible(), "not opened while locked")
            app.session_lock.locked = False
            app._session_lock_changed(False)
            app.tray.available = True  # the host is back after unlock
            timers.pop()()
            self.assertFalse(window.is_visible())

            app.tray.available = False  # no host after unlock: never invisible
            app._session_lock_changed(False)
            timers.pop()()
            self.assertTrue(window.is_visible())
        app.tray = app.session_lock = None
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
