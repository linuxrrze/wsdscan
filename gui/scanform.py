"""Toolkit-independent logic of the Scan to PDF app.

Everything here is plain Python so it can be tested without GTK: which
choices a scanner allows, turning form values into scan arguments and the
shared config file, and user-facing progress texts.
"""

import gettext
import os
import shutil
import subprocess
import sys
from types import SimpleNamespace

import wsdscan

_ = gettext.translation("wsdscan", fallback=True).gettext

APP_ID = "io.github.wsdscan.ScanToPdf"
APP_NAME = "Scan to PDF"
VERSION = "1.3.0"

# (value, label) pairs in display order.
SOURCES = [("duplex", _("Both sides")), ("adf", _("One side"))]
MODES = [("color", _("Color")), ("gray", _("Grayscale")), ("bw", _("Black & white"))]
PAPERS = [("a4", "A4"), ("a5", "A5"), ("letter", "Letter"), ("legal", "Legal")]
OCR_ENGINES = [("auto", _("Automatic")), ("ocrmypdf", "OCRmyPDF"), ("tesseract", "Tesseract")]
DEFAULT_RESOLUTIONS = [100, 150, 200, 300, 600]

# [gui] section of the shared config file.
GUI_DEFAULTS = {"open_after_scan": False, "notify": True, "width": 480, "height": 720,
                "tray": False, "last_scanner": ""}


def documents_dir():
    """The user's Documents folder (XDG), falling back to the home folder."""
    try:
        path = subprocess.run(["xdg-user-dir", "DOCUMENTS"], capture_output=True, text=True,
                              timeout=2).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        path = ""
    home = os.path.expanduser("~")
    if path and path != home and os.path.isdir(path):
        return path
    fallback = os.path.join(home, "Documents")
    return fallback if os.path.isdir(fallback) else home


def load_gui_config():
    raw = wsdscan.load_config_section("gui")
    values = dict(GUI_DEFAULTS)
    for key, default in GUI_DEFAULTS.items():
        if key not in raw:
            continue
        text = raw[key].strip().lower()
        if isinstance(default, bool):
            values[key] = text in ("true", "yes", "1", "on")
        elif isinstance(default, str):
            values[key] = raw[key].strip()
        elif text.isdigit():
            values[key] = int(text)
    return values


# Display names for common Tesseract language codes.
LANGUAGE_NAMES = {
    "ara": _("Arabic"), "bul": _("Bulgarian"), "cat": _("Catalan"), "ces": _("Czech"),
    "chi_sim": _("Chinese (simplified)"), "chi_tra": _("Chinese (traditional)"),
    "dan": _("Danish"), "deu": _("German"), "ell": _("Greek"), "eng": _("English"),
    "est": _("Estonian"), "fin": _("Finnish"), "fra": _("French"), "heb": _("Hebrew"),
    "hrv": _("Croatian"), "hun": _("Hungarian"), "ita": _("Italian"), "jpn": _("Japanese"),
    "kor": _("Korean"), "lav": _("Latvian"), "lit": _("Lithuanian"), "nld": _("Dutch"),
    "nor": _("Norwegian"), "pol": _("Polish"), "por": _("Portuguese"), "ron": _("Romanian"),
    "rus": _("Russian"), "slk": _("Slovak"), "slv": _("Slovenian"), "spa": _("Spanish"),
    "srp": _("Serbian"), "swe": _("Swedish"), "tur": _("Turkish"), "ukr": _("Ukrainian"),
}


def language_label(code):
    name = LANGUAGE_NAMES.get(code)
    return f"{name} ({code})" if name else code


