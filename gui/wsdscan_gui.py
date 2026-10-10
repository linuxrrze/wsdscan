#!/usr/bin/env python3
"""Scan to PDF: desktop app for WSD network scanners (GTK 4 + libadwaita).

Scans every sheet in the document feeder into one PDF. Uses wsdscan.py for
the scanner protocol and shares its config file (~/.config/wsdscan/config.ini),
so defaults set here also apply to the `wsdscan` command.
"""

import os
import sys
import threading

HERE = os.path.dirname(os.path.realpath(__file__))
# Installed: wsdscan.py sits next to this file (also as a link in the source
# tree). The parent folder is only searched if needed, and last, so nothing
# else there (e.g. in ~/.local/share) can shadow a module.
sys.path.insert(0, HERE)
if not os.path.exists(os.path.join(HERE, "wsdscan.py")):
    sys.path.append(os.path.dirname(HERE))

# On KDE Plasma, use the desktop's own (KDE) file dialogs through the portal.
if "KDE" in os.environ.get("XDG_CURRENT_DESKTOP", "").upper().split(":"):
    os.environ.setdefault("GDK_DEBUG", "portals")

try:
    import gi

    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    gi.require_version("GdkPixbuf", "2.0")
    gi.require_version("Graphene", "1.0")
    from gi.repository import Adw, Gdk, GdkPixbuf, Gio, GLib, GObject, Graphene, Gtk
except (ImportError, ValueError) as e:
    sys.exit(f"Scan to PDF needs GTK 4 and libadwaita for Python ({e}).\n"
             "Debian/Ubuntu: sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1\n"
             "Fedora: sudo dnf install python3-gobject gtk4 libadwaita\n"
             "Arch: sudo pacman -S python-gobject gtk4 libadwaita")

import scanform  # noqa: E402
import wsdscan  # noqa: E402
from scanform import _  # noqa: E402

MIN_ADW = (1, 5)
THUMB_HEIGHT = 150  # page preview height in pixels
THUMB_DETAIL = 2  # thumbnail pixels per preview pixel of the page (sharp on HiDPI)
ICONS = os.path.join(HERE, "data", "icons")  # the correction icons (installed: next to this file)
CSS = """
.correction { min-width: 24px; min-height: 24px; padding: 0; }
.correction:not(:checked) { opacity: 0.55; }
"""
MAX_THUMBNAIL_SOURCE_PIXELS = 100_000_000  # 600 dpi A3 is ~70 megapixels


def in_main_thread(func, *args):
    """Run func(*args) in the GTK main loop (from a worker thread).

    At default priority, not idle: idle callbacks can starve while GTK keeps
    redrawing animations (e.g. a spinner), delaying results and progress.
    """
    def call():
        func(*args)
        return GLib.SOURCE_REMOVE
    GLib.idle_add(call, priority=GLib.PRIORITY_DEFAULT)


def call_in_main_thread(func):
    """Run func() in the GTK main loop and return its result; for worker threads
    that need to read widgets (only allowed in the main thread)."""
    done, result = threading.Event(), {}

    def run():
        try:
            result["value"] = func()
        except Exception as e:  # handed to the worker
            result["error"] = e
        done.set()

    in_main_thread(run)
    done.wait()
    if "error" in result:
        raise result["error"]
    return result["value"]


def run_in_thread(work, on_done, on_error):
    """Run work() in a thread; report its result or ScanError in the main loop."""
    def target():
        try:
            result = work()
        except wsdscan.ScanError as e:
            in_main_thread(on_error, e)
        except Exception as e:  # unexpected: still tell the user
            in_main_thread(on_error, wsdscan.ScanError(f"{type(e).__name__}: {e}"))
        else:
            in_main_thread(on_done, result)
    threading.Thread(target=target, daemon=True).start()


class ChoiceRow(Adw.ComboRow):
    """Combo row over (value, label) pairs."""

    def __init__(self, title, options, value):
        super().__init__(title=title, use_markup=False)
        self.values = []
        self.set_options(options, value)

    def set_options(self, options, value):
        self.values = [v for v, _label in options]
        self.set_model(Gtk.StringList.new([label for _v, label in options]))
        if value in self.values:
            self.set_selected(self.values.index(value))

    def get_value(self):
        index = self.get_selected()
        return self.values[index] if 0 <= index < len(self.values) else None

    def set_value(self, value):
        if value in self.values:
            self.set_selected(self.values.index(value))


class ScanSettings:
    """The scan option rows; used by the main window and the preferences."""

    def __init__(self, values, choices, ocr):
        self.choices = choices
        self.ocr_status = ocr
        self.lossless_wanted = bool(values["lossless"])
        self._syncing = False
        self.source = ChoiceRow(_("Sides"), choices.sources, values["source"])
        self.mode = ChoiceRow(scanform.pgettext("setting", "Color"), choices.modes, values["mode"])
        self.resolution = ChoiceRow(_("Resolution"), self._resolution_options(),
                                    choices.pick_resolution(values["resolution"]))
        self.paper = ChoiceRow(_("Paper size"), scanform.PAPERS, values["paper"])
        self.lossless = Adw.SwitchRow(use_markup=False, title=_("Lossless"))
        self.exposure = Adw.ExpanderRow(use_markup=False, title=_("Adjust brightness and contrast"),
                                        subtitle=_("Experimental; off = scanner default"),
                                        show_enable_switch=True)
        self.brightness = Adw.SpinRow.new_with_range(wsdscan.EXPOSURE_RANGE[0],
                                                     wsdscan.EXPOSURE_RANGE[1], 50)
        self.brightness.set_title(_("Brightness"))
        self.brightness.set_use_markup(False)
        self.contrast = Adw.SpinRow.new_with_range(wsdscan.EXPOSURE_RANGE[0],
                                                   wsdscan.EXPOSURE_RANGE[1], 50)
        self.contrast.set_title(_("Contrast"))
        self.contrast.set_use_markup(False)
        self.exposure.add_row(self.brightness)
        self.exposure.add_row(self.contrast)
        self.set_exposure(values["brightness"], values["contrast"])
        self.ocr = Adw.SwitchRow(use_markup=False, title=_("Recognize text (OCR)"))
        self.set_ocr(values)
        self.skip_blank = Adw.SwitchRow(
            use_markup=False, title=_("Remove blank pages"),
            subtitle=_("For example the empty backs of one-sided pages"),
            active=bool(values["skip_blank"]))
        self.deskew = Adw.SwitchRow(
            use_markup=False, title=_("Straighten pages"),
            subtitle=_("Pages fed in crooked; works best with automatic paper size"),
            active=bool(values["deskew"]))
        self.auto_rotate = Adw.SwitchRow(use_markup=False, title=_("Turn pages upright"))
        self.set_auto_rotate(values)
        self.review = Adw.SwitchRow(
            use_markup=False, title=_("Review pages before saving"),
            subtitle=_("Remove single pages before saving and text recognition"),
            active=bool(values["review_pages"]))

        self.mode.connect("notify::selected", lambda *_a: self.sync_lossless())
        self.lossless.connect("notify::active", self._on_lossless_toggled)
        self.sync_lossless()

    def rows(self):
        return [self.source, self.mode, self.resolution, self.paper, self.lossless, self.exposure,
                self.ocr, self.skip_blank, self.deskew, self.auto_rotate, self.review]

    def _resolution_options(self):
        return [(r, f"{r} dpi") for r in self.choices.resolutions]

    def _on_lossless_toggled(self, *_args):
        if not self._syncing:  # a user click, not sync_lossless()
            self.lossless_wanted = self.lossless.get_active()

    def sync_lossless(self):
        checked, sensitive, hint = self.choices.lossless_state(self.mode.get_value(),
                                                               self.lossless_wanted)
        self._syncing = True
        self.lossless.set_active(checked)
        self._syncing = False
        self.lossless.set_sensitive(sensitive)
        self.lossless.set_subtitle(hint)

    def set_ocr(self, values):
        """Switch state and subtitle; unavailable without OCRmyPDF/Tesseract."""
        status = self.ocr_status
        self.ocr.set_active(bool(values["ocr"]) and status.available)
        self.ocr.set_sensitive(status.available)
        self.ocr.set_subtitle(status.describe(values["ocr_engine"], values["ocr_lang"]))

    def set_auto_rotate(self, values):
        """Unavailable without Tesseract's orientation detection."""
        available = self.ocr_status.osd
        self.auto_rotate.set_active(bool(values["auto_rotate"]) and available)
        self.auto_rotate.set_sensitive(available)
        self.auto_rotate.set_subtitle(_("Pages that are sideways or upside down")
                                      if available else self.ocr_status.OSD_HINT)

    def set_exposure(self, brightness, contrast):
        enabled = brightness is not None or contrast is not None
        self.brightness.set_value(brightness or 0)
        self.contrast.set_value(contrast or 0)
        self.exposure.set_enable_expansion(enabled)
        self.exposure.set_expanded(enabled)

    def apply_choices(self, choices):
        """Narrow the rows down to what the scanner offers, keeping values if possible."""
        current = self.values()
        self.choices = choices
        self.source.set_options(choices.sources, choices.pick(choices.sources, current["source"]))
        self.mode.set_options(choices.modes, choices.pick(choices.modes, current["mode"]))
        self.resolution.set_options(self._resolution_options(),
                                    choices.pick_resolution(current["resolution"]))
        self.exposure.set_visible(choices.exposure)
        self.sync_lossless()

    def set_values(self, values):
        self.review.set_active(bool(values["review_pages"]))
        self.skip_blank.set_active(bool(values["skip_blank"]))
        self.deskew.set_active(bool(values["deskew"]))
        self.set_auto_rotate(values)
        self.source.set_value(values["source"])
        self.mode.set_value(values["mode"])
        self.resolution.set_value(self.choices.pick_resolution(values["resolution"]))
        self.paper.set_value(values["paper"])
        self.lossless_wanted = bool(values["lossless"])
        self.set_exposure(values["brightness"], values["contrast"])
        self.set_ocr(values)
        self.sync_lossless()

    def values(self):
        exposure = self.exposure.get_enable_expansion() and self.exposure.get_visible()
        return {
            "source": self.source.get_value(),
            "mode": self.mode.get_value(),
            "resolution": self.resolution.get_value(),
            "paper": self.paper.get_value(),
            # The user's choice; bw / scanner limits are applied when scanning.
            "lossless": self.lossless_wanted,
            "brightness": int(self.brightness.get_value()) if exposure else None,
            "contrast": int(self.contrast.get_value()) if exposure else None,
            "ocr": self.ocr.get_active(),
            "skip_blank": self.skip_blank.get_active(),
            "deskew": self.deskew.get_active(),
            "auto_rotate": self.auto_rotate.get_active(),
            "review_pages": self.review.get_active(),
        }

    def scan_values(self):
        """Values for scanning: lossless as shown, i.e. forced on/off by mode and scanner."""
        return dict(self.values(), lossless=self.lossless.get_active())


