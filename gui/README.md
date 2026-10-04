# Scan to PDF (desktop app)

A desktop app for GNOME, KDE Plasma and Ubuntu that scans every sheet in your network scanner's document feeder into one PDF. It's built on `wsdscan.py` and works with any scanner that supports WSD (Web Services on Devices) and has a document feeder, such as the Epson ES-580W.

It uses GTK 4 and libadwaita, so it looks native on GNOME and Ubuntu Desktop. On KDE Plasma it uses KDE's own file dialogs (through desktop portals) and follows the light/dark setting.

| After a scan, with text recognition | Reviewing pages before saving | A scanner's settings |
|---|---|---|
| ![Main window after a scan](../docs/screenshots/main-window.png) | ![Review step with a blank page unticked](../docs/screenshots/review-pages.png) | ![Scanner page in the preferences](../docs/screenshots/scanner-settings.png) |

## Install

**1. Dependencies** (Python 3.9+, GTK 4.12+, libadwaita 1.5+ and their Python bindings; preinstalled on most GNOME and Ubuntu desktops):

| Distribution | Command |
|---|---|
| Ubuntu 24.04+, Debian 13+ | `sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1` |
| Fedora 40+ | `sudo dnf install python3-gobject gtk4 libadwaita` |
| openSUSE Tumbleweed | `sudo zypper install python3-gobject-Gdk typelib-1_0-Gtk-4_0 typelib-1_0-Adw-1` |
| Arch, Manjaro | `sudo pacman -S python-gobject gtk4 libadwaita` |

On KDE, these packages may not be installed yet, even though the GTK libraries themselves often are.

**2. The app**, from this directory:

```sh
./install.sh                 # for you (~/.local)
sudo ./install.sh --system   # for all users (/usr/local)
```

The installer:
- checks the dependencies and prints the right install command if something is missing
- installs the app and the `wsdscan` command
- adds **Scan to PDF** to the application menu (GNOME Activities, KDE launcher, Ubuntu app grid) with its icon
- adds AppStream metadata, so the app is described properly in GNOME Software and KDE Discover

To remove it: `./install.sh --uninstall` (add `--system` if you installed with it). Your settings are kept.

**Installing on another computer:** the installer needs `wsdscan.py` from the repository root. `gui/wsdscan.py` is only a link to it, so copying just the `gui` folder (with `cp -r` or `rsync -a`) leaves it behind. Build a self-contained package instead, and copy that:

```sh
gui/install.sh --dist                  # creates scan-to-pdf-<version>.tar.gz
# on the other computer:
tar -xzf scan-to-pdf-<version>.tar.gz && cd scan-to-pdf-<version> && ./install.sh
```

## Using it

1. Put the pages in the scanner's document feeder.
2. Open **Scan to PDF**. It connects to your scanner and shows it at the top. With several scanners configured, choose one in the **Scanner** selector; the app starts with the one used last. Without any configured, it uses the one it finds on the network.
3. Check the file name and folder. The **Scan settings** row shows the current settings, e.g. "Both sides · Color · 300 dpi · A4". Click it (or press Ctrl+E) to change them in a separate dialog:
   - The dialog has sides, color, resolution, paper size, lossless, brightness/contrast, **Recognize text (OCR)** and **Review pages before saving**. It only offers what your scanner supports.
   - Changes apply to the next scans.
   - **Save for This Scanner** keeps them as the scanner's settings, also for `wsdscan --scanner NAME`. Without a configured scanner the button is **Save as Defaults**.
   - **Reset to Defaults** goes back to the saved settings.
4. Click **Scan** (or press Ctrl+Enter). Each page appears as a thumbnail under **Pages** as soon as it's scanned, with a page counter. **Cancel** (Esc) stops after the current page.
5. With **Review pages before saving** switched on (Scan settings; default off, i.e. every page is processed), the app pauses after the last page:
   - Untick the pages you don't want, e.g. blank backs, then click **Save**.
   - Only the kept pages are saved and passed to text recognition.
   - **Discard Scan** saves nothing.
6. With text recognition on, the thumbnails show the state of each page: waiting, in progress, done (✓). The counter shows "Recognizing text: page 2 of 4". This per-page counter works with **Tesseract**. **OCRmyPDF** doesn't report single pages, so with it the app shows overall progress only and marks all pages done at the end.

When the scan is done:
- A message offers to **Open** the PDF.
- If the window is in the background, a desktop notification appears.
- Existing files are never overwritten; the app saves as `Name (2).pdf` instead.

| Shortcut | Action |
|---|---|
| Ctrl+Enter | Scan |
| Ctrl+E | Scan settings |
| Esc | Cancel the scan |
| F5 | Reconnect to the scanner |
| Ctrl+, | Preferences |
| Ctrl+Q | Quit |