class OcrStatus:
    """Which OCR engines and languages are installed (checked once at startup)."""

    INSTALL_HINT = _("Install OCRmyPDF or Tesseract to enable text recognition, "
                     "e.g. “sudo apt install ocrmypdf”.")

    def __init__(self, engines, languages):
        self.engines = engines            # installed engine names, preferred first
        self.languages = languages        # installed Tesseract languages
        self.default_lang = wsdscan.default_ocr_languages(languages)

    @classmethod
    def detect(cls):
        return cls([name for name, _path in wsdscan.ocr_engines()],
                   wsdscan.tesseract_languages())

    @property
    def available(self):
        return bool(self.engines)

    def engine_options(self):
        """Engine choices for the preferences: Automatic plus the installed ones."""
        return [(v, label) for v, label in OCR_ENGINES if v == "auto" or v in self.engines]

    def language_choices(self, configured):
        """(code, label, selected, installed) for the language list in the preferences.

        Ordered like the automatic choice (system language and English first);
        `configured` is the saved 'deu+eng' value ('' = automatic). Configured
        languages that are not installed are listed too, so they can be removed.
        """
        selected = [code for code in (configured or "").split("+") if code]
        order = self.default_lang.split("+") + self.languages + selected
        return [(code, language_label(code), code in selected, code in self.languages)
                for code in dict.fromkeys(order) if code]

    @staticmethod
    def join_languages(choices, selected_codes):
        """The ocr_lang value for the selected codes, in list order ('' if none)."""
        return "+".join(code for code, *_rest in choices if code in selected_codes)

    def describe(self, engine, lang):
        """Subtitle of the OCR switch, e.g. 'OCRmyPDF · deu+eng'."""
        if not self.available:
            return self.INSTALL_HINT
        name = engine if engine in self.engines else self.engines[0]
        return f"{dict(OCR_ENGINES)[name]} · {lang or self.default_lang}"


class Choices:
    """What the form may offer, narrowed down by the scanner's capabilities."""

    def __init__(self, sources, modes, resolutions, lossless, lossless_hint, exposure):
        self.sources = sources            # [(value, label)]
        self.modes = modes                # [(value, label)]
        self.resolutions = resolutions    # [int]
        self.lossless = lossless          # "free", "on" (forced) or "off" (unavailable)
        self.lossless_hint = lossless_hint
        self.exposure = exposure          # brightness/contrast adjustable

    @classmethod
    def unknown(cls):
        """Before the scanner answered: offer everything."""
        return cls(list(SOURCES), list(MODES), list(DEFAULT_RESOLUTIONS), "free", "", True)

    @classmethod
    def from_capabilities(cls, caps):
        formats = caps.get("formats") or []
        has_tiff = not formats or wsdscan.TIFF_FORMAT in formats
        has_jpeg = not formats or any(f in formats for f in wsdscan.JPEG_FORMATS)
        colors = caps.get("colors") or []

        def offered(mode):
            return not colors or wsdscan.COLOR_ENTRIES[mode] in colors

        modes = [(v, label) for v, label in MODES
                 if offered(v) and (has_tiff if v == "bw" else (has_jpeg or has_tiff))]
        sources = [(v, label) for v, label in SOURCES if v != "duplex" or caps.get("duplex")]
        if not has_tiff:
            lossless, hint = "off", _("Not offered by this scanner")
        elif not has_jpeg:
            lossless, hint = "on", _("This scanner only sends uncompressed images")
        else:
            lossless, hint = "free", ""
        ds = caps.get("device_settings") or {}
        exposure = ds.get("brightness") is not False or ds.get("contrast") is not False
        return cls(sources, modes, caps.get("resolutions") or list(DEFAULT_RESOLUTIONS),
                   lossless, hint, exposure)

    def pick(self, options, value):
        """`value` if offered, else the first offered value."""
        values = [v for v, _label in options]
        return value if value in values else (values[0] if values else value)

    def pick_resolution(self, dpi):
        """`dpi` if offered, else the closest offered resolution."""
        if dpi in self.resolutions or not self.resolutions:
            return dpi
        return min(self.resolutions, key=lambda r: (abs(r - dpi), r))

    def lossless_state(self, mode, wanted):
        """(checked, sensitive, hint) for the lossless switch."""
        if mode == "bw":
            return True, False, _("Black & white is always lossless")
        if self.lossless == "off":
            return False, False, self.lossless_hint
        if self.lossless == "on":
            return True, False, self.lossless_hint
        return wanted, True, _("Much larger files; no JPEG compression")