class FolderRow(Adw.ActionRow):
    """Shows a folder; the button opens the (portal) folder chooser."""

    def __init__(self, title, folder):
        super().__init__(title=title, use_markup=False)
        self.folder = folder
        self.set_subtitle(scanform.display_path(folder))
        button = Gtk.Button(icon_name="folder-open-symbolic", valign=Gtk.Align.CENTER,
                            tooltip_text=_("Choose folder"))
        button.add_css_class("flat")
        button.connect("clicked", self._choose)
        self.add_suffix(button)
        self.set_activatable_widget(button)

    def _choose(self, button):
        dialog = Gtk.FileDialog(title=_("Save scans in"), modal=True)
        dialog.set_initial_folder(Gio.File.new_for_path(self.folder))
        dialog.select_folder(button.get_root(), None, self._chosen)

    def _chosen(self, dialog, result):
        try:
            folder = dialog.select_folder_finish(result)
        except GLib.Error:
            return  # cancelled
        if folder and folder.get_path():
            self.set_folder(folder.get_path())

    def set_folder(self, folder):
        self.folder = folder
        self.set_subtitle(scanform.display_path(folder))


def make_thumbnail(data, info=None):
    """Decode a scanned page at preview size; runs in the scan thread. None if not possible.
    With a sheet found in a larger scan area (info), the sheet gets the preview size.

    Uses GTK's own JPEG/TIFF decoder: gdk-pixbuf's loaders are optional and
    may be missing (newer versions delegate to the glycin loaders).
    """
    try:
        width, height = wsdscan.image_size(data)
    except wsdscan.ScanError:
        return None
    if width * height > MAX_THUMBNAIL_SOURCE_PIXELS:
        return None  # a crafted header must not make GTK allocate gigabytes
    try:
        texture = Gdk.Texture.new_from_bytes(GLib.Bytes.new(data))
    except GLib.Error:
        return None
    width, height = texture.get_width(), texture.get_height()
    downloader = Gdk.TextureDownloader.new(texture)
    downloader.set_format(Gdk.MemoryFormat.R8G8B8A8)
    pixels, stride = downloader.download_bytes()
    pixbuf = GdkPixbuf.Pixbuf.new_from_bytes(pixels, GdkPixbuf.Colorspace.RGB, True, 8,
                                             width, height, stride)
    target = THUMB_HEIGHT * THUMB_DETAIL
    if info and info.get("paper") and info["size"][1]:
        sheet = min(info["paper"][2:4])  # its shorter side may end up as the height
        target = min(height, round(target * info["size"][1] / max(1, sheet)))
    if height <= target:
        return pixbuf
    return pixbuf.scale_simple(max(1, width * target // height), target,
                               GdkPixbuf.InterpType.BILINEAR)


def pixbuf_texture(pixbuf):
    fmt = Gdk.MemoryFormat.R8G8B8A8 if pixbuf.get_has_alpha() else Gdk.MemoryFormat.R8G8B8
    return Gdk.MemoryTexture.new(pixbuf.get_width(), pixbuf.get_height(), fmt,
                                 pixbuf.read_pixel_bytes(), pixbuf.get_rowstride())


class PagePaintable(GObject.Object, Gdk.Paintable):
    """A scanned page as it will be saved: the thumbnail placed with the
    page's corrections (cut, straightened, turned), like wsdscan.page_layout
    does in the PDF."""

    def __init__(self, texture, info):
        super().__init__()
        self.texture, self.info = texture, info

    def do_get_intrinsic_aspect_ratio(self):
        page_w, page_h, _place = wsdscan.page_layout(self.info)
        return page_w / page_h if page_h else 0.0

    def do_snapshot(self, snapshot, width, height):
        page_w, page_h, cx, cy, angle = wsdscan.page_geometry(self.info)
        if not page_w or not page_h:
            return
        scale = min(width / page_w, height / page_h)
        thumb = self.texture.get_width() / self.info["size"][0]
        white = Gdk.RGBA()
        white.parse("white")
        area = Graphene.Rect().init(0, 0, page_w * scale, page_h * scale)
        snapshot.save()
        snapshot.translate(Graphene.Point().init((width - page_w * scale) / 2,
                                                 (height - page_h * scale) / 2))
        snapshot.push_clip(area)
        snapshot.append_color(white, area)  # outside the scan (a sheet's cut-off corner)
        # Plain steps (every GSK renderer draws them): the point (cx, cy) of
        # the image to the page's center, turned around it.
        snapshot.translate(Graphene.Point().init(page_w * scale / 2, page_h * scale / 2))
        snapshot.rotate(angle)
        snapshot.scale(scale / thumb, scale / thumb)
        snapshot.translate(Graphene.Point().init(-cx * thumb, -cy * thumb))
        snapshot.append_texture(self.texture, Graphene.Rect().init(
            0, 0, self.texture.get_width(), self.texture.get_height()))
        snapshot.pop()
        snapshot.restore()

    def changed(self):
        self.invalidate_size()
        self.invalidate_contents()


class PageTile(Gtk.Box):
    """Preview of one scanned page: thumbnail, page number, keep checkbox
    (review step), text recognition state, a button per correction made
    (cut to the sheet, straightened, turned) that switches it off and on,
    and buttons to turn the page by 90° (review step)."""

    def __init__(self, number, pixbuf, on_toggled, info=None, dpi=300):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.number = number
        self.info, self.dpi = info, dpi
        self.paintable = None
        self.picture = Gtk.Picture(content_fit=Gtk.ContentFit.CONTAIN, can_shrink=True,
                                   width_request=THUMB_HEIGHT * 7 // 10,
                                   height_request=THUMB_HEIGHT)
        self.picture.add_css_class("card")
        if pixbuf:
            texture = pixbuf_texture(pixbuf)
            if info and info["size"][0] and info["size"][1]:
                self.paintable = PagePaintable(texture, info)
                self.picture.set_paintable(self.paintable)
            else:
                self.picture.set_paintable(texture)
        self.correction_buttons = {}
        corrections = Gtk.Box(spacing=2, halign=Gtk.Align.START, valign=Gtk.Align.END,
                              margin_bottom=4, margin_start=4)
        for name in wsdscan.corrections_done(info) if info else []:
            button = Gtk.ToggleButton(active=info["use"][name], sensitive=False,
                                      child=Gtk.Image(icon_name=scanform.CORRECTION_ICONS[name],
                                                      pixel_size=12))
            button.add_css_class("osd")
            button.add_css_class("correction")
            button.connect("toggled", self._correction_toggled, name)
            self.correction_buttons[name] = button
            corrections.append(button)
            self._show_correction(name)
        self.turn_buttons = Gtk.Box(spacing=2, halign=Gtk.Align.START, valign=Gtk.Align.START,
                                    margin_top=4, margin_start=4, visible=False)
        for degrees, tooltip in ((-90, _("Turn left")), (90, _("Turn right"))):
            button = Gtk.Button(tooltip_text=tooltip,
                                child=Gtk.Image(icon_name=scanform.TURN_ICONS[degrees],
                                                pixel_size=12))
            button.add_css_class("osd")
            button.add_css_class("correction")
            button.connect("clicked", self._turn_clicked, degrees)
            self.turn_buttons.append(button)
        self.keep = Gtk.CheckButton(active=True, visible=False, halign=Gtk.Align.END,
                                    valign=Gtk.Align.START, margin_top=4, margin_end=4,
                                    tooltip_text=_("Keep this page"))
        self.keep.connect("toggled", self._toggled, on_toggled)
        self.state = Gtk.Image(visible=False, halign=Gtk.Align.END, valign=Gtk.Align.END,
                               margin_bottom=4, margin_end=4, pixel_size=20)
        self.spinner = Gtk.Spinner(spinning=True, visible=False, halign=Gtk.Align.END,
                                   valign=Gtk.Align.END, margin_bottom=4, margin_end=4)
        overlay = Gtk.Overlay(child=self.picture)
        for widget in (self.keep, self.state, self.spinner, corrections, self.turn_buttons):
            overlay.add_overlay(widget)
        self.append(overlay)
        self.label = Gtk.Label(label=_("Page {n}").format(n=number), css_classes=["caption"])
        self.append(self.label)

    def _toggled(self, button, on_toggled):
        self.picture.set_opacity(1.0 if button.get_active() else 0.35)
        on_toggled()

    def _correction_toggled(self, button, name):
        self.info["use"][name] = button.get_active()  # read when the scan saves the PDF
        self._show_correction(name)
        if self.paintable:
            self.paintable.changed()

    def _turn_clicked(self, _button, degrees):
        self.turn(degrees)

    def turn(self, degrees):
        """Turn the page by hand (read when the scan saves the PDF)."""
        self.info["turn"] = (self.info.get("turn", 0) + degrees) % 360
        self.paintable.changed()

    def _show_correction(self, name):
        button = self.correction_buttons[name]
        button.set_tooltip_text(scanform.correction_tooltip(name, self.info, self.dpi,
                                                            button.get_active()))

    def set_corrections_editable(self, editable):
        """Corrections can be switched until the PDF is saved (with the review step)."""
        for button in self.correction_buttons.values():
            button.set_sensitive(editable)
        self.turn_buttons.set_visible(editable and self.paintable is not None)

    def mark_blank(self, blank):
        """A blank page starts unticked (removed unless the review keeps it)."""
        self.keep.set_active(not blank)
        self.picture.set_tooltip_text(_("Blank page") if blank else None)

    def set_review(self, review):
        self.keep.set_visible(review)
        self.keep.set_sensitive(review)

    def kept(self):
        return self.keep.get_active()

    def set_ocr_state(self, state):
        """None, "waiting", "running" or "done"."""
        self.spinner.set_visible(state == "running")
        self.state.set_visible(state in ("waiting", "done"))
        if state == "done":
            self.state.set_from_icon_name("object-select-symbolic")
            self.state.set_tooltip_text(_("Text recognized"))
        elif state == "waiting":
            self.state.set_from_icon_name("content-loading-symbolic")
            self.state.set_tooltip_text(_("Waiting for text recognition"))


class MainWindow(Adw.ApplicationWindow):
    RETRY_SECONDS = 30  # look for a missing scanner again (e.g. started before the network)

    def __init__(self, app):
        super().__init__(application=app, title=scanform.APP_NAME)
        self.app = app
        self.device = None
        self._connect_token = 0
        self._retry_source = 0
        self.choices = scanform.Choices.unknown()
        self.cancel_event = None
        gui = app.gui_config
        self.set_default_size(gui["width"], gui["height"])
        self.set_size_request(360, 400)

        cfg = app.config
        # Header
        self.title = Adw.WindowTitle(title=scanform.APP_NAME, subtitle=_("Looking for the scanner…"))
        header = Adw.HeaderBar(title_widget=self.title)
        refresh = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text=_("Reconnect to the scanner"),
                             action_name="win.reconnect")
        header.pack_start(refresh)
        menu = Gio.Menu()
        menu.append(_("Preferences"), "app.preferences")
        menu.append(_("About Scan to PDF"), "app.about")
        menu.append(_("Quit"), "app.quit")
        header.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu,
                                       primary=True, tooltip_text=_("Main Menu")))

        self.banner = Adw.Banner(use_markup=False)  # text and button: scanform.scanner_problem

        # Scanner: selector (with two or more configured scanners) and status
        self.scanner_choice = ChoiceRow(_("Scanner"), [], None)
        self.scanner_choice.set_visible(False)
        self._updating_choice = False
        self.scanner_choice.connect("notify::selected", self._scanner_chosen)
        self.scanner_row = Adw.ActionRow(use_markup=False, title=_("Scanner"), subtitle=_("Searching…"))
        self.scanner_row.add_prefix(Gtk.Image(icon_name="scanner-symbolic"))
        self.spinner = Gtk.Spinner(spinning=True, valign=Gtk.Align.CENTER)
        self.scanner_row.add_suffix(self.spinner)
        scanner_group = Adw.PreferencesGroup()
        scanner_group.add(self.scanner_choice)
        scanner_group.add(self.scanner_row)
        self.refresh_scanner_choice()

        # Document
        self.name_row = Adw.EntryRow(use_markup=False, title=_("File name"))
        self.name_row.connect("entry-activated", self._name_activated)
        self.folder_row = FolderRow(_("Folder"), cfg["outdir"] or scanform.documents_dir())
        # Scan settings: summary here, the rows live in ScanSettingsDialog.
        self.settings = ScanSettings(cfg, self.choices, app.ocr)
        self.settings_row = Adw.ActionRow(use_markup=False, title=_("Scan settings"), activatable=True,
                                          action_name="win.scan-settings")
        self.settings_row.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
        doc_group = Adw.PreferencesGroup(title=_("Document"))
        doc_group.add(self.name_row)
        doc_group.add(self.folder_row)
        doc_group.add(self.settings_row)
        self.new_file_name()
        self.update_summary()

        # Action area
        self.scan_button = Gtk.Button(label=_("Scan"), action_name="win.scan",
                                      halign=Gtk.Align.CENTER)
        self.scan_button.add_css_class("suggested-action")
        self.scan_button.add_css_class("pill")
        self.cancel_button = Gtk.Button(label=_("Cancel"), action_name="win.cancel",
                                        halign=Gtk.Align.CENTER, visible=False)
        self.cancel_button.add_css_class("pill")
        self.progress = Gtk.Label(css_classes=["dim-label"], wrap=True, justify=Gtk.Justification.CENTER)
        hint = Gtk.Label(label=_("Put your pages in the document feeder, then click Scan."),
                         css_classes=["dim-label", "caption"], wrap=True)
        self.review_bar = Gtk.Box(spacing=12, halign=Gtk.Align.CENTER, visible=False)
        discard = Gtk.Button(label=_("Discard Scan"))
        discard.add_css_class("pill")
        discard.connect("clicked", lambda *_a: self.finish_review(False))
        self.save_button = Gtk.Button(label=_("Save"))
        self.save_button.add_css_class("suggested-action")
        self.save_button.add_css_class("pill")
        self.save_button.connect("clicked", lambda *_a: self.finish_review(True))
        self.review_bar.append(discard)
        self.review_bar.append(self.save_button)
        actions = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for child in (hint, self.scan_button, self.cancel_button, self.review_bar, self.progress):
            actions.append(child)

        # Page preview
        self.tiles = []
        self.last_toast = None  # "Saved …" of the previous scan
        self.review = None      # (threading.Event, result dict) while reviewing
        self.ocr_tiles = []     # tiles of the pages being recognized
        self.ocr_total = 0
        self.blank_pages = 0    # found blank in the current scan
        self.scan_reviewed = False
        self.review_open = False  # page corrections can be switched (review, before saving)
        self.scan_dpi = 300
        self.page_count = Gtk.Label(css_classes=["dim-label"])
        self.page_grid = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, homogeneous=True,
                                     min_children_per_line=2, max_children_per_line=6,
                                     column_spacing=12, row_spacing=12)
        self.pages_group = Adw.PreferencesGroup(title=_("Pages"), visible=False)
        self.pages_group.set_header_suffix(self.page_count)
        self.pages_group.add(self.page_grid)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=24,
                      margin_top=24, margin_bottom=24, margin_start=12, margin_end=12)
        for section in (scanner_group, doc_group, actions, self.pages_group):
            box.append(section)

        self.toasts = Adw.ToastOverlay(child=Gtk.ScrolledWindow(
            child=Adw.Clamp(child=box, maximum_size=560), vexpand=True,
            hscrollbar_policy=Gtk.PolicyType.NEVER))
        view = Adw.ToolbarView(content=self.toasts)
        view.add_top_bar(header)
        view.add_top_bar(self.banner)
        self.set_content(view)

        self.actions = {}
        self._add_action("scan", self.on_scan)
        self._add_action("cancel", self.on_cancel)
        self._add_action("reconnect", lambda *_a: self.connect_scanner())
        self._add_action("scan-settings", lambda *_a: ScanSettingsDialog(self).present(self))
        self.connect("close-request", self._on_close)
        monitor = Gio.NetworkMonitor.get_default()
        network = monitor.connect("network-changed", self._network_changed)
        self.connect("destroy", lambda *_a: (monitor.disconnect(network), self._cancel_retry()))
        self.connect_scanner()

    def refresh_scanner_choice(self):
        names = list(self.app.scanners)
        self._updating_choice = True
        if names == self.scanner_choice.values:
            self.scanner_choice.set_value(self.app.config["scanner"])
        else:
            self.scanner_choice.set_options([(n, n) for n in names], self.app.config["scanner"])
        self._updating_choice = False
        self.scanner_choice.set_visible(len(names) >= 2)

    def _scanner_chosen(self, *_args):
        name = self.scanner_choice.get_value()
        if not self._updating_choice and name and name != self.app.config["scanner"]:
            # Not inside the combo row's own signal: switching rebuilds its list,
            # which crashed GTK while the selection was still being changed.
            def switch():
                self.app.select_scanner(name)
                return GLib.SOURCE_REMOVE
            GLib.idle_add(switch)

    def _add_action(self, name, callback):
        action = Gio.SimpleAction.new(name, None)
        action.connect("activate", callback)
        self.add_action(action)
        self.actions[name] = action

    def _on_close(self, *_args):
        width, height = self.get_default_size()
        self.app.save_gui({"width": width, "height": height})
        if self.app.tray_active():
            self.set_visible(False)  # keep running in the status bar
            return True
        if self.cancel_event:
            self.cancel_event.set()
        if self.review:
            self.finish_review(False)
        return False

    # --- scanner connection --------------------------------------------------

    def connect_scanner(self, quiet=False):
        """Look for the scanner. quiet: a retry in the background that keeps
        showing "not found" until the scanner answers."""
        cfg = self.app.config
        self._cancel_retry()
        self._connect_token += 1
        token = self._connect_token
        if not quiet:
            self.device = None
            self.spinner.set_visible(True)
            self.scanner_row.set_title(_("Scanner"))
            self.scanner_row.set_subtitle(_("Searching…"))
            self.title.set_subtitle(_("Looking for the scanner…") if scanform.scanner_configured(cfg)
                                    else _("Looking for scanners on the network…"))
            self.banner.set_revealed(False)
            self.actions["scan"].set_enabled(False)

        def work():
            device = wsdscan.find_scanner(cfg["host"] or None, cfg["model"] or None)
            try:
                caps = wsdscan.get_capabilities(device["service"])
            except wsdscan.SoapFault as e:
                raise wsdscan.ScanError(f"could not read scanner capabilities: {e}")
            return device, caps

        # Only the latest attempt counts: an older one that times out later
        # must not report "not found" over a scanner found since.
        run_in_thread(work, lambda result: token == self._connect_token and self._connected(result),
                      lambda error: token == self._connect_token and self._connect_failed(error))

    def _cancel_retry(self):
        if self._retry_source:
            GLib.source_remove(self._retry_source)
            self._retry_source = 0

    def _retry_connect(self):
        self._retry_source = 0
        if self.device is not None:
            return GLib.SOURCE_REMOVE
        if self.app.busy:  # a scan looks for the scanner itself
            self._retry_source = GLib.timeout_add_seconds(self.RETRY_SECONDS, self._retry_connect)
        else:
            self.connect_scanner(quiet=True)
        return GLib.SOURCE_REMOVE

    def _network_changed(self, _monitor, available):
        if available and self.device is None and self._retry_source:
            self._cancel_retry()
            self._retry_source = GLib.timeout_add_seconds(2, self._retry_connect)

    def _connected(self, result):
        device, caps = result
        self.device = device
        self.choices = scanform.Choices.from_capabilities(caps)
        self.settings.apply_choices(self.choices)
        self.update_summary()
        title, subtitle = scanform.describe_scanner(device, caps, self.app.config["scanner"])
        self.scanner_row.set_title(title)
        self.scanner_row.set_subtitle(subtitle)
        self.title.set_subtitle(title)
        self.spinner.set_visible(False)
        self.banner.set_revealed(False)
        self.actions["scan"].set_enabled(not self.app.busy)

    def _connect_failed(self, error):
        problem = scanform.scanner_problem(error, self.app.config)
        self.spinner.set_visible(False)
        self.scanner_row.set_title(problem.title)
        self.scanner_row.set_subtitle(problem.detail)
        self.title.set_subtitle(problem.status)
        self.banner.set_title(problem.banner)
        self.banner.set_button_label(problem.button)
        self.banner.set_action_name(problem.action)
        self.banner.set_revealed(True)
        # The scan looks for the scanner again, so it may be tried anyway.
        self.actions["scan"].set_enabled(problem.can_scan and not self.app.busy)
        if problem.retry:
            self._retry_source = GLib.timeout_add_seconds(self.RETRY_SECONDS, self._retry_connect)

    # --- scanning -----------------------------------------------------------

    def _name_activated(self, *_args):
        if self.review:
            self.finish_review(True)  # Enter after editing the name: save
        else:
            self.activate_action("win.scan", None)

    def update_summary(self):
        self.settings_row.set_subtitle(scanform.settings_summary(
            self.settings.scan_values(), review=self.settings.review.get_active()))

    def new_file_name(self):
        self.name_row.set_text(wsdscan.render_filename(self.app.config["filename"]))

    def set_busy(self, busy):
        # File name and folder stay editable: they are read only when saving.
        # The scan settings row is locked through its action.
        self.scan_button.set_visible(not busy)
        self.cancel_button.set_visible(busy)
        self.actions["scan"].set_enabled(not busy)
        self.actions["reconnect"].set_enabled(not busy)
        self.actions["scan-settings"].set_enabled(not busy)
        self.scanner_choice.set_sensitive(not busy)
        self.app.set_busy(busy)

    def set_progress(self, text):
        self.progress.set_text(text)
        self.app.set_status(text or _("Ready"))

    # --- page preview -------------------------------------------------------

    def clear_pages(self):
        self.page_grid.remove_all()
        self.tiles = []
        self.ocr_tiles = []
        self.pages_group.set_visible(False)

    def add_page(self, number, pixbuf, info=None):
        tile = PageTile(number, pixbuf, self.update_page_count, info, self.scan_dpi)
        if info and info["blank"]:
            tile.mark_blank(True)
        tile.set_corrections_editable(self.scan_reviewed and self.review_open)
        self.tiles.append(tile)
        self.page_grid.append(tile)
        self.pages_group.set_visible(True)
        self.update_page_count()

    def update_page_count(self):
        kept = sum(tile.kept() for tile in self.tiles)
        self.page_count.set_text(scanform.pages_summary(len(self.tiles), kept))
        if self.review:
            self.save_button.set_sensitive(kept > 0)

    def start_review(self, event, result):
        """Called from the scan thread (via the main loop) after the last page."""
        self.review = (event, result)
        for tile in self.tiles:
            tile.set_review(True)
        self.cancel_button.set_visible(False)
        self.review_bar.set_visible(True)
        self.update_page_count()
        self.set_progress(scanform.progress_text("review", len(self.tiles)))
        if not self.is_visible():
            self.present()  # the decision needs the window

    def finish_review(self, save):
        if not self.review:
            return
        event, result = self.review
        self.review = None
        kept = [i for i, tile in enumerate(self.tiles) if tile.kept()]
        result["keep"] = kept if save else None
        self.ocr_tiles = [self.tiles[i] for i in kept]
        self.review_open = False
        for tile in self.tiles:
            tile.set_review(False)
            tile.set_corrections_editable(False)
        self.review_bar.set_visible(False)
        self.cancel_button.set_visible(True)
        event.set()

    def on_progress(self, event, pages):
        """Scan progress in the main loop: counter, OCR state per page."""
        if event == "blank":
            self.blank_pages += 1
            return  # the tile shows it; keep the progress text
        if event == "all_blank":
            self.blank_pages = 0
            for tile in self.tiles:  # they are saved after all
                tile.mark_blank(False)
            return
        if event == "ocr":
            if not self.ocr_tiles:  # without review: the pages not removed as blank
                self.ocr_tiles = [tile for tile in self.tiles if tile.kept()]
            self.ocr_total = pages
            for tile in self.ocr_tiles:
                tile.set_ocr_state("waiting")
            if self.ocr_tiles:
                self.ocr_tiles[0].set_ocr_state("running")
        elif event == "ocr_page":
            for i, tile in enumerate(self.ocr_tiles):
                tile.set_ocr_state("done" if i < pages else "running" if i == pages else "waiting")
        if event in ("ocr", "ocr_page"):
            # During OCR the cancel button would leave a half-processed file.
            self.cancel_button.set_sensitive(False)
        self.set_progress(scanform.progress_text(event, pages, self.ocr_total))

    # --- scanning -----------------------------------------------------------

    def on_scan(self, *_args):
        if self.cancel_event:
            return
        if not self.name_row.get_text().strip():
            self.new_file_name()
        try:
            os.makedirs(self.folder_row.folder, exist_ok=True)
        except OSError as e:
            self.show_error(_("Cannot use the folder: {error}").format(error=e.strerror))
            return
        values = dict(self.app.config, **self.settings.scan_values())
        args = scanform.scan_args(values)
        review = self.settings.review.get_active()
        if self.last_toast:
            self.last_toast.dismiss()  # belongs to the previous scan
            self.last_toast = None
        self.cancel_event = threading.Event()
        cancel = self.cancel_event
        self.clear_pages()
        self.ocr_total = 0
        self.blank_pages = 0  # found blank, removed unless reviewed
        self.scan_reviewed = review
        self.review_open = review  # corrections can still be switched (until saving)
        self.scan_dpi = args.resolution
        self.cancel_button.set_sensitive(True)
        self.set_busy(True)
        self.set_progress(scanform.progress_text("scanning", 0))

        def progress(event, pages):
            in_main_thread(self.on_progress, event, pages)

        def page_image(number, data, info):
            in_main_thread(self.add_page, number, make_thumbnail(data, info), info)

        def select_pages(images, infos):
            """Wait (in the scan thread) for the user's review decision."""
            decided, result = threading.Event(), {"keep": None}
            in_main_thread(self.start_review, decided, result)
            while not decided.wait(0.2):
                if cancel.is_set():
                    in_main_thread(self.finish_review, False)
                    return None
            return result["keep"]

        saved = {}

        def output_path():
            """Called by the scan right before saving: the name and folder as
            they are now, after scanning and reviewing."""
            def current():
                folder = self.folder_row.folder
                return folder, (scanform.output_path(folder, self.name_row.get_text())
                                or scanform.output_path(folder, wsdscan.render_filename(
                                    self.app.config["filename"])))
            folder, out = call_in_main_thread(current)
            try:
                os.makedirs(folder, exist_ok=True)
            except OSError as e:
                raise wsdscan.ScanError(_("Cannot use the folder {folder}: {error}").format(
                    folder=folder, error=e.strerror))
            saved["out"] = out
            return out

        def work():
            pages, complete, ocr_error = wsdscan.scan_to_file(
                args, output_path, device=self.device, on_progress=progress,
                should_stop=cancel.is_set, on_page_image=page_image,
                select_pages=select_pages if review else None)
            return saved["out"], pages, complete, ocr_error

        run_in_thread(work, self._scan_done, self._scan_failed)

    def on_cancel(self, *_args):
        if self.review:
            self.finish_review(False)
        elif self.cancel_event:
            self.cancel_event.set()
            self.set_progress(_("Cancelling after the current page…"))

    def _scan_done(self, result):
        out, pages, complete, ocr_error = result
        self.cancel_event = None
        self.set_busy(False)
        self.set_progress("")
        if not ocr_error:
            for tile in self.ocr_tiles:
                tile.set_ocr_state("done")  # OCRmyPDF reports no single pages
        else:
            for tile in self.ocr_tiles:
                tile.set_ocr_state(None)
        name = os.path.basename(out)
        if complete and self.blank_pages and not self.scan_reviewed:
            message = _("Saved {pages} pages as “{name}”, removed {blank} blank pages").format(
                pages=pages, name=name, blank=self.blank_pages)
        elif complete:
            message = _("Saved {pages} pages as “{name}”").format(pages=pages, name=name)
        else:
            message = _("Scan stopped early; saved {pages} pages as “{name}”").format(
                pages=pages, name=name)
        toast = Adw.Toast(use_markup=False, title=message, button_label=_("Open"), action_name="app.open-file",
                          action_target=GLib.Variant("s", out), timeout=8)
        self.app.saved_files.add(out)
        self.toasts.add_toast(toast)
        self.last_toast = toast
        self.new_file_name()
        if ocr_error:
            self.show_error(_("{error}\n\nThe scan was saved without recognized text.").format(
                error=ocr_error), heading=_("Text recognition failed"))
        if self.device is None:
            # The scan found the scanner although the last connection attempt
            # failed: refresh the status (and hide the "not found" banner).
            self.connect_scanner()
        if self.app.gui_config["open_after_scan"]:
            self.app.open_file(out)
        if self.app.gui_config["notify"] and not (self.is_visible() and self.is_active()):
            self.app.notify_done(message, out)

    def _scan_failed(self, error):
        self.cancel_event = None
        self.review = None
        self.review_bar.set_visible(False)
        self.set_busy(False)
        self.set_progress("")
        if isinstance(error, wsdscan.ScanCancelled):
            self.clear_pages()
            self.toasts.add_toast(Adw.Toast(use_markup=False, title=_("Scan discarded")
                                            if "discarded" in str(error) or "removed" in str(error)
                                            else _("Scan cancelled")))
            return
        self.show_error(str(error))

    def show_error(self, message, heading=None):
        dialog = Adw.AlertDialog(heading=heading or _("Scanning failed"), body=message)
        dialog.add_response("ok", _("OK"))
        dialog.present(self)

    def apply_preferences(self, reconnect):
        cfg = self.app.config
        self.refresh_scanner_choice()
        if reconnect:
            # Another scanner may offer what the previous one did not (e.g. black &
            # white): offer everything until the new scanner has answered.
            self.choices = scanform.Choices.unknown()
            self.settings.apply_choices(self.choices)
        self.settings.set_values(cfg)
        self.update_summary()
        self.folder_row.set_folder(cfg["outdir"] or scanform.documents_dir())
        self.new_file_name()
        if reconnect:
            self.connect_scanner()


