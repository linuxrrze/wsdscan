"""Fake `ocrmypdf` and `tesseract` executables for tests.

make_ocr_bin(directory, engines) writes stand-ins that implement just the
command-line interface wsdscan uses. They log their arguments to
$FAKE_OCR_LOG (one JSON list per call) and fail if $FAKE_OCR_FAIL is set.
"""

import json
import os
import sys

TESSERACT = r'''
import json, os, sys
args = sys.argv[1:]
if args == ["--version"]:
    print("tesseract 5.3.4\n leptonica-1.84.1"); sys.exit(0)
if args == ["--list-langs"]:
    print('List of available languages in "/usr/share/tesseract-ocr/5/tessdata/" (3):')
    print("deu\neng\nosd"); sys.exit(0)
with open(os.environ["FAKE_OCR_LOG"], "a") as log:
    pages = open(args[0]).read().split()
    log.write(json.dumps(["tesseract"] + args + ["PAGES"] + pages) + "\n")
print("Tesseract Open Source OCR Engine v5.3.4 with Leptonica", file=sys.stderr)
if os.environ.get("FAKE_OCR_FAIL"):
    print("Error opening data file", file=sys.stderr); sys.exit(1)
for n, page in enumerate(pages, 1):  # like real Tesseract with a file list
    print(f"Page {n} : {page}", file=sys.stderr, flush=True)
lang = args[args.index("-l") + 1]
with open(args[1] + ".pdf", "wb") as f:
    f.write(b"%PDF-1.4 fake tesseract output lang=" + lang.encode()
            + b" pages=" + str(len(pages)).encode() + b"\n")
'''

OCRMYPDF = r'''
import json, os, shutil, sys
args = sys.argv[1:]
if args == ["--version"]:
    print("16.4.2"); sys.exit(0)
with open(os.environ["FAKE_OCR_LOG"], "a") as log:
    log.write(json.dumps(["ocrmypdf"] + args) + "\n")
if os.environ.get("FAKE_OCR_FAIL"):
    print("ERROR - tesseract failed", file=sys.stderr); sys.exit(15)
shutil.copyfile(args[-2], args[-1])
with open(args[-1], "ab") as f:
    f.write(b"%fake ocrmypdf text layer\n")
'''


def make_ocr_bin(directory, engines=("ocrmypdf", "tesseract")):
    """Create fake engines in `directory`; use it as the only PATH entry.

    tesseract is always created when ocrmypdf is (OCRmyPDF needs it).
    """
    os.makedirs(directory, exist_ok=True)
    wanted = set(engines) | ({"tesseract"} if "ocrmypdf" in engines else set())
    for name, body in (("tesseract", TESSERACT), ("ocrmypdf", OCRMYPDF)):
        if name in wanted:
            path = os.path.join(directory, name)
            with open(path, "w") as f:
                f.write(f"#!{sys.executable}\n{body}")
            os.chmod(path, 0o755)
    return directory


def read_log(path):
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return [json.loads(line) for line in f]
