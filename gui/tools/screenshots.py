#!/usr/bin/env python3
"""Make the README screenshots of Scan to PDF.

Drives the real app against the fake WSD scanner from tests/ (with
document-like pages rendered by GTK) and fake OCR tools, and saves PNGs of
the window. Run from the repository root with GTK 4 / libadwaita available:

    python3 gui/tools/screenshots.py docs/screenshots

Uses its own temporary config file and home folder; your settings are not
touched. For realistic pictures the fake scanner (listening on 127.0.0.1)
is shown under the LAN address 192.168.2.13: only the displayed address is
replaced; the app, the protocol and its checks are real.
"""

import os
import struct
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
GUI = os.path.dirname(HERE)
ROOT = os.path.dirname(GUI)
sys.path[:0] = [GUI, ROOT, os.path.join(ROOT, "tests")]
WORK = tempfile.mkdtemp(prefix="wsdscan-screenshots-")
os.environ["WSDSCAN_CONFIG"] = os.path.join(WORK, "config.ini")
os.environ["XDG_CONFIG_HOME"] = os.path.join(WORK, "config")
os.environ["HOME"] = os.path.join(WORK, "home")
SHOWN_HOST = "192.168.2.13"
os.environ.setdefault("LANG", "en_US.UTF-8")

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gsk", "4.0")
gi.require_version("PangoCairo", "1.0")
from gi.repository import Adw, Gdk, GLib, Graphene, Gsk, Gtk, Pango, PangoCairo  # noqa: E402

import fake_ocr  # noqa: E402
import fake_wsd  # noqa: E402

PAGE_W, PAGE_H = 620, 877  # A4 proportions, enough for sharp thumbnails

DOCUMENTS = [
    ("Example Corp. · Main Street 1 · 12345 Example Town", "Invoice No. 2026-0815",
     ["Date: 2026-10-04", "", "Item                       Qty     Price",
      "Network scanner service      1    120.00", "Document feeder rollers      2     38.50",
      "Cleaning kit                 1     12.90", "", "Total                              209.90",
      "", "Thank you for your order."]),
    None,  # blank back of the invoice
    ("City Library", "Reminder",
     ["Dear reader,", "", "the following book is due:", "",
      "  “Scanning for Beginners”, returned by 2026-10-18", "",
      "You can renew it online or at the front desk.", "", "Kind regards,",
      "Your City Library"]),
    ("Health Insurance", "Annual statement 2026",
     ["Policy holder: Jane Example", "Policy number: 0042-1337", "",
      "Contributions this year            1,284.00", "Reimbursed costs                     312.40",
      "", "Please keep this statement for your tax return."]),
]


def render_page(doc):
    """A document-like page as raw RGB pixels (rendered with GTK/Pango)."""
    snapshot = Gtk.Snapshot()
    rect = Graphene.Rect().init(0, 0, PAGE_W, PAGE_H)
    white = Gdk.RGBA()
    white.parse("#fdfdfb")
    snapshot.append_color(white, rect)
    if doc:
        header, title, lines = doc
        context = Pango.FontMap.create_context(PangoCairo.FontMap.get_default())
        ink = Gdk.RGBA()
        ink.parse("#222222")
        y = 50
        for text, font, gap in ([(header, "Sans 11", 60), (title, "Sans Bold 22", 70)]
                                + [(line, "Monospace 12", 26) for line in lines]):
            layout = Pango.Layout.new(context)
            layout.set_font_description(Pango.FontDescription.from_string(font))
            layout.set_text(text, -1)
            snapshot.save()
            snapshot.translate(Graphene.Point().init(56, y))
            snapshot.append_layout(layout, ink)
            snapshot.restore()
            y += gap
        line = Gdk.RGBA()
        line.parse("#888888")
        snapshot.append_color(line, Graphene.Rect().init(56, 82, PAGE_W - 112, 1))
    node = snapshot.to_node()
    renderer = Gsk.CairoRenderer()
    renderer.realize_for_display(Gdk.Display.get_default())
    texture = renderer.render_texture(node, rect)
    renderer.unrealize()
    downloader = Gdk.TextureDownloader.new(texture)
    downloader.set_format(Gdk.MemoryFormat.R8G8B8)
    pixels, stride = downloader.download_bytes()
    data = pixels.get_data()
    return b"".join(data[y * stride:y * stride + PAGE_W * 3] for y in range(PAGE_H))


def rgb_tiff(pixels):
    """Uncompressed RGB TIFF, as a scanner sends for lossless scans."""
    offset = 8
    entries = [(256, 4, [PAGE_W]), (257, 4, [PAGE_H]), (258, 3, [8, 8, 8]), (259, 3, [1]),
               (262, 3, [2]), (273, 4, [offset]), (277, 3, [3]), (278, 4, [PAGE_H]),
               (279, 4, [len(pixels)]), (284, 3, [1])]
    out = bytearray(b"II*\x00") + struct.pack("<I", 0) + pixels
    extra = bytearray()
    ifd_pos = len(out)
    extra_pos = ifd_pos + 2 + 12 * len(entries) + 4
    packed = []
    for tag, typ, values in entries:
        data = struct.pack("<" + ("H" if typ == 3 else "I") * len(values), *values)
        if len(data) <= 4:
            value = data.ljust(4, b"\0")
        else:
            value = struct.pack("<I", extra_pos + len(extra))
            extra += data
        packed.append(struct.pack("<HHI", tag, typ, len(values)) + value)
    struct.pack_into("<I", out, 4, ifd_pos)
    out += struct.pack("<H", len(entries)) + b"".join(packed) + b"\0\0\0\0" + extra
    return bytes(out)


