"""Fake `scanimage` (SANE) for tests.

make_scanimage_bin(directory, config) writes a stand-in for the parts of
scanimage that wsdscan uses: -f (device list), -A -d DEVICE (options) and a
batch scan (--batch=PATTERN --batch-print). config (JSON, in the directory):
  devices  [[name, vendor, model, type]]
  options  {device name: scanimage -A output}
  pages    [image file paths]: written one per page, in order
  exit     exit status after the pages (scanimage: 0, or 7 = feeder empty)
  stderr   printed at the end of a scan
  delay    seconds per page
  extra_lines  printed before the pages (e.g. a file name that is no page)
  switched_off on/off options that -A shows as off although set to yes (as
               epsonds does with adf-crp once scanimage sets the scan area)
Each call is logged to log.json in the directory (one JSON list per line).
"""

import json
import os
import sys

# Real "scanimage -A" output for the Epson ES-580W (sane-backends 1.4.0).
EPSONDS_OPTIONS = """
  Standard:
    --source ADF Front|ADF Duplex [ADF Front]
        Selects the scan source (such as a document-feeder).
    --mode Lineart|Gray|Color [Color]
        Selects the scan mode (e.g., lineart, monochrome, or color).
    --depth 1|8bit [8]
        Number of bits per sample, typical values are 1 for "line-art" and 8
        for multibit scans.
    --resolution 50|75|100|150|200|240|300|360|400|600dpi [50]
        Sets the resolution of the scanned image.
  Geometry:
    -l 0..215.9mm [0]
        Top-left x position of scan area.
    -t 0..393.7mm [0]
        Top-left y position of scan area.
    -x 0..215.9mm [215.9]
        Width of scan-area.
    -y 0..393.7mm [393.7]
        Height of scan-area.
  Optional equipment:
    --eject []
        Eject the sheet in the ADF
    --load [inactive]
        Load a sheet in the ADF
    --adf-skew[=(yes|no)] [no]
        Enables ADF skew correction
    --adf-crp[=(yes|no)] [no]
        Enables ADF auto cropping
"""
AIRSCAN_OPTIONS = """
  Standard:
    --resolution 100|300dpi [300]
        Sets the resolution of the scanned image.
    --mode Color|Gray [Color]
        Selects the scan mode (e.g., lineart, monochrome, or color).
    --scan-intent *unset*|Document [*unset*]
        Optimize scan for Text/Photo/etc.
    --source ADF|ADF Duplex [ADF]
        Selects the scan source (such as a document-feeder).
  Geometry:
    -l 0..215.9mm [0]
        Top-left x position of scan area.
    -t 0..393.7mm [0]
        Top-left y position of scan area.
    -x 0..215.9mm [215.9]
        Width of scan-area.
    -y 0..393.7mm [393.7]
        Height of scan-area.
  Enhancement:
    --brightness -100..100% (in steps of 1) [0]
        Controls the brightness of the acquired image.
    --contrast -100..100% (in steps of 1) [0]
        Controls the contrast of the acquired image.
    --shadow 0..100% (in steps of 1) [0]
        Selects what radiance level should be considered "black".
    --highlight 0..100% (in steps of 1) [100]
        Selects what radiance level should be considered "white".
    --analog-gamma 0.0999908..4 [1]
        Analog gamma-correction
    --negative[=(yes|no)] [no]
        Swap black and white
    --adf-justification-x <string> [inactive]
        ADF width justification (left/right/center)
    --adf-justification-y <string> [inactive]
        ADF height justification (top/bottom/center)
"""

SCANIMAGE = r'''
import json, os, re, signal, sys, time
here = os.path.dirname(os.path.abspath(__file__))
config = json.load(open(os.path.join(here, "fake-sane.json")))
args = sys.argv[1:]
with open(os.path.join(here, "log.json"), "a") as log:
    log.write(json.dumps(args) + "\n")
if args[:1] == ["-f"]:
    for name, vendor, model, kind in config.get("devices", []):
        print(args[1].replace("%d", name).replace("%v", vendor).replace("%m", model)
              .replace("%t", kind).replace("%n", "\n"), end="")
    sys.exit(0)
device = args[args.index("-d") + 1] if "-d" in args else None
if "-A" in args:
    if device not in config.get("options", {}):
        print(f"scanimage: open of device {device} failed: Invalid argument", file=sys.stderr)
        sys.exit(1)
    print("Output format is not set, using pnm as a default.", file=sys.stderr)
    print(f"\nAll options specific to device `{device}':", end="")
    text = config["options"][device]
    for arg in args:
        name = re.fullmatch(r"--([\w-]+)=yes", arg)
        if name and name.group(1) not in config.get("switched_off", []):
            text = re.sub(rf"(--{re.escape(name.group(1))}\[=\(yes\|no\)\]) \[no\]",
                          r"\1 [yes]", text)
    print(text)
    sys.exit(0)
signal.signal(signal.SIGINT, lambda *a: (print("scanimage: received signal 2", file=sys.stderr),
                                         sys.exit(1)))
pattern = next(a.split("=", 1)[1] for a in args if a.startswith("--batch="))
print("Scanning infinity pages, incrementing by 1, numbering from 1", file=sys.stderr)
for line in config.get("extra_lines", []):
    print(line, flush=True)
for n, page in enumerate(config.get("pages", []), 1):
    time.sleep(config.get("delay", 0))
    print(f"Scanning page {n}", file=sys.stderr)
    with open(pattern % n, "wb") as out, open(page, "rb") as f:
        out.write(f.read())
    print(pattern % n, flush=True)
time.sleep(config.get("delay", 0))
if config.get("stderr"):
    print(config["stderr"], file=sys.stderr)
print(f"Batch terminated, {len(config.get('pages', []))} pages scanned", file=sys.stderr)
sys.exit(config.get("exit", 0))
'''


def make_scanimage_bin(directory, **config):
    """Create a fake scanimage in directory (use it as the PATH); returns it."""
    os.makedirs(directory, exist_ok=True)
    configure(directory, **config)
    path = os.path.join(directory, "scanimage")
    with open(path, "w") as f:
        f.write(f"#!{sys.executable}\n{SCANIMAGE}")
    os.chmod(path, 0o755)
    return directory


def configure(directory, **config):
    """Change what the fake scanimage in directory does."""
    with open(os.path.join(directory, "fake-sane.json"), "w") as f:
        json.dump(config, f)


def read_log(directory):
    path = os.path.join(directory, "log.json")
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return [json.loads(line) for line in f]