class ScanSettingsDialog(Adw.Dialog):
    """Settings for the next scans. Shows the main window's ScanSettings rows,
    so changes apply immediately; they can be saved as defaults."""

    def __init__(self, window):
        super().__init__(title=_("Scan Settings"), content_width=440)
        self.window = window
        self.group = Adw.PreferencesGroup(
            description=_("These settings apply to your next scans. Only options your "
                          "scanner supports are offered."))
        for row in window.settings.rows():
            self.group.add(row)
        page = Adw.PreferencesPage()
        page.add(self.group)

        reset = Gtk.Button(label=_("Reset to Defaults"))
        reset.connect("clicked", self._reset)
        save = Gtk.Button(label=_("Save for This Scanner") if window.app.config["scanner"]
                          else _("Save as Defaults"))
        save.connect("clicked", self._save_defaults)
        buttons = Gtk.Box(spacing=12, halign=Gtk.Align.CENTER, margin_top=12,
                          margin_bottom=12, margin_start=12, margin_end=12)
        buttons.append(reset)
        buttons.append(save)

        self.toasts = Adw.ToastOverlay(child=page)
        view = Adw.ToolbarView(content=self.toasts)
        view.add_top_bar(Adw.HeaderBar())
        view.add_bottom_bar(buttons)
        self.set_child(view)
        self.connect("closed", self._closed)

    def _reset(self, *_args):
        self.window.settings.set_values(self.window.app.config)
        self.toasts.add_toast(Adw.Toast(use_markup=False, title=_("Defaults restored")))

    def _save_defaults(self, *_args):
        try:
            self.window.app.save_scanner_settings(self.window.settings.values())
        except (ValueError, OSError) as e:
            self.window.show_error(_("Could not save the defaults: {error}").format(error=e))
            return
        self.toasts.add_toast(Adw.Toast(use_markup=False, title=_("Saved as defaults of “{name}”").format(
            name=self.window.app.config["scanner"]) if self.window.app.config["scanner"]
            else _("Saved as defaults")))

    def _closed(self, *_args):
        # Hand the rows back so the next dialog can show them again.
        for row in self.window.settings.rows():
            self.group.remove(row)
        self.window.update_summary()