def settings_summary(values, review=False):
    """One line for the main window, e.g. 'Both sides · Color · 300 dpi · A4'."""
    parts = [dict(SOURCES).get(values["source"], values["source"]),
             dict(MODES).get(values["mode"], values["mode"]),
             f"{values['resolution']} dpi",
             dict(PAPERS).get(values["paper"], values["paper"])]
    if values.get("lossless") and values["mode"] != "bw":  # b/w is always lossless
        parts.append(_("lossless"))
    if values.get("brightness") is not None:
        parts.append(_("brightness {n}").format(n=values["brightness"]))
    if values.get("contrast") is not None:
        parts.append(_("contrast {n}").format(n=values["contrast"]))
    if values.get("ocr"):
        parts.append(_("OCR"))
    if review:
        parts.append(_("review pages"))
    return " · ".join(parts)


# --- Scanner profiles ([scanner NAME] sections) --------------------------------

SCANNER_KEYS = [key for key in wsdscan.CONFIG_DEFAULTS if key != "scanner"]


def full_profile(shared, entries):
    """All settings of a scanner: its own values, the rest from [scan].

    The address and model filter are never inherited: they identify the scanner.
    """
    profile = {key: entries.get(key, shared.get(key, wsdscan.CONFIG_DEFAULTS[key]))
               for key in SCANNER_KEYS}
    profile["host"] = entries.get("host", "")
    profile["model"] = entries.get("model", "")
    return profile


def scanner_subtitle(entries):
    """'192.168.2.13', 'Found automatically' or with model filter / own settings."""
    parts = [entries.get("host") or _("Found automatically")]
    if entries.get("model"):
        parts.append(_("model “{model}”").format(model=entries["model"]))
    return " · ".join(parts)


def unique_scanner_name(base, existing):
    """'Office', or 'Office 2', 'Office 3', ... if taken."""
    base = " ".join((base or _("Scanner")).replace("[", "(").replace("]", ")").split())[:60]
    if base not in existing:
        return base
    n = 2
    while f"{base} {n}" in existing:
        n += 1
    return f"{base} {n}"


def scanner_from_device(device, existing, shared):
    """(name, settings) for a scanner found on the network; settings start
    as a copy of the shared defaults."""
    name = unique_scanner_name(device_name(device), existing)
    return name, full_profile(shared, {"host": wsdscan.urlsplit(device["device_url"]).hostname})


def migrate_scanners(shared, scanners, device=None):
    """Scanners for the preferences, each with all its settings.

    Without configured scanners, the [scan] settings (older versions, or just
    the connected scanner) become the first scanner, named after the device
    if one is connected, e.g. 'EPSON ES-580W'.

    Returns (scanners, default_name, migrated).
    """
    scanners = {name: full_profile(shared, entries) for name, entries in scanners.items()}
    if not scanners and (shared.get("host") or shared.get("model") or device):
        entries = {k: shared[k] for k in ("host", "model") if shared.get(k)}
        if device and not entries:
            entries["host"] = wsdscan.urlsplit(device["device_url"]).hostname
        name = unique_scanner_name(
            (device_name(device) if device else "") or shared.get("model") or _("Scanner"),
            scanners)
        return {name: full_profile(shared, entries)}, name, True
    default = wsdscan.default_scanner_name(shared, scanners)
    return scanners, default if default in scanners else "", False


def needs_reconnect(old, new, connected):
    """After saving preferences: look for the scanner again if its address or
    model filter changed, or if no scanner is connected right now."""
    changed = (new.get("host"), new.get("model")) != (old.get("host"), old.get("model"))
    return changed or not connected


def scan_args(values):
    """Form values (as stored in the config) -> arguments for wsdscan.scan_to_file."""
    return SimpleNamespace(
        host=values.get("host") or None,
        model=values.get("model") or None,
        source=values["source"],
        mode=values["mode"],
        resolution=int(values["resolution"]),
        paper=values["paper"],
        lossless=bool(values["lossless"]),
        brightness=values.get("brightness"),
        contrast=values.get("contrast"),
        ocr=bool(values.get("ocr")),
        ocr_engine=values.get("ocr_engine") or "auto",
        ocr_lang=values.get("ocr_lang") or None,
    )


def output_path(folder, name):
    """Absolute, non-existing PDF path for the entered file name (None if empty)."""
    name = (name or "").strip()
    if not name or name in (".pdf", ".PDF"):
        return None
    clean = wsdscan.render_filename(name)
    return wsdscan.unique_path(os.path.join(os.path.expanduser(folder), clean))