## Text recognition (OCR)

**Recognize text (OCR)** in the Scan settings makes the PDF searchable. The switch only works if [OCRmyPDF](https://ocrmypdf.readthedocs.io/) or [Tesseract](https://tesseract-ocr.github.io/) is installed:

```sh
sudo apt install ocrmypdf            # or: sudo apt install tesseract-ocr
sudo apt install tesseract-ocr-deu   # more languages, e.g. German
```

Otherwise it's greyed out, with a hint on how to install them. The app checks for the tools when it starts, so restart it after installing them.

The switch's subtitle shows the engine and languages that will be used, e.g. "OCRmyPDF · eng+deu".

By default, the app uses all installed languages, with your system language and English first. With more than 4 installed, it uses only the system language and English. To choose yourself, open the scanner's page in **Preferences**, turn off **Choose languages automatically** under Text recognition, and switch the languages on or off. This applies to that scanner, also in the `wsdscan` command. If text recognition fails, the PDF is still saved, without text, and a message explains why.

## Status bar and start at login

In **Preferences → Status bar**:
- **Show icon in the status bar:** Scan to PDF keeps running there when you close the window. Clicking the icon opens or hides the window. Its menu has **Open Scan to PDF**, **Scan** (scans with the current settings, without opening the window) and **Quit**. While scanning, the icon's tooltip shows the progress. When a scan finishes in the background, a notification appears; click it to open the PDF.
- **Start at login:** starts Scan to PDF hidden in the status bar when you log in. It creates the standard autostart entry `~/.config/autostart/io.github.wsdscan.ScanToPdf.desktop` (uninstalling removes it). If no status bar is available at login, the window opens instead, so the app is never invisible.

To quit while the icon is shown, use **Quit** in the icon's menu or the main menu, or Ctrl+Q.

| Desktop | Status bar icon |
|---|---|
| KDE Plasma | built in |
| Ubuntu | built in (the "Ubuntu AppIndicators" extension is enabled by default) |
| Fedora / plain GNOME | needs the [AppIndicator and KStatusNotifierItem Support](https://extensions.gnome.org/extension/615/appindicator-support/) extension; Preferences tells you if no status bar was found |

The icon uses the StatusNotifierItem D-Bus protocol directly, so it needs no extra library.

`wsdscan-gui --background` starts the app in the status bar by hand.

## Settings

Every scanner has **its own settings**. Different scanners can be set up differently, and the same scanner can be configured several times under different names, e.g. "Office color" and "Office b/w".

**Preferences** (Ctrl+,) has these sections:
- **Scanners:** the list of your scanners; the radio button marks the default. Click a scanner to open its page, or click the bin to remove it.
  - **Add Scanner by Address…** (the last row of the list) adds a scanner by its IP address or host name, e.g. if it isn't found on the network. Enter the address; the name ("EPSON ES-580W") is proposed as soon as the scanner answers.
  - **Find Scanners** lists the WSD scanners on the network; **Add** takes one over (**Add Again** for a second configuration of the same device).
  - A scanner set up in an older version becomes the first entry automatically, named after the device, e.g. "EPSON ES-580W".
- **After scanning:** open the PDF, notify when done.
- **Status bar:** see above.

**A scanner's page** has:
- **Scanner:**
  - **IP address or host name** (first): empty = find it automatically on the network. If the scanner's IP address changes (assigned automatically by the router), use its network name instead, e.g. `EPSON1234AB.local`.
  - **Name:** editable. While you haven't typed one, the name of the scanner found at the address ("EPSON ES-580W") is proposed.
  - **Device:** what was found at that address.
  - **Use by default.**
- **Scan settings:** sides, color, resolution, paper size, lossless, brightness/contrast, text recognition on/off, review pages. Once the device has answered, only what it supports is offered.
- **Text recognition (OCR):** engine (Automatic, OCRmyPDF, Tesseract) and languages. **Choose languages automatically** is on by default and shows its current result. Turn it off to pick languages from the installed ones.
- **Saving:** folder, file-name template (`{date}`, `{time}`).
- **Duplicate** (a copy to configure differently) and **Remove Scanner**.

With two or more scanners, the main window shows a **Scanner** selector at the top, with the same names as in the preferences. The app remembers the one used last; the `wsdscan` command uses the default scanner (or `--scanner NAME`).

Changes in the main window's **Scan settings** dialog apply until you close the app. **Save for This Scanner** keeps them as that scanner's settings.

The settings live in `~/.config/wsdscan/config.ini` and are **shared with the `wsdscan` command**:
- `[scanner NAME]`: one section per scanner, with all its settings.
- `[scan]`: names the default scanner. Its settings are used where no scanner is configured, e.g. by `wsdscan --host …`.
- `[gui]`: options that only the app uses.

```ini
[scan]
scanner = Office color

[scanner Office color]
host = 192.168.2.13
mode = color
resolution = 300
ocr = true
outdir = /home/me/Documents/Scans
filename = scan_{date}_{time}.pdf

[scanner Office b/w]
host = 192.168.2.13
mode = bw
review_pages = true

[gui]
open_after_scan = false
notify = true
tray = false
last_scanner = Office color
```

| Key | Values | Notes |
|---|---|---|
| `scanner` | name of a `[scanner NAME]` section | the default scanner; see below |
| `host` | IP address or hostname, or empty | empty = find the scanner automatically |
| `model` | text, or empty | only use scanners whose name contains it; for the `wsdscan` command (`--model`), not shown in the app (an address or host name identifies a scanner better) |
| `source` | `duplex`, `adf` | `adf` = one side |
| `mode` | `color`, `gray`, `bw` | black & white is always lossless |
| `resolution` | dpi, e.g. `300` | the scanner decides which values are allowed |
| `paper` | `a4`, `a5`, `letter`, `legal` | |
| `lossless` | `true`, `false` | color/gray without JPEG compression |
| `brightness`, `contrast` | −1000 … 1000, or `default` | experimental |
| `outdir` | a folder, or empty | empty: the app uses your Documents folder, the command the current directory |
| `filename` | template | `{date}` → `2026-10-03`, `{time}` → `14-30-05` |
| `ocr` | `true`, `false` | recognize text; needs OCRmyPDF or Tesseract |
| `ocr_engine` | `auto`, `ocrmypdf`, `tesseract` | `auto` prefers OCRmyPDF |
| `ocr_lang` | e.g. `deu+eng`, or empty | empty = automatic: all installed languages (system language and English first); only those two if more than 4 are installed |
| `review_pages` | `true`, `false` | app only: review the pages before saving and OCR; the command ignores it |

All keys except `scanner` can be set per scanner in `[scanner NAME]`; a scanner's values take precedence over `[scan]`. See [Multiple scanners](../README.md#multiple-scanners) in the main README.

`[gui]` keys, used only by the app:

| Key | Values | Notes |
|---|---|---|
| `open_after_scan` | `true`, `false` | open the PDF after scanning |
| `notify` | `true`, `false` | notification when a scan finishes in the background |
| `tray` | `true`, `false` | status bar icon |
| `last_scanner` | scanner name | the scanner used last; the app starts with it |
| `width`, `height` | pixels | window size, saved when the window closes |

Start at login is not a config key: it's the autostart entry described above.

## Desktop integration

| | GNOME / Ubuntu | KDE Plasma |
|---|---|---|
| Look | native (libadwaita, Yaru/Adwaita) | GNOME-style, follows light/dark |
| Menu entry and icon | yes | yes |
| File dialog | GNOME | KDE (via xdg-desktop-portal) |
| Notifications | yes | yes |
| Status bar icon | Ubuntu: yes; plain GNOME: with the AppIndicator extension | yes |
| Start at login | yes (XDG autostart) | yes (XDG autostart) |
| Software center listing | GNOME Software (AppStream) | Discover (AppStream) |

On KDE, the app sets `GDK_DEBUG=portals` for itself so GTK uses KDE's dialogs through the portal. Set `GDK_DEBUG` yourself to override this.

## Files

```
gui/
├── wsdscan_gui.py     the app (GTK 4 + libadwaita)
├── scanform.py        GUI logic without GTK (choices per scanner, file names, settings,
│                      status bar menu, autostart entry)
├── tray.py            status bar icon (StatusNotifierItem + dbusmenu over D-Bus)
├── install.sh         installer for users or the whole system; --dist builds a package
├── wsdscan.py         → ../wsdscan.py (link; the installer and --dist copy the real file)
├── data/              .desktop file, AppStream metainfo, app icon, status bar icon
├── tools/             screenshots.py: makes the screenshots in docs/screenshots
└── tests/             tests for the logic and desktop files; GTK and D-Bus tests
```

Screenshots: `docs/screenshots/` is made with `gui/tools/screenshots.py`. It drives the real app against the fake scanner from `tests/`, with document-like pages and fake OCR tools, and needs GTK 4 and a display:

```sh
python3 gui/tools/screenshots.py docs/screenshots
```

Tests, from the repository root:

```sh
python3 -m unittest discover -s gui/tests -v
```

Two of the test files need a real desktop environment:
- `test_gui_gtk.py` opens the real window and scans from a fake scanner, including page preview and review. It's skipped without GTK 4 or a display; on a server it can run under `xvfb-run`.
- `test_tray.py` checks the status bar icon on a private D-Bus with a fake status bar host. It needs PyGObject and `dbus-daemon`.