def wait(check, timeout=20.0):
    context = GLib.MainContext.default()
    deadline = time.monotonic() + timeout
    while not check():
        if time.monotonic() > deadline:
            raise TimeoutError("timed out")
        context.iteration(False)
        time.sleep(0.01)


def settle(seconds=0.6):
    """Let animations (dialogs, toasts) finish."""
    end = time.monotonic() + seconds
    wait(lambda: time.monotonic() > end)


def save_window(window, path):
    """Render everything inside the window (including open dialogs) to a PNG.

    Lays the content out explicitly first: without a connected browser the
    Broadway backend draws no new frames, so the last layout may be stale.
    """
    content = window.get_first_child()
    width, height = window.get_width(), window.get_height()
    content.allocate(width, height, -1, None)
    snapshot = Gtk.Snapshot()
    background = Gdk.RGBA()
    background.parse("#fafafb")  # libadwaita's light window background
    snapshot.append_color(background, Graphene.Rect().init(0, 0, width, height))
    Gtk.WidgetPaintable.new(content).snapshot(snapshot, width, height)
    node = snapshot.to_node()
    renderer = window.get_renderer()
    texture = renderer.render_texture(node, Graphene.Rect().init(0, 0, width, height))
    texture.save_to_png(path)
    print("saved", path)


def main():
    out_dir = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else "screenshots")
    os.makedirs(out_dir, exist_ok=True)
    os.environ["PATH"] = fake_ocr.make_ocr_bin(os.path.join(WORK, "bin"), ["tesseract"])
    os.environ["FAKE_OCR_LOG"] = os.path.join(WORK, "ocr.log")
    os.environ["LANG"] = "en_US.UTF-8"

    Adw.init()
    pages = [rgb_tiff(render_page(doc)) for doc in DOCUMENTS]
    original = fake_wsd.make_tiff
    served = iter(range(10 ** 6))
    fake_wsd.make_tiff = lambda color, **kw: pages[next(served) % len(pages)]

    import scanform  # noqa: E402
    import wsdscan  # noqa: E402
    import wsdscan_gui  # noqa: E402

    fake = fake_wsd.FakeScanner(model="ES-580W", service_path="/WDP/SCAN",
                                formats=["exif", "tiff-single-uncompressed"],
                                colors=["RGB24", "Grayscale8", "BlackAndWhite1"],
                                resolutions=[100, 300], sheets=2)
    fake.start()
    # Show the fake under a LAN address: probe the fake when that address is
    # configured, and show the address in place of 127.0.0.1 - only in the
    # displayed texts, so the app's address checks still run unchanged.
    real_probe = wsdscan.probe
    wsdscan.probe = lambda host, *a, **k: real_probe(
        fake.host if host == SHOWN_HOST else host, *a, **k)
    real_describe, real_label = scanform.describe_scanner, scanform.device_label
    scanform.describe_scanner = lambda *a, **k: tuple(
        text.replace("127.0.0.1", SHOWN_HOST) for text in real_describe(*a, **k))
    scanform.device_label = lambda *a, **k: real_label(*a, **k).replace("127.0.0.1", SHOWN_HOST)
    out = os.path.join(os.environ["HOME"], "Documents", "Scans")
    profile = dict(scanform.full_profile(wsdscan.CONFIG_DEFAULTS, {"host": SHOWN_HOST}),
                   lossless=True, ocr=True, outdir=out, filename="Invoice {date}")
    wsdscan.save_config({"scanner": "Office"}, scanners={
        "Office": profile,
        "Office b/w": dict(profile, mode="bw", review_pages=True, skip_blank=True, ocr=False)},
        sections={"gui": {"width": 520, "height": 940, "last_scanner": "Office"}})

    app = wsdscan_gui.ScanApp()
    app.register(None)
    app.activate()
    window = app.window
    wait(lambda: window.device is not None)

    # 1. Main window after a scan with text recognition.
    window.on_scan()
    wait(lambda: window.cancel_event is None)
    settle()
    save_window(window, os.path.join(out_dir, "main-window.png"))

    # 2. Review step: the blank back page was found and starts unticked.
    window.scanner_choice.set_value("Office b/w")
    wait(lambda: window.app.config["scanner"] == "Office b/w" and window.device is not None)
    window.on_scan()
    wait(lambda: window.review is not None)
    assert [tile.kept() for tile in window.tiles] == [True, False, True, True]
    settle()
    save_window(window, os.path.join(out_dir, "review-pages.png"))
    window.finish_review(True)
    wait(lambda: window.cancel_event is None)

    # 3. A scanner's page in the preferences.
    window.scanner_choice.set_value("Office")
    wait(lambda: window.app.config["scanner"] == "Office" and window.device is not None)
    if window.last_toast:
        window.last_toast.dismiss()
    prefs = wsdscan_gui.PreferencesDialog(window)
    prefs.present(window)
    prefs.open_scanner("Office")
    settle(1.0)
    save_window(window, os.path.join(out_dir, "scanner-settings.png"))

    fake_wsd.make_tiff = original
    fake.stop()


if __name__ == "__main__":
    main()