def progress_text(event, pages, total=0):
    if event == "connecting":
        return _("Connecting to the scanner…")
    if event == "scanning":
        return _("Scanning…")
    if event == "page":
        return gettext.ngettext("Scanned {n} page", "Scanned {n} pages", pages).format(n=pages)
    if event == "saving":
        return _("Saving PDF…")
    if event == "ocr":
        return _("Recognizing text…")
    if event == "ocr_page":
        return _("Recognizing text: page {n} of {total}").format(n=pages, total=total)
    if event == "review":
        return _("Untick the pages you don’t want, then click Save.")
    return ""


def pages_summary(scanned, kept=None):
    """Header of the page preview, e.g. '4 pages' or '3 of 4 pages kept'."""
    if kept is None or kept == scanned:
        return gettext.ngettext("{n} page", "{n} pages", scanned).format(n=scanned)
    return gettext.ngettext("{kept} of {n} page kept", "{kept} of {n} pages kept",
                            scanned).format(kept=kept, n=scanned)


# --- Status bar menu and autostart ---------------------------------------------

TRAY_MENU = [  # (id, label, action); id 0 is the menu root
    (1, _("Open Scan to PDF"), "open"),
    (2, _("Scan"), "scan"),
    (3, None, None),  # separator
    (4, _("Quit"), "quit"),
]


def tray_menu_layout(enabled):
    """dbusmenu layout as plain Python: (0, root props, [(id, props, [])...]).

    `enabled` maps actions to True/False (e.g. "scan" while scanning).
    """
    children: list[tuple] = []
    for item_id, label, action in TRAY_MENU:
        props: dict[str, object]
        if label is None:
            props = {"type": "separator"}
        else:
            props = {"label": label, "enabled": enabled.get(action, True), "visible": True}
        children.append((item_id, props, []))
    return (0, {"children-display": "submenu"}, children)


def tray_action(item_id):
    return next((action for i, _label, action in TRAY_MENU if i == item_id), None)


def autostart_path():
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "autostart", f"{APP_ID}.desktop")


def launch_command():
    """How to start the app: the installed launcher, else python + this file."""
    launcher = shutil.which("wsdscan-gui")
    if launcher:
        return [launcher]
    return [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                         "wsdscan_gui.py")]


def desktop_exec(args):
    """Quote a command line for an Exec= key (Desktop Entry Specification)."""
    def quote(arg):
        if not any(c in arg for c in ' \t\n"\'\\><~|&;$*?#()`'):
            return arg
        return '"' + "".join("\\" + c if c in '"`$\\' else c for c in arg) + '"'
    return " ".join(quote(a) for a in args)


def autostart_entry(command):
    return "\n".join([
        "[Desktop Entry]",
        "Type=Application",
        f"Name={APP_NAME}",
        "Comment=" + _("Scan to PDF in the status bar"),
        f"Exec={desktop_exec(command + ['--background'])}",
        f"Icon={APP_ID}",
        "Terminal=false",
        "NoDisplay=true",
        "X-GNOME-Autostart-enabled=true",
        "X-KDE-autostart-after=panel",
        "",
    ])


def autostart_enabled():
    return os.path.exists(autostart_path())


def set_autostart(enabled, command=None):
    path = autostart_path()
    if not enabled:
        if os.path.exists(path):
            os.remove(path)
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(autostart_entry(command or launch_command()))


def device_name(device):
    """'EPSON ES-580W': what the scanner calls itself."""
    return f"{device.get('manufacturer') or ''} {device.get('model') or ''}".strip()


def device_label(device):
    """'EPSON ES-580W · 192.168.2.13'."""
    return f"{device_name(device)} · {wsdscan.urlsplit(device['device_url']).hostname}"


def describe_scanner(device, caps, name=""):
    """(title, subtitle) for the main window's scanner row.

    The title is the configured scanner name (as in the preferences), else the
    device name; the subtitle adds the device (if named differently), address,
    state and conditions.
    """
    title = name or device_name(device)
    parts = [device_name(device)] if name and name != device_name(device) else []
    parts.append(wsdscan.urlsplit(device["device_url"]).hostname)
    parts.append(caps.get("state") or "?")
    parts += caps.get("conditions") or []
    return title, " · ".join(parts)