class OcrRows:
    """Engine and languages of one scanner's text recognition."""

    def __init__(self, values, ocr):
        self.ocr = ocr
        self.engine = ChoiceRow(_("Engine"), ocr.engine_options(), values["ocr_engine"])
        self.auto = Adw.SwitchRow(
            use_markup=False, title=_("Choose languages automatically"),
            subtitle=_("Currently: {langs}").format(langs=ocr.default_lang),
            active=not values["ocr_lang"])
        self.lang_choices = ocr.language_choices(values["ocr_lang"] or ocr.default_lang)
        self.lang_rows = {}
        for code, label, selected, installed in self.lang_choices:
            self.lang_rows[code] = Adw.SwitchRow(
                use_markup=False, title=label, active=selected, subtitle="" if installed else _("Not installed"))
        self.hint = Adw.ActionRow(
            use_markup=False, subtitle=_("More languages come as packages, e.g. “tesseract-ocr-fra” for French."),
            css_classes=["property"])
        self.auto.connect("notify::active", lambda *_a: self._sync())
        for row in self.rows():
            row.set_sensitive(ocr.available)
        self._sync()

    def rows(self):
        return [self.engine, self.auto, *self.lang_rows.values(), self.hint]

    def _sync(self):
        manual = self.ocr.available and not self.auto.get_active()
        for row in self.lang_rows.values():
            row.set_sensitive(manual)

    def values(self):
        if self.auto.get_active():
            lang = ""
        else:
            selected = {code for code, row in self.lang_rows.items() if row.get_active()}
            lang = scanform.OcrStatus.join_languages(self.lang_choices, selected)
        return {"ocr_engine": self.engine.get_value() or "auto", "ocr_lang": lang}


