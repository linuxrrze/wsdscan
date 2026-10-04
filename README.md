# wsdscan

Small CLI that scans from a network scanner and saves all pages from the document feeder as one PDF.

It works with any scanner that supports WSD (Microsoft's network scan protocol: WS-Discovery and WS-Scan, SOAP over HTTP) and has a document feeder. It was developed and tested with the **Epson ES-580W**, which has no eSCL/AirScan, so WSD is its only driverless option.

The tool talks to the scanner directly and writes the PDF itself. It needs only Python 3, with no SANE, `scanimage` or extra packages. WSD must be enabled on the scanner, usually in its web configuration.

Only standard WSD features are used. Everything model-dependent (resolutions, color modes, formats, duplex, paper size, brightness/contrast) is read from the scanner at run time.

## Setup

```sh
chmod +x wsdscan.py
export WSDSCAN_HOST=192.168.2.13     # or pass --host each time
./wsdscan.py --info              # capabilities and status
```

### Choosing a scanner

- **`--scanner NAME`** (or `WSDSCAN_SCANNER`): use a scanner configured in the [config file](#configuration-file), e.g. `--scanner Office`. Without it, the default scanner from the config file is used.
- **`--host <ip>`** (or `WSDSCAN_HOST`): use the scanner at that address.
- **Without a host**, the tool searches the network by multicast. If exactly one WSD scanner answers, it's used. If several answer, the tool lists them and asks you to choose with `--host` or `--model`. Multicast usually fails inside Docker containers with bridge networking, so set the host there.
- **`--model <text>`** (or `WSDSCAN_MODEL`): only use a scanner whose manufacturer or model name contains this text, case-insensitively, e.g. `--model ES-580W` or `--model epson`. Together with `--host`, it also checks that the scanner at that address is the expected one.
- **`--list`**: show the scanners found and exit:

```sh
./wsdscan.py --list
192.168.2.13     EPSON ES-580W  (firmware 13.SW19PB, service http://192.168.2.13:80/WDP/SCAN)
```

## Usage

```sh
./wsdscan.py                      # duplex, color, 300 dpi, A4 -> scan_<date>_<time>.pdf
./wsdscan.py invoice.pdf          # explicit file name
./wsdscan.py -s adf -m gray -r 100 letter.pdf
./wsdscan.py -m bw contract.pdf   # black & white: sharp text, small files
./wsdscan.py --lossless photo.pdf # color without JPEG compression
./wsdscan.py --brightness 200 --contrast 100 faint.pdf
./wsdscan.py --ocr letter.pdf         # searchable PDF (needs OCRmyPDF or Tesseract)
./wsdscan.py -p letter --outdir ~/Scans
./wsdscan.py -v                   # show the protocol steps
```

Defaults below are the built-in ones; the [config file](#configuration-file) can change them.

| Option | Default | Meaning |
|---|---|---|
| `--scanner` | `$WSDSCAN_SCANNER` / config | configured scanner to use (see [multiple scanners](#multiple-scanners)) |
| `--host` | `$WSDSCAN_HOST` | scanner IP; multicast discovery if unset |
| `--model` | `$WSDSCAN_MODEL` | only scanners whose manufacturer/model contains this text |
| `--outdir` | current directory | folder for the default file name |
| `-L/--list` | | list the scanners found, then exit |
| `-s/--source` | `duplex` | `adf` (one side) or `duplex` |
| `-m/--mode` | `color` | `color`, `gray` or `bw` (black & white, always lossless) |
| `-l/--lossless` | off | color/gray without JPEG compression (see below); `--no-lossless` turns a configured `lossless = true` off |
| `--brightness N` | scanner default | −1000 … 1000, or `default`; experimental |
| `--contrast N` | scanner default | −1000 … 1000, or `default`; experimental |
| `-r/--resolution` | `300` | dpi; checked against what the scanner reports |
| `-p/--paper` | `a4` | `a4`, `a5`, `letter`, `legal` |
| `-i/--info` | | print capabilities and status, then exit |
| `-c/--check` | | test mode: `--info` plus scanner-side validation of every setting combination; no scan |
| `--ocr` / `--no-ocr` | off | recognize text so the PDF is searchable (see below) |
| `--ocr-engine` | `auto` | `auto` (OCRmyPDF if installed, else Tesseract), `ocrmypdf`, `tesseract` |
| `--ocr-lang` | installed languages (see below) | Tesseract languages, e.g. `deu+eng` |
| `--show-config` | | show the config file location, the effective defaults and the installed OCR engines and languages |
| `-f/--force` | off | overwrite an existing output file |

The tool reads the sheets in the feeder until it's empty.

### Text recognition (OCR)

With `--ocr`, the tool adds an invisible text layer to the PDF, so you can search it and copy text from it. It needs one of these programs, which it detects automatically:

| Engine | Install (Debian/Ubuntu) | How it's used |
|---|---|---|
| [OCRmyPDF](https://ocrmypdf.readthedocs.io/) (preferred) | `sudo apt install ocrmypdf` | Runs on the finished PDF with `--output-type pdf`. The scanned images stay unchanged; no PDF/A conversion. |
| [Tesseract](https://tesseract-ocr.github.io/) | `sudo apt install tesseract-ocr` | Builds the searchable PDF directly from the scanned page images. |

Other distributions: Fedora `dnf install ocrmypdf` / `tesseract`, Arch `pacman -S ocrmypdf` / `tesseract`.

- **Languages:** by default, all installed Tesseract languages, with your system language and English first (e.g. `eng+deu` on an English desktop with `tesseract-ocr-deu` installed). With more than 4 installed (e.g. `tesseract-ocr-all`), only the system language and English are used, since every extra language makes recognition slower and less accurate. `--show-config` shows the installed languages and the automatic choice.
- **Changing the languages:** set `ocr_lang = deu+eng` in the config file, choose them in the desktop app's Preferences, or use `--ocr-lang` for a single run. An empty `ocr_lang` means automatic.
- **Checks before scanning:** if `--ocr` is requested but no engine or a requested language is missing, the tool stops before feeding any paper.
- **If text recognition fails after scanning:** the PDF is kept without text, a warning explains why, and the exit code is `3`.

`~/.config/wsdscan/config.ini` (or `$WSDSCAN_CONFIG`) sets your own defaults. The **Scan to PDF** desktop app edits it in its preferences, so settings made there also apply to this command. You can also edit it by hand:

```ini
[scan]
host = 192.168.2.13
mode = bw
resolution = 300
outdir = ~/Documents/Scans
filename = scan_{date}_{time}.pdf
```

Precedence, highest first:
1. command-line options
2. `WSDSCAN_HOST` / `WSDSCAN_MODEL`
3. the config file
4. built-in defaults

`./wsdscan.py --show-config` shows the result, including the configured scanners.

#### Multiple scanners

Each `[scanner NAME]` section configures one scanner by `host` and/or `model`. It can also set its own defaults for any `[scan]` setting, e.g. black & white for one scanner or a different folder. `scanner =` in `[scan]` names the default:

```ini
[scan]
scanner = Office
mode = color

[scanner Office]
host = 192.168.2.13

[scanner Home]
model = ADS-1700W
mode = bw
outdir = ~/Documents/Home
```

```sh
./wsdscan.py letter.pdf                    # Office, color
./wsdscan.py --scanner Home letter.pdf     # Home, black & white, into ~/Documents/Home
./wsdscan.py --list                        # network scanners; configured ones are marked [Office]
```

Precedence, highest first:
1. command-line options
2. environment variables
3. the selected `[scanner NAME]`
4. `[scan]`
5. built-in defaults

Without `scanner =`, the first configured scanner is the default, unless `[scan]` itself sets `host` or `model`, as in older config files. The desktop app manages this list in its Preferences and stores each scanner with all its settings. The same device can appear several times with different settings. The key `review_pages` belongs to the desktop app; this command ignores it. All keys are described in [gui/README.md](gui/README.md#settings). An invalid value stops the command with the file name and key in the message.

### Image quality and file size

| Mode | Transfer | In the PDF | Rough size per A4 page at 300 dpi (estimate) |
|---|---|---|---|
| `color`, `gray` (default) | JPEG from the scanner | embedded unchanged | a few hundred KB |
| `bw` | 1-bit uncompressed TIFF | 1-bit, zlib (lossless) | about 30–150 KB |
| `color --lossless` | 24-bit uncompressed TIFF (≈ 26 MB) | zlib (lossless) | several MB |
| `gray --lossless` | 8-bit uncompressed TIFF (≈ 9 MB) | zlib (lossless) | a few MB |

Some scanners use a fixed JPEG quality; the ES-580W's is fixed at 50 over WSD. Use `--lossless` when that isn't good enough, for example for photos or very fine print. For text documents, `bw` usually gives the sharpest result and the smallest files.

`--brightness` and `--contrast` are sent as WS-Scan exposure settings. Scanners usually say whether they support them but report no value range; the ES-580W, for example, doesn't. `--check` shows which values it accepts, but whether and how strongly the scanner applies them can only be seen in a test scan.

### Example: what the Epson ES-580W offers over WSD

Measured with `--info` (firmware 13.SW19PB):
- **Resolutions:** only 100 and 300 dpi (no 200 or 600, although the sensor is 600 dpi optical)
- **Color modes:** color, gray and black & white
- **Formats:** JPEG (`exif`, quality fixed at 50) and uncompressed TIFF
- **Paper size:** up to 8.5 × 15.5 in
- **Fixed settings:** automatic paper size off, rotation 0° only, scaling 100% only, content type "Text" only

Exit codes: `0` success, `1` error (nothing saved), `2` the scan stopped partway (for example, a paper jam), and the pages scanned so far were saved, `3` the scan was saved but text recognition failed.

## Test mode

`--check` lists everything the scanner reports, including manufacturer, firmware, serial, status, active conditions (e.g. an empty feeder), formats, color modes, resolutions, optical resolution and paper sizes. Values the tool can't use are marked `(unused)`.

The validation table covers every source × mode × format (JPEG or lossless) × resolution the scanner reports. If the scanner reports brightness and contrast support, `--check` also probes which values (−1000, −500, 0, 500, 1000) it accepts. Finally it validates exactly the settings given on the command line, including `-m`, `--lossless`, `--brightness` and `--contrast`.

It also lists the optional WSD settings the scanner offers. Of these, the tool uses brightness and contrast:
- content type (text/photo)
- automatic paper size
- auto exposure, brightness and contrast
- JPEG quality range
- scaling and rotation

Anything else the scanner reports in that section, including vendor extensions, is listed under `other:`. "not reported" means the scanner didn't mention the setting. `--info` shows the same list without the validation step.

It then asks the scanner to validate a scan ticket for every combination of source, mode and resolution with WS-Scan's `ValidateScanTicket`. No paper is fed and no scan job is created.

```sh
./wsdscan.py --check                       # checks the defaults: duplex, color, 300 dpi, A4
./wsdscan.py --check -s adf -m gray -r 600 # checks specific settings
```

The final line says whether the chosen settings would work. The exit code is `0` if they're usable and `1` if not, so it also works as a health check from scripts or cron. If the scanner doesn't implement `ValidateScanTicket`, the table is skipped and the result is based on the reported capabilities alone.

## Tests

```sh
python3 -m unittest discover -s tests -v
```

Standard library only, about 20 seconds. `tests/fake_wsd.py` is a configurable fake WSD scanner (UDP discovery and SOAP over HTTP on ephemeral localhost ports). The suite covers:

- unit tests for JPEG parsing, PDF writing, SOAP fault and multipart parsing, ticket building and settings validation
- end-to-end runs of the CLI against the fake: duplex, one-sided and gray scans, an empty feeder, a busy scanner, a paper jam, an existing output file, unsupported settings, `--info` and `--check`
- the config file (precedence, validation, saving), progress reporting and cancelling

The tests never read your real config file. The desktop app's tests are in `gui/tests`.

`ES580W_PROFILE` in the test file models the real device: `/WDP/SCAN` service path, only `exif`/`tiff` formats. When the real scanner surprises you, record the new behavior there or as a fake option and add a test.

## Protocol

1. WS-Discovery Probe (UDP 3702) returns the device URL, e.g. `http://<ip>:80/WSD/DEVICE`.
2. WS-Transfer `Get` on that URL returns the scanner service URL.
3. `GetScannerElements` returns capabilities and status.
4. `CreateScanJob` returns a job ID and token.
5. `RetrieveImage` is called repeatedly, returning one JPEG per page side (MTOM multipart), until the fault `ClientErrorNoImagesAvailable` says the feeder is empty.

## Desktop app

`gui/` contains **Scan to PDF**, a GTK 4 / libadwaita desktop app for GNOME, KDE Plasma and Ubuntu. It has a menu entry, desktop notifications, the desktop's own file dialogs, and a preferences dialog for all the settings above. Install it with `gui/install.sh`, which also installs this tool as the `wsdscan` command. See [gui/README.md](gui/README.md).

## License

Copyright (C) 2026 Marcel Ritter

This program is free software: you can redistribute it and/or modify it under the terms of the GNU Affero General Public License as published by the Free Software Foundation, either version 3 of the License, or (at your option) any later version. See [LICENSE](LICENSE).