class ScannerPage(Adw.NavigationPage):
    """All settings of one scanner (a subpage of the preferences)."""

    LOOKUP_DELAY_MS = 800

    def __init__(self, prefs, name):
        window = prefs.window
        app = window.app
        values = prefs.profiles.get(name) or scanform.full_profile(app.shared_config, {})
        super().__init__(title=name or _("New Scanner"))
        self.prefs = prefs
        self.old_name = name
        # Not shown in the app (an address or host name identifies a scanner
        # better); a model filter from the config file is kept for the CLI.
        self.kept_model = values["model"]
        self.removed = False
        self.name_auto = not name  # propose the device name until the user types one
        self._setting_name = False
        self._lookup_source = 0
        self._lookup_token = 0

        ident = Adw.PreferencesGroup(title=_("Scanner"))
        self.name_row = Adw.EntryRow(use_markup=False, title=_("Name"), text=name or "")
        self.name_row.connect("changed", self._name_changed)
        self.host_row = Adw.EntryRow(use_markup=False, title=_("IP address or host name"),
                                     text=values["host"])
        self.host_row.connect("changed", lambda *_a: self._schedule_lookup())
        self.device_row = Adw.ActionRow(use_markup=False, title=_("Device"), css_classes=["property"])
        is_default = name == prefs.default_name or not prefs.profiles
        self.default_row = Adw.SwitchRow(use_markup=False, title=_("Use by default"), active=is_default)
        if is_default and name:
            # There is always one default: choose another scanner to change it.
            self.default_row.set_sensitive(False)
            self.default_row.set_subtitle(_("To change, make another scanner the default"))
        # Address first: the name is proposed from the scanner found there.
        for widget in (self.host_row, self.name_row, self.device_row, self.default_row):
            ident.add(widget)

        connected = name is not None and name == app.config["scanner"] and window.device
        self.settings = ScanSettings(values, window.choices if connected else
                                     scanform.Choices.unknown(), app.ocr)
        scan = Adw.PreferencesGroup(title=_("Scan settings"),
                                    description=_("Used for every scan with this scanner."))
        for row in self.settings.rows():
            scan.add(row)

        self.ocr_rows = OcrRows(values, app.ocr)
        ocr = Adw.PreferencesGroup(
            title=_("Text recognition (OCR)"),
            description=None if app.ocr.available else app.ocr.INSTALL_HINT)
        for row in self.ocr_rows.rows():
            ocr.add(row)

        saving = Adw.PreferencesGroup(title=_("Saving"))
        self.folder = FolderRow(_("Folder"), values["outdir"] or scanform.documents_dir())
        self.follow_documents = not values["outdir"]
        self.filename = Adw.EntryRow(use_markup=False, title=_("File name"), text=values["filename"])
        hint = Adw.ActionRow(
            use_markup=False, subtitle=_("{date} and {time} are replaced with the scan date and time."),
            css_classes=["property"])
        for widget in (self.folder, self.filename, hint):
            saving.add(widget)

        page = Adw.PreferencesPage()
        for group in (ident, scan, ocr, saving):
            page.add(group)
        if name:
            actions = Adw.PreferencesGroup()
            box = Gtk.Box(spacing=12, halign=Gtk.Align.CENTER)
            duplicate = Gtk.Button(label=_("Duplicate"))
            duplicate.add_css_class("pill")
            duplicate.connect("clicked", self._duplicate)
            remove = Gtk.Button(label=_("Remove Scanner"))
            remove.add_css_class("pill")
            remove.add_css_class("destructive-action")
            remove.connect("clicked", self._confirm_remove)
            box.append(duplicate)
            box.append(remove)
            actions.add(box)
            page.add(actions)

        view = Adw.ToolbarView(content=page)
        view.add_top_bar(Adw.HeaderBar())
        self.set_child(view)
        self.connect("hiding", lambda *_a: self.apply())
        if not name:
            # Adding: start with the address; the name follows from the device.
            self.connect("shown", lambda *_a: self.host_row.grab_focus())
        if connected:
            self.device_row.set_subtitle(scanform.device_label(window.device))
        else:
            self._schedule_lookup()

    # --- name and device lookup ----------------------------------------------

    def _name_changed(self, *_args):
        if not self._setting_name:
            self.name_auto = False

    def _set_name(self, text):
        self._setting_name = True
        self.name_row.set_text(text)
        self._setting_name = False

    def _schedule_lookup(self):
        if self._lookup_source:
            GLib.source_remove(self._lookup_source)
            self._lookup_source = 0
        host = self.host_row.get_text().strip()
        if not host:
            self.device_row.set_subtitle(_("No address: found automatically on the network "
                                           "when scanning"))
            return
        self.device_row.set_subtitle(_("Searching…"))
        self._lookup_source = GLib.timeout_add(self.LOOKUP_DELAY_MS, self._lookup, host)

    def _lookup(self, host):
        self._lookup_source = 0
        self._lookup_token += 1
        token = self._lookup_token

        def work():
            device = wsdscan.find_scanner(host)
            try:
                return device, wsdscan.get_capabilities(device["service"])
            except wsdscan.SoapFault as e:
                raise wsdscan.ScanError(str(e))

        def done(result):
            if token != self._lookup_token or self.removed:
                return  # an older lookup, or the page is gone
            device, caps = result
            name = scanform.device_name(device)
            self.device_row.set_subtitle(scanform.device_label(device))
            self.settings.apply_choices(scanform.Choices.from_capabilities(caps))
            if self.name_auto and name:
                others = {n: v for n, v in self.prefs.profiles.items() if n != self.old_name}
                self._set_name(scanform.unique_scanner_name(name, others))

        def failed(error):
            if token == self._lookup_token:
                self.device_row.set_subtitle(_("Not found: {error}").format(error=error))

        run_in_thread(work, done, failed)
        return GLib.SOURCE_REMOVE

    # --- saving into the preferences --------------------------------------------

    def values(self):
        values = dict(self.settings.values(), **self.ocr_rows.values(),
                      host=self.host_row.get_text().strip(), model=self.kept_model,
                      outdir=self.folder.folder, filename=self.filename.get_text().strip()
                      or wsdscan.CONFIG_DEFAULTS["filename"])
        if self.follow_documents and values["outdir"] == scanform.documents_dir():
            values["outdir"] = ""  # keep following the Documents folder
        return values

    def chosen_name(self):
        """The entered name, made valid and unique."""
        others = {n: v for n, v in self.prefs.profiles.items() if n != self.old_name}
        name = self.name_row.get_text().strip()
        try:
            wsdscan.check_scanner_name(name)
        except ValueError:
            name = ""
        return scanform.unique_scanner_name(name or _("Scanner"), others)

    def apply(self):
        if self._lookup_source:
            GLib.source_remove(self._lookup_source)
            self._lookup_source = 0
        self._lookup_token += 1  # ignore lookups still running
        if self.removed:
            return
        name = self.chosen_name()
        self.prefs.update_profile(self.old_name, name, self.values(),
                                  self.default_row.get_active())
        self.old_name = name

    def _duplicate(self, *_args):
        self.apply()
        name = self.old_name
        copy = scanform.unique_scanner_name(name, self.prefs.profiles)
        self.prefs.update_profile(None, copy, dict(self.prefs.profiles[name]), False)
        self.removed = True  # already applied; don't apply again when hidden
        self.prefs.pop_subpage()
        self.prefs.open_scanner(copy)

    def _confirm_remove(self, *_args):
        dialog = Adw.AlertDialog(
            heading=_("Remove “{name}”?").format(name=self.old_name),
            body=_("Its settings are deleted. The scanner itself is not affected."))
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("remove", _("Remove"))
        dialog.set_response_appearance("remove", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.connect("response", self._remove_response)
        dialog.present(self)

    def _remove_response(self, _dialog, response):
        if response == "remove":
            self.removed = True
            self.prefs.remove_profile(self.old_name)
            self.prefs.pop_subpage()


class PreferencesDialog(Adw.PreferencesDialog):
    """Scanners (each with all its settings) and app behavior; saved to config.ini."""

    def __init__(self, window):
        super().__init__(title=_("Preferences"), search_enabled=False)
        self.window = window
        app = window.app
        self.profiles, self.default_name, self.migrated = scanform.migrate_scanners(
            app.shared_config, app.scanners, window.device if not app.scanners else None)
        page = Adw.PreferencesPage()

        self.scanners_group = Adw.PreferencesGroup(title=_("Scanners"))
        find = Gtk.Button(label=_("Find Scanners"), valign=Gtk.Align.CENTER)
        find.connect("clicked", self._find_scanners)
        self.scanners_group.set_header_suffix(find)
        self.scanner_rows: list[Gtk.Widget] = []
        self.open_page: ScannerPage | None = None
        self._rebuild_scanners()
        self.found = Adw.PreferencesGroup(title=_("Scanners on the network"), visible=False)
        self.found_rows: list[Gtk.Widget] = []

        behavior = Adw.PreferencesGroup(title=_("After scanning"))
        self.open_after = Adw.SwitchRow(use_markup=False, title=_("Open PDF after scanning"),
                                        active=app.gui_config["open_after_scan"])
        self.notify_row = Adw.SwitchRow(use_markup=False, title=_("Notify when a scan is done"),
                                        subtitle=_("Only while the window is in the background"),
                                        active=app.gui_config["notify"])
        behavior.add(self.open_after)
        behavior.add(self.notify_row)

        background = Adw.PreferencesGroup(title=_("Status bar"))
        self.tray_row = Adw.SwitchRow(use_markup=False, title=_("Show icon in the status bar"),
                                      active=app.gui_config["tray"])
        if app.status_bar_host_present():
            self.tray_row.set_subtitle(_("Closing the window keeps Scan to PDF running there."))
        else:
            self.tray_row.set_subtitle(
                _("Your desktop shows no status bar icons right now. On GNOME, install the "
                  "“AppIndicator and KStatusNotifierItem Support” extension."))
        self.autostart_row = Adw.SwitchRow(
            use_markup=False, title=_("Start at login"),
            subtitle=_("In the status bar, without opening the window"),
            active=scanform.autostart_enabled())
        self.tray_row.connect("notify::active", lambda *_a: self._sync_background())
        background.add(self.tray_row)
        background.add(self.autostart_row)
        self._sync_background()

        cli = Adw.PreferencesGroup(
            description=_("The scanners and their settings are shared with the wsdscan "
                          "command (--scanner NAME)."))
        # The path goes into a row: rows show plain text, group descriptions may not.
        cli.add(Adw.ActionRow(use_markup=False, title=_("Settings file"),
                              subtitle=scanform.display_path(wsdscan.config_path()),
                              css_classes=["property"]))

        for group in (self.scanners_group, self.found, behavior, background, cli):
            page.add(group)
        self.add(page)
        self.connect("closed", self._save)

    def _sync_background(self):
        tray = self.tray_row.get_active()
        self.autostart_row.set_sensitive(tray)  # starting hidden needs the icon
        if not tray:
            self.autostart_row.set_active(False)

    # --- scanner list -------------------------------------------------------

    def open_scanner(self, name):
        self.open_page = ScannerPage(self, name)
        self.push_subpage(self.open_page)

    def update_profile(self, old_name, new_name, values, make_default):
        """Store a scanner's settings; keeps the list order when renaming."""
        if old_name in self.profiles:
            self.profiles = {(new_name if n == old_name else n): (values if n == old_name else v)
                             for n, v in self.profiles.items()}
        else:
            self.profiles[new_name] = values
        if make_default or self.default_name in (old_name, "") or \
                self.default_name not in self.profiles:
            self.default_name = new_name
        self._rebuild_scanners()

    def remove_profile(self, name):
        self.profiles.pop(name, None)
        self._rebuild_scanners()

    def _rebuild_scanners(self):
        for row in self.scanner_rows:
            self.scanners_group.remove(row)
        self.scanner_rows = []
        if self.profiles and self.default_name not in self.profiles:
            self.default_name = next(iter(self.profiles))
        self.scanners_group.set_description(
            _("Each scanner has its own settings. The marked one is used by default.")
            if self.profiles else
            _("No scanner configured yet. Add one, or let Find Scanners look on the network."))
        leader = None
        for name, values in self.profiles.items():
            row = Adw.ActionRow(use_markup=False, title=name, subtitle=scanform.scanner_subtitle(values),
                                activatable=True)
            row.connect("activated", lambda _r, n=name: self.open_scanner(n))
            radio = Gtk.CheckButton(active=name == self.default_name, valign=Gtk.Align.CENTER,
                                    tooltip_text=_("Use by default"))
            if leader:
                radio.set_group(leader)
            leader = leader or radio
            radio.connect("toggled", self._default_toggled, name)
            row.add_prefix(radio)
            remove = Gtk.Button(icon_name="user-trash-symbolic", tooltip_text=_("Remove"),
                                valign=Gtk.Align.CENTER)
            remove.add_css_class("flat")
            remove.connect("clicked", lambda _b, n=name: self.remove_profile(n))
            row.add_suffix(remove)
            row.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
            self.scanners_group.add(row)
            self.scanner_rows.append(row)
        # Always last in the list: adding a scanner by its address.
        add = Adw.ActionRow(use_markup=False, title=_("Add Scanner by Address…"),
                            subtitle=_("If it is not found on the network"), activatable=True)
        add.add_prefix(Gtk.Image(icon_name="list-add-symbolic"))
        add.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
        add.connect("activated", lambda *_a: self.open_scanner(None))
        self.add_row = add
        self.scanners_group.add(add)
        self.scanner_rows.append(add)

    def _default_toggled(self, radio, name):
        if radio.get_active():
            self.default_name = name

    def _find_scanners(self, button):
        button.set_sensitive(False)
        self.found.set_visible(True)
        self.found.set_description(_("Searching…"))
        for row in self.found_rows:
            self.found.remove(row)
        self.found_rows = []

        def done(devices):
            button.set_sensitive(True)
            self.found.set_description(None)
            known = {v.get("host") for v in self.profiles.values()}
            for device in devices:
                host = wsdscan.urlsplit(device["device_url"]).hostname
                row = Adw.ActionRow(use_markup=False, title=scanform.device_name(device), subtitle=host)
                # A scanner may be added several times, e.g. with different settings.
                add = Gtk.Button(label=_("Add Again") if host in known else _("Add"),
                                 valign=Gtk.Align.CENTER)
                add.connect("clicked", self._add_found, device)
                row.add_suffix(add)
                self.found.add(row)
                self.found_rows.append(row)

        def failed(error):
            button.set_sensitive(True)
            self.found.set_description(None)
            row = Adw.ActionRow(use_markup=False, title=_("No scanner found"), subtitle=str(error))
            self.found.add(row)
            self.found_rows.append(row)

        run_in_thread(lambda: wsdscan.discover(None), done, failed)

    def _add_found(self, button, device):
        name, values = scanform.scanner_from_device(device, self.profiles,
                                                    self.window.app.shared_config)
        self.update_profile(None, name, values, False)
        button.set_label(_("Add Again"))

    def _save(self, *_args):
        if self.open_page:
            self.open_page.apply()  # closed while a scanner page was shown
        app = self.window.app
        old = dict(app.config)  # effective settings before saving
        shared = {"scanner": self.default_name if self.profiles else ""}
        if self.migrated:
            shared.update(host="", model="")  # now part of the first scanner
        tray = self.tray_row.get_active()
        try:
            app.save_config(shared, {"open_after_scan": self.open_after.get_active(),
                                     "notify": self.notify_row.get_active(),
                                     "tray": tray},
                            scanners=self.profiles)
            scanform.set_autostart(tray and self.autostart_row.get_active())
        except (ValueError, OSError) as e:
            self.window.show_error(_("Could not save the preferences: {error}").format(error=e))
            return
        app.set_tray(tray)
        new = app.config
        self.window.apply_preferences(
            scanform.needs_reconnect(old, new, connected=self.window.device is not None)
            or old["scanner"] != new["scanner"])


class ScanApp(Adw.Application):
    def __init__(self):
        super().__init__(application_id=scanform.APP_ID,
                         flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)
        GLib.set_application_name(_(scanform.APP_NAME))
        self.add_main_option("background", 0, GLib.OptionFlags.NONE, GLib.OptionArg.NONE,
                             _("Start in the status bar without opening the window"), None)
        self.config = dict(wsdscan.CONFIG_DEFAULTS)         # effective: [scan] + scanner
        self.shared_config = dict(wsdscan.CONFIG_DEFAULTS)  # [scan] only
        self.scanners = {}                                  # [scanner NAME] profiles
        self.gui_config = dict(scanform.GUI_DEFAULTS)
        self.window = None
        self.ocr = scanform.OcrStatus([], [])
        self.tray = None
        self.session_lock = None
        self.saved_files = set()  # "open-file" may only open these
        self.config_error = None
        self.busy = False

    def do_startup(self):
        Adw.Application.do_startup(self)
        for name, callback, accels in (
                ("preferences", self.on_preferences, ["<Control>comma"]),
                ("about", self.on_about, []),
                ("quit", lambda *_a: self.on_quit(), ["<Control>q"])):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", callback)
            self.add_action(action)
            self.set_accels_for_action(f"app.{name}", accels)
        open_file = Gio.SimpleAction.new("open-file", GLib.VariantType.new("s"))
        open_file.connect("activate", lambda _a, path: self.open_file(path.get_string()))
        self.add_action(open_file)
        display = Gdk.Display.get_default()
        if display:
            Gtk.IconTheme.get_for_display(display).add_search_path(ICONS)
            css = Gtk.CssProvider()
            css.load_from_string(CSS)
            Gtk.StyleContext.add_provider_for_display(display, css,
                                                      Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self.ocr = scanform.OcrStatus.detect()
        try:
            self.load_settings()
        except wsdscan.ScanError as e:
            self.config_error = e  # broken config file: start with defaults, explain
        if self.gui_config["tray"]:
            self.set_tray(True)
        self.set_accels_for_action("win.scan", ["<Control>Return"])
        self.set_accels_for_action("win.cancel", ["Escape"])
        self.set_accels_for_action("win.reconnect", ["F5"])
        self.set_accels_for_action("win.scan-settings", ["<Control>e"])

    def do_command_line(self, command_line):
        background = command_line.get_options_dict().contains("background")
        self.show_ui(background)
        return 0

    def do_activate(self):
        self.show_ui(False)

    def show_ui(self, background):
        first = self.window is None
        window = self.window = self.window or MainWindow(self)
        if background and self.tray:
            # Show the window anyway if no status bar host takes the icon.
            GLib.timeout_add_seconds(3, self._show_if_no_tray)
        else:
            window.present()
        if first and self.config_error:
            window.show_error(_("The settings file could not be read and is ignored "
                                     "until you save the preferences:\n{error}").format(
                                         error=self.config_error))

    def _show_if_no_tray(self):
        locked = self.session_lock and self.session_lock.locked  # shown at unlock instead
        if not self.tray_active() and self.window and not self.window.is_visible() and not locked:
            self.window.present()
        return GLib.SOURCE_REMOVE

    # --- status bar icon -----------------------------------------------------

    def set_tray(self, enabled):
        if enabled and not self.tray:
            import tray  # needs D-Bus; only loaded when used
            connection = self.get_dbus_connection()
            if connection is None:
                return
            self.tray = tray.TrayIcon(connection, self.on_tray_action, self._tray_available)
            self.tray.start()
            self.watch_session_lock()
            self.hold()  # keep running while the window is hidden
        elif not enabled and self.tray:
            self.tray.stop()
            self.tray = None
            if self.session_lock:
                self.session_lock.stop()
                self.session_lock = None
            self.release()
            if self.window and not self.window.is_visible():
                self.window.present()

    def tray_active(self):
        return bool(self.tray and self.tray.available)

    def _tray_available(self, available):
        if not available and self.window and not self.window.is_visible():
            # Never leave the app invisible, but not while the screen is
            # locked: GNOME removes the status bar host then. The lock may be
            # reported shortly after the host is gone, so look in a moment.
            GLib.timeout_add_seconds(3, self._show_if_no_tray)

    def watch_session_lock(self):
        try:
            system_bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
        except GLib.Error:
            return  # no system bus: the window opens whenever the host is gone
        import tray
        self.session_lock = tray.SessionLock(system_bus, self._session_lock_changed)
        self.session_lock.start()

    def _session_lock_changed(self, locked):
        if not locked and self.tray and self.window and not self.window.is_visible():
            # After unlock, give the status bar host time to come back.
            GLib.timeout_add_seconds(3, self._show_if_no_tray)

    def status_bar_host_present(self):
        connection = self.get_dbus_connection()
        if connection is None:
            return False
        try:
            reply = connection.call_sync(
                "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                "NameHasOwner", GLib.Variant("(s)", ("org.kde.StatusNotifierWatcher",)),
                GLib.VariantType.new("(b)"), Gio.DBusCallFlags.NONE, 1000, None)
        except GLib.Error:
            return False
        return bool(reply.unpack()[0])

    def on_tray_action(self, action):
        window = self.window
        if action == "open" and window:
            if window.is_visible() and window.is_active():
                window.close()  # hides it (tray active)
            else:
                window.present()
        elif action == "scan" and window:
            window.activate_action("win.scan", None)
        elif action == "quit":
            self.activate_action("quit", None)

    def set_status(self, text):
        if self.tray:
            self.tray.set_tooltip(text)

    def set_busy(self, busy):
        self.busy = busy
        if self.tray:
            self.tray.set_enabled("scan", not busy)

    def on_quit(self):
        if self.window:
            if self.window.cancel_event:
                self.window.cancel_event.set()
            if self.window.review:
                self.window.finish_review(False)
            width, height = self.window.get_default_size()
            self.save_gui({"width": width, "height": height})
        if self.tray:
            self.tray.stop()
            self.tray = None
        self.quit()

    def load_settings(self):
        """Read the config file: shared defaults, scanners, and the effective
        settings of the scanner used last in the app (else the default one)."""
        self.gui_config = scanform.load_gui_config()
        self.shared_config = wsdscan.load_config(apply_profile=False)
        self.scanners = wsdscan.load_scanners()
        last = self.gui_config["last_scanner"]
        self.config = wsdscan.load_config(scanner=last if last in self.scanners else None)

    def select_scanner(self, name):
        try:
            self.config = wsdscan.load_config(scanner=name)
        except wsdscan.ScanError as e:
            if self.window:
                self.window.show_error(str(e))
            return
        self.gui_config["last_scanner"] = name
        self.save_gui({"last_scanner": name})
        if self.window:
            self.window.apply_preferences(reconnect=True)

    def save_scanner_settings(self, values):
        """Save settings for the current scanner (or as shared defaults without one)."""
        name = self.config["scanner"]
        if not name:
            self.save_config(values, {})
            return
        scanners = {n: scanform.full_profile(self.shared_config, v)
                    for n, v in self.scanners.items()}
        scanners[name].update(values)
        self.save_config({}, {}, scanners=scanners)

    def save_config(self, values, gui, scanners=None):
        wsdscan.save_config(values, sections={"gui": gui}, scanners=scanners)
        self.load_settings()

    def save_gui(self, gui):
        try:
            wsdscan.save_config({}, sections={"gui": gui})
        except (ValueError, OSError, wsdscan.ScanError):
            pass  # window size is not worth an error

    def on_preferences(self, *_args):
        if self.window:
            PreferencesDialog(self.window).present(self.window)

    def on_about(self, *_args):
        about = Adw.AboutDialog(
            application_name=_(scanform.APP_NAME), application_icon=scanform.APP_ID,
            version=scanform.VERSION, developer_name="wsdscan",
            comments=_("Scan documents from WSD network scanners into PDF files."),
            license_type=Gtk.License.AGPL_3_0)
        about.present(self.window)

    def open_file(self, path):
        # The action is reachable over D-Bus by other programs: only open what
        # this app has saved itself.
        if path not in self.saved_files:
            return
        launcher = Gtk.FileLauncher(file=Gio.File.new_for_path(path))
        launcher.launch(self.window, None, None)

    def notify_done(self, message, path):
        note = Gio.Notification.new(_("Scan finished"))
        note.set_body(message)
        note.set_default_action_and_target("app.open-file", GLib.Variant("s", path))
        self.send_notification("scan-done", note)


def check_versions():
    version = (Adw.get_major_version(), Adw.get_minor_version())
    if version < MIN_ADW:
        sys.exit(f"Scan to PDF needs libadwaita {MIN_ADW[0]}.{MIN_ADW[1]} or newer "
                 f"(found {version[0]}.{version[1]}).")


def main():
    GLib.set_prgname(scanform.APP_ID)  # X11 window class = desktop file name
    check_versions()
    return ScanApp().run(sys.argv)


if __name__ == "__main__":
    sys.exit(main())
