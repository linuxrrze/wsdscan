#!/usr/bin/env python3
"""Scan from a WSD network scanner and save the pages as one PDF.

Works with scanners that support Microsoft's WSD scan protocol (WS-Scan) and
have a document feeder. Developed and tested with the Epson ES-580W.

Talks WSD (WS-Discovery + WS-Scan, SOAP over HTTP) directly to the scanner and
writes the PDF itself, so it needs nothing beyond the Python 3 standard
library: no SANE, no scanimage, no img2pdf.

Protocol flow:
  1. WS-Discovery Probe (UDP 3702)     -> device UUID + device URL
  2. WS-Transfer Get on the device URL -> URL of the hosted scanner service
  3. GetScannerElements                -> capabilities and status
  4. CreateScanJob                     -> JobId + JobToken
  5. RetrieveImage, repeatedly         -> one JPEG per page side, until the
                                          feeder is empty
"""

import argparse
import configparser
import datetime
import getpass
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
import xml.etree.ElementTree as ET
import zlib
from urllib.parse import urlsplit
from xml.sax.saxutils import escape

WSD_PORT = 3702
WSD_MULTICAST = "239.255.255.250"

NS = {
    "soap": "http://www.w3.org/2003/05/soap-envelope",
    "wsa": "http://schemas.xmlsoap.org/ws/2004/08/addressing",
    "wsd": "http://schemas.xmlsoap.org/ws/2005/04/discovery",
    "wsdp": "http://schemas.xmlsoap.org/ws/2006/02/devprof",
    "wscn": "http://schemas.microsoft.com/windows/2006/08/wdp/scan",
}
ANONYMOUS = "http://schemas.xmlsoap.org/ws/2004/08/addressing/role/anonymous"
ACTION_PROBE = "http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe"
ACTION_GET = "http://schemas.xmlsoap.org/ws/2004/09/transfer/Get"
SCAN_ACTION = NS["wscn"] + "/"

COLOR_ENTRIES = {"color": "RGB24", "gray": "Grayscale8", "bw": "BlackAndWhite1"}
# WS-Scan formats that deliver a plain JPEG stream ("exif" = JPEG with an
# EXIF header), in order of preference.
JPEG_FORMATS = ("jfif", "exif")
# Lossless transfer, used for black & white (JPEG cannot hold 1-bit images)
# and for --lossless color/gray. Compressed with zlib when writing the PDF.
TIFF_FORMAT = "tiff-single-uncompressed"
# WS-Scan ExposureSettings range for brightness and contrast.
EXPOSURE_RANGE = (-1000, 1000)
EXPOSURE_PROBE = (-1000, -500, 0, 500, 1000)
# Paper sizes in 1/1000 inch, the unit WS-Scan uses.
PAPER_SIZES = {
    "a4": (8268, 11693),
    "a5": (5827, 8268),
    "letter": (8500, 11000),
    "legal": (8500, 14000),
}

# Faults that mean "try again shortly" rather than "give up".
RETRY_FAULTS = ("ServerErrorTemporaryError", "ServerErrorNotAcceptingJobs", "Busy")
# Faults meaning the scanner does not implement the requested operation.
UNSUPPORTED_FAULTS = ("ActionNotSupported", "UnknownAction", "OperationNotSupported",
                      "UnsupportedOperation")
RETRY_COUNT = 10
RETRY_DELAY = 1.0
TIMEOUT = 10
SCAN_TIMEOUT = 120  # RetrieveImage blocks while the page is being scanned

verbose = False


class SoapFault(Exception):
    def __init__(self, code, reason=""):
        super().__init__(f"{code}: {reason}" if reason else code)
        self.code = code


class ScanError(Exception):
    """A user-facing error; the CLI prints it and exits with status 1."""


class ScanCancelled(ScanError):
    """The scan was stopped on request (should_stop returned True)."""


def die(msg):
    raise ScanError(msg)


def log(msg):
    if verbose:
        print(msg, file=sys.stderr)


def q(tag):
    """'wscn:Foo' -> '{namespace}Foo' for ElementTree lookups."""
    prefix, name = tag.split(":")
    return f"{{{NS[prefix]}}}{name}"


def find_text(root, tag, default=None):
    el = root.find(f".//{q(tag)}")
    return el.text.strip() if el is not None and el.text else default


def find_all_text(root, tag):
    return [el.text.strip() for el in root.iter(q(tag)) if el.text]


def envelope(to, action, body):
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        f'<soap:Envelope xmlns:soap="{NS["soap"]}" xmlns:wsa="{NS["wsa"]}"'
        f' xmlns:wsd="{NS["wsd"]}" xmlns:wscn="{NS["wscn"]}">'
        "<soap:Header>"
        f"<wsa:To>{escape(to)}</wsa:To>"
        f"<wsa:Action>{escape(action)}</wsa:Action>"
        f"<wsa:MessageID>urn:uuid:{uuid.uuid4()}</wsa:MessageID>"
        f"<wsa:ReplyTo><wsa:Address>{ANONYMOUS}</wsa:Address></wsa:ReplyTo>"
        "</soap:Header>"
        f"<soap:Body>{body}</soap:Body>"
        "</soap:Envelope>"
    ).encode()


def parse_fault(root):
    fault = root.find(f".//{q('soap:Fault')}")
    if fault is None:
        return None
    # The most specific (innermost) Subcode carries the WS-Scan error name.
    values = [v.text.strip() for v in fault.iter(q("soap:Value")) if v.text]
    code = values[-1].split(":")[-1] if values else "UnknownFault"
    return SoapFault(code, find_text(fault, "soap:Text", ""))


# --- HTTP / SOAP ------------------------------------------------------------

def soap_call(url, to, action, body="", timeout=TIMEOUT):
    """POST a SOAP request. Returns (xml_root, attachment_bytes_or_None)."""
    log(f"> {action.rsplit('/', 1)[-1]} -> {url}")
    req = urllib.request.Request(
        url, data=envelope(to, action, body),
        headers={"Content-Type": "application/soap+xml; charset=utf-8"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            ctype, data = resp.headers.get("Content-Type", ""), resp.read()
    except urllib.error.HTTPError as e:
        with e:
            ctype, data = e.headers.get("Content-Type", ""), e.read()
        try:
            fault = parse_fault(ET.fromstring(data))
        except ET.ParseError:
            fault = None
        raise fault or SoapFault(f"HTTP{e.code}", e.reason)
    except (urllib.error.URLError, OSError) as e:
        die(f"cannot reach scanner at {url}: {e}")

    attachment = None
    if ctype.lower().startswith("multipart/"):
        data, attachment = split_multipart(ctype, data)
    root = ET.fromstring(data)
    fault = parse_fault(root)
    if fault:
        raise fault
    return root, attachment


def split_multipart(ctype, data):
    """Split an MTOM/XOP multipart response into (soap_xml, binary_part)."""
    m = re.search(r'boundary="?([^";]+)"?', ctype, re.I)
    if not m:
        die("multipart response without boundary")
    xml_part, binary = None, None
    for chunk in data.split(b"--" + m.group(1).encode())[1:]:
        if chunk.startswith(b"--"):
            break  # closing boundary
        head, sep, body = chunk.partition(b"\r\n\r\n")
        if not sep:
            continue
        if body.endswith(b"\r\n"):
            body = body[:-2]
        if xml_part is None and b"xml" in head.lower():
            xml_part = body
        else:
            binary = body
    if xml_part is None:
        die("multipart response without SOAP part")
    return xml_part, binary


# --- Discovery and capabilities ----------------------------------------------

def probe(host, timeout=3.0):
    """WS-Discovery Probe. Returns [(endpoint_uuid, [device_urls])] for scanners."""
    if host:
        name, _, port = host.partition(":")
        target = (name, int(port or WSD_PORT))
    else:
        target = (WSD_MULTICAST, WSD_PORT)
    msg = envelope("urn:schemas-xmlsoap-org:ws:2005:04:discovery", ACTION_PROBE,
                   "<wsd:Probe/>")
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    sock.settimeout(timeout)
    log(f"> Probe -> {target[0]}:{target[1]}")
    found = {}
    try:
        sock.sendto(msg, target)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            sock.settimeout(max(0.1, deadline - time.monotonic()))
            try:
                data, _ = sock.recvfrom(65535)
            except socket.timeout:
                break
            try:
                root = ET.fromstring(data)
            except ET.ParseError:
                continue
            for match in root.iter(q("wsd:ProbeMatch")):
                if "ScanDeviceType" not in (find_text(match, "wsd:Types") or ""):
                    continue
                addr = find_text(match, "wsa:Address")
                xaddrs = (find_text(match, "wsd:XAddrs") or "").split()
                if addr and xaddrs:
                    found[addr] = xaddrs
            if host and found:
                break
    finally:
        sock.close()
    return list(found.items())


def get_device(endpoint, xaddrs):
    """WS-Transfer Get. Returns a dict with model metadata and the scan service URL."""
    for url in xaddrs:
        try:
            root, _ = soap_call(url, endpoint, ACTION_GET)
        except SoapFault as e:
            log(f"  metadata from {url} failed: {e}")
            continue
        device = {
            "manufacturer": find_text(root, "wsdp:Manufacturer", "?"),
            "model": find_text(root, "wsdp:ModelName", "?"),
            "firmware": find_text(root, "wsdp:FirmwareVersion", "?"),
            "serial": find_text(root, "wsdp:SerialNumber", "?"),
            "device_url": url,
            "service": None,
        }
        for hosted in root.iter(q("wsdp:Hosted")):
            if "ScannerServiceType" in (find_text(hosted, "wsdp:Types") or ""):
                device["service"] = find_text(hosted, "wsa:Address")
                break
        return device
    return None


def discover(host):
    """All WSD scanners that answer, with their metadata. Dies if none does."""
    devices = probe(host)
    if not devices:
        where = host or "the local network (multicast)"
        die(f"no WSD scanner answered on {where}. Check that WSD is enabled on "
            "the scanner" + ("" if host else ", or pass --host <ip>"))
    found = []
    for endpoint, xaddrs in devices:
        device = get_device(endpoint, xaddrs)
        if device and device["service"]:
            log(f"  found {describe_device(device)}")
            found.append(device)
    if not found:
        die(f"none of the {len(devices)} WSD device(s) offers a scan service")
    return found


def describe_device(device):
    return (f"{device['manufacturer']} {device['model']} "
            f"at {urlsplit(device['device_url']).hostname}")


def model_matches(device, model):
    """Case-insensitive substring match on manufacturer and model name."""
    return not model or model.lower() in f"{device['manufacturer']} {device['model']}".lower()


def find_scanner(host, model=None):
    """Pick the scanner to use: the one at `host`, or the only one found by
    multicast discovery, optionally narrowed down by `model`."""
    found = discover(host)
    matches = [d for d in found if model_matches(d, model)]
    if not matches:
        die(f"no scanner matches --model {model!r}; found: "
            + "; ".join(describe_device(d) for d in found))
    if len(matches) > 1 and not host:
        hint = "pass --host <ip>" + ("" if model else " or --model <name>")
        die(f"{len(matches)} scanners found ("
            + "; ".join(describe_device(d) for d in matches) + f"); {hint}")
    return matches[0]


def list_scanners(host, model=None):
    found = [d for d in discover(host) if model_matches(d, model)]
    if not found:
        die(f"no scanner matches --model {model!r}")
    configured = {}
    for name, entries in load_scanners().items():
        if entries.get("host"):
            configured.setdefault(entries["host"], []).append(name)
    for d in found:
        ip = urlsplit(d["device_url"]).hostname
        names = configured.get(ip, [])
        print(f"{ip:<16} {d['manufacturer']} {d['model']}"
              f"  (firmware {d['firmware']}, service {d['service']})"
              + (f"  [{', '.join(names)}]" if names else ""))


def parse_size(el):
    if el is None:
        return None
    w, h = find_text(el, "wscn:Width", ""), find_text(el, "wscn:Height", "")
    return (int(w), int(h)) if w.isdigit() and h.isdigit() else None


def parse_side(side):
    """Colors and resolutions of an ADFFront/ADFBack element."""
    if side is None:
        return [], []
    colors = find_all_text(side, "wscn:ColorEntry")
    widths = side.find(f".//{q('wscn:Widths')}")
    resolutions = sorted({int(v) for v in find_all_text(widths, "wscn:Width")
                          if v.isdigit()}) if widths is not None else []
    return colors, resolutions


# DeviceSettings children described individually; anything else the scanner
# reports is listed generically, so nothing is hidden.
KNOWN_DEVICE_SETTINGS = {
    "FormatsSupported", "CompressionQualityFactorSupported", "ContentTypesSupported",
    "DocumentSizeAutoDetectSupported", "AutoExposureSupported", "BrightnessSupported",
    "ContrastSupported", "ScalingRangeSupported", "RotationsSupported",
}


def local_name(tag):
    return tag.rsplit("}", 1)[-1]


def parse_flag(parent, tag):
    """True/False for a boolean child element, None if not reported."""
    el = parent.find(q(tag))
    if el is None or not el.text:
        return None
    return el.text.strip().lower() in ("true", "1")


def parse_range(el):
    """(min, max) from MinValue/MaxValue children, None if not reported."""
    if el is None:
        return None
    lo, hi = find_text(el, "wscn:MinValue", ""), find_text(el, "wscn:MaxValue", "")
    if lo.lstrip("-").isdigit() and hi.lstrip("-").isdigit():
        return int(lo), int(hi)
    return None


def describe_element(el):
    """Flatten an unknown element into 'Leaf=value, ...' text."""
    leaves = [x for x in el.iter() if len(x) == 0 and x.text and x.text.strip()]
    if len(leaves) == 1 and leaves[0] is el:
        return el.text.strip()
    values = {}
    for leaf in leaves:
        values.setdefault(local_name(leaf.tag), []).append(leaf.text.strip())
    return ", ".join(f"{name}={'/'.join(v)}" for name, v in values.items()) or "(empty)"


def parse_device_settings(root):
    """Optional scan settings from ScannerConfiguration/DeviceSettings."""
    ds = root.find(f".//{q('wscn:DeviceSettings')}")
    if ds is None:
        return None
    scaling = ds.find(q("wscn:ScalingRangeSupported"))
    return {
        "content_types": find_all_text(ds, "wscn:ContentTypeValue"),
        "auto_size": parse_flag(ds, "wscn:DocumentSizeAutoDetectSupported"),
        "auto_exposure": parse_flag(ds, "wscn:AutoExposureSupported"),
        "brightness": parse_flag(ds, "wscn:BrightnessSupported"),
        "contrast": parse_flag(ds, "wscn:ContrastSupported"),
        "quality": parse_range(ds.find(q("wscn:CompressionQualityFactorSupported"))),
        "scaling": None if scaling is None else (
            parse_range(scaling.find(q("wscn:ScalingWidth"))),
            parse_range(scaling.find(q("wscn:ScalingHeight")))),
        "rotations": find_all_text(ds, "wscn:RotationValue"),
        "other": [(local_name(child.tag), describe_element(child)) for child in ds
                  if local_name(child.tag) not in KNOWN_DEVICE_SETTINGS],
    }


def parse_conditions(root):
    """Active device conditions, e.g. 'InputTrayEmpty (Informational)'."""
    conditions = []
    for cond in root.iter(q("wscn:DeviceCondition")):
        name = find_text(cond, "wscn:Name", "?")
        details = [v for v in (find_text(cond, "wscn:Component"),
                               find_text(cond, "wscn:Severity")) if v]
        conditions.append(f"{name} ({', '.join(details)})" if details else name)
    return conditions


def parse_capabilities(root):
    adf = root.find(f".//{q('wscn:ADF')}")
    front = adf.find(q("wscn:ADFFront")) if adf is not None else None
    back = adf.find(q("wscn:ADFBack")) if adf is not None else None
    colors, resolutions = parse_side(front)
    back_colors, back_resolutions = parse_side(back)
    return {
        "state": find_text(root, "wscn:ScannerState", "?"),
        "reasons": find_all_text(root, "wscn:ScannerStateReason"),
        "formats": find_all_text(root, "wscn:FormatValue"),
        "has_adf": adf is not None,
        "has_platen": root.find(f".//{q('wscn:Platen')}") is not None,
        "duplex": (find_text(adf, "wscn:ADFSupportsDuplex", "") in ("true", "1"))
                  if adf is not None else False,
        "colors": colors,
        "resolutions": resolutions,
        "back_colors": back_colors,
        "back_resolutions": back_resolutions,
        "min_size": parse_size(front.find(q("wscn:ADFMinimumSize"))) if front is not None else None,
        "max_size": parse_size(front.find(q("wscn:ADFMaximumSize"))) if front is not None else None,
        "optical_resolution": parse_size(front.find(q("wscn:ADFOpticalResolution")))
                              if front is not None else None,
        "description": {
            "name": find_text(root, "wscn:ScannerName"),
            "info": find_text(root, "wscn:ScannerInfo"),
            "location": find_text(root, "wscn:ScannerLocation"),
        },
        "conditions": parse_conditions(root),
        "device_settings": parse_device_settings(root),
    }


def get_capabilities(service):
    body = ("<wscn:GetScannerElementsRequest><wscn:RequestedElements>"
            "<wscn:Name>wscn:ScannerDescription</wscn:Name>"
            "<wscn:Name>wscn:ScannerConfiguration</wscn:Name>"
            "<wscn:Name>wscn:ScannerStatus</wscn:Name>"
            "</wscn:RequestedElements></wscn:GetScannerElementsRequest>")
    root, _ = soap_call(service, service, SCAN_ACTION + "GetScannerElements", body)
    return parse_capabilities(root)


def fmt_size(size):
    return f"{size[0] / 1000:.2f} x {size[1] / 1000:.2f} in" if size else "?"


def print_info(device, caps):
    usable_colors = set(COLOR_ENTRIES.values())

    def mark(values, usable):
        """List values, flagging the ones this tool cannot use."""
        if not values:
            return "?"
        return ", ".join(v if v in usable else f"{v} (unused)" for v in values)

    rows = [
        ("manufacturer", device["manufacturer"]),
        ("model", device["model"]),
        ("firmware", device["firmware"]),
        ("serial", device["serial"]),
        ("device URL", device["device_url"]),
        ("scan service", device["service"]),
        ("state", f"{caps['state']} {' '.join(caps['reasons'])}".rstrip()),
        ("feeder", ("yes, duplex" if caps["duplex"] else "yes, one-sided")
                   if caps["has_adf"] else "no"),
        ("flatbed", "yes" if caps["has_platen"] else "no"),
        ("formats", mark(caps["formats"], JPEG_FORMATS + (TIFF_FORMAT,))),
        ("colors", mark(caps["colors"], usable_colors)),
        ("resolutions", ", ".join(map(str, caps["resolutions"])) or "?"),
    ]
    if caps["duplex"] and (caps["back_colors"] != caps["colors"]
                           or caps["back_resolutions"] != caps["resolutions"]):
        rows += [("back colors", mark(caps["back_colors"], usable_colors)),
                 ("back resolutions", ", ".join(map(str, caps["back_resolutions"])) or "?")]
    rows += [("min size", fmt_size(caps["min_size"])),
             ("max size", fmt_size(caps["max_size"]))]
    if caps["optical_resolution"]:
        rows.append(("optical res.", "{} x {} dpi".format(*caps["optical_resolution"])))
    for key in ("name", "info", "location"):
        if caps["description"][key]:
            rows.append((f"scanner {key}", caps["description"][key]))
    rows.append(("conditions", ", ".join(caps["conditions"]) or "none"))
    for label, value in rows:
        print(f"{label + ':':<18}{value}")
    print_device_settings(caps["device_settings"])


def print_device_settings(ds):
    print()
    print("Optional settings reported by the scanner "
          "(this tool uses brightness and contrast):")
    if ds is None:
        print("  (none: the scanner reports no DeviceSettings)")
        return

    def flag(value):
        return {True: "yes", False: "no", None: "not reported"}[value]

    def span(rng, unit=""):
        return f"{rng[0]}-{rng[1]}{unit}" if rng else "not reported"

    scaling = "not reported"
    if ds["scaling"]:
        width, height = ds["scaling"]
        scaling = f"width {span(width, ' %')}, height {span(height, ' %')}"
    rows = [
        ("content types", ", ".join(ds["content_types"]) or "not reported"),
        ("auto paper size", flag(ds["auto_size"])),
        ("auto exposure", flag(ds["auto_exposure"])),
        ("brightness", flag(ds["brightness"])),
        ("contrast", flag(ds["contrast"])),
        ("JPEG quality", span(ds["quality"])),
        ("scaling", scaling),
        ("rotations", ", ".join(ds["rotations"]) or "not reported"),
    ]
    rows += [(f"other: {name}", value) for name, value in ds["other"]]
    for label, value in rows:
        print(f"  {label + ':':<17} {value}")


# --- Scanning ----------------------------------------------------------------

def uses_tiff(mode, lossless):
    return mode == "bw" or lossless


def settings_problems(args, caps):
    """Reasons why the scanner cannot do what args ask for. Unknown = allowed."""
    problems = []
    if not caps["has_adf"]:
        problems.append("scanner reports no document feeder")
    if args.source == "duplex" and not caps["duplex"]:
        problems.append("scanner reports no duplex support; use --source adf")
    color = COLOR_ENTRIES[args.mode]
    if caps["colors"] and color not in caps["colors"]:
        problems.append(f"mode {args.mode} ({color}) not supported; scanner offers "
                        f"{', '.join(caps['colors'])}")
    if caps["resolutions"] and args.resolution not in caps["resolutions"]:
        problems.append(f"resolution {args.resolution} not supported; choose one of "
                        f"{', '.join(map(str, caps['resolutions']))}")
    if uses_tiff(args.mode, args.lossless):
        if caps["formats"] and TIFF_FORMAT not in caps["formats"]:
            why = "black & white" if args.mode == "bw" else "--lossless"
            problems.append(f"{why} needs the {TIFF_FORMAT} format; scanner offers "
                            f"{', '.join(caps['formats'])}")
    elif caps["formats"] and not any(f in caps["formats"] for f in JPEG_FORMATS):
        problems.append(f"scanner offers no JPEG format ({', '.join(JPEG_FORMATS)}); "
                        f"formats: {', '.join(caps['formats'])}"
                        + ("; try --lossless" if TIFF_FORMAT in caps["formats"] else ""))
    ds = caps.get("device_settings") or {}
    for name in ("brightness", "contrast"):
        if getattr(args, name) is not None and ds.get(name) is False:
            problems.append(f"scanner reports no {name} support")
    return problems


def validate(args, caps):
    problems = settings_problems(args, caps)
    if problems:
        die("; ".join(problems))


def pick_format(caps, mode="color", lossless=False):
    if uses_tiff(mode, lossless):
        return TIFF_FORMAT
    for fmt in JPEG_FORMATS:
        if fmt in caps["formats"]:
            return fmt
    return JPEG_FORMATS[0]  # scanner did not list formats; try the default


def scan_ticket(source, mode, resolution, paper, caps, lossless=False,
                brightness=None, contrast=None):
    """Build a <wscn:ScanTicket> for CreateScanJob and ValidateScanTicket."""
    width, height = PAPER_SIZES[paper]
    if caps["max_size"]:
        width, height = min(width, caps["max_size"][0]), min(height, caps["max_size"][1])
    side = (f"<wscn:ColorProcessing>{COLOR_ENTRIES[mode]}</wscn:ColorProcessing>"
            f"<wscn:Resolution><wscn:Width>{resolution}</wscn:Width>"
            f"<wscn:Height>{resolution}</wscn:Height></wscn:Resolution>")
    duplex = source == "duplex"
    sides = f"<wscn:MediaFront>{side}</wscn:MediaFront>"
    if duplex:
        sides += f"<wscn:MediaBack>{side}</wscn:MediaBack>"
    exposure = ""
    if brightness is not None or contrast is not None:
        exposure = (
            "<wscn:Exposure><wscn:ExposureSettings>"
            + (f"<wscn:Contrast>{contrast}</wscn:Contrast>" if contrast is not None else "")
            + (f"<wscn:Brightness>{brightness}</wscn:Brightness>" if brightness is not None else "")
            + "</wscn:ExposureSettings></wscn:Exposure>")
    try:
        user = getpass.getuser()
    except Exception:
        user = "wsdscan"
    return (
        "<wscn:ScanTicket>"
        "<wscn:JobDescription>"
        "<wscn:JobName>wsdscan</wscn:JobName>"
        f"<wscn:JobOriginatingUserName>{escape(user)}</wscn:JobOriginatingUserName>"
        "</wscn:JobDescription>"
        "<wscn:DocumentParameters>"
        f"<wscn:Format>{pick_format(caps, mode, lossless)}</wscn:Format>"
        "<wscn:ImagesToTransfer>0</wscn:ImagesToTransfer>"  # 0 = whole feeder
        f"<wscn:InputSource>{'ADFDuplex' if duplex else 'ADF'}</wscn:InputSource>"
        "<wscn:InputSize><wscn:InputMediaSize>"
        f"<wscn:Width>{width}</wscn:Width><wscn:Height>{height}</wscn:Height>"
        "</wscn:InputMediaSize></wscn:InputSize>"
        f"{exposure}"
        f"<wscn:MediaSides>{sides}</wscn:MediaSides>"
        "</wscn:DocumentParameters>"
        "</wscn:ScanTicket>"
    )


def ticket_for(args, caps):
    return scan_ticket(args.source, args.mode, args.resolution, args.paper, caps,
                       lossless=args.lossless, brightness=args.brightness,
                       contrast=args.contrast)


def call_with_retry(service, action, body, timeout=TIMEOUT):
    for attempt in range(RETRY_COUNT):
        try:
            return soap_call(service, service, SCAN_ACTION + action, body, timeout)
        except SoapFault as e:
            if attempt + 1 < RETRY_COUNT and any(r in e.code for r in RETRY_FAULTS):
                log(f"  {e.code}, retrying")
                time.sleep(RETRY_DELAY)
                continue
            raise


def scan(service, args, caps, on_page=None, should_stop=None):
    """Run one scan job. Returns (list_of_image_bytes, dpi, complete).

    on_page(n, image) is called after each page with its JPEG/TIFF data;
    should_stop() is checked before each page and cancels the job when it
    returns True.
    """
    ticket = ticket_for(args, caps)
    root, _ = call_with_retry(service, "CreateScanJob",
                              f"<wscn:CreateScanJobRequest>{ticket}</wscn:CreateScanJobRequest>")
    job_id, token = find_text(root, "wscn:JobId"), find_text(root, "wscn:JobToken")
    if not job_id or not token:
        die("scanner did not return a job id")
    final_res = root.find(f".//{q('wscn:DocumentFinalParameters')}//{q('wscn:Resolution')}")
    dpi = args.resolution
    if final_res is not None and (find_text(final_res, "wscn:Width") or "").isdigit():
        dpi = int(find_text(final_res, "wscn:Width"))
    log(f"  job {job_id} created ({dpi} dpi)")

    pages, complete = [], True
    try:
        while True:
            if should_stop and should_stop():
                raise ScanCancelled("scan cancelled")
            body = ("<wscn:RetrieveImageRequest><wscn:DocumentDescription>"
                    f"<wscn:DocumentName>page{len(pages) + 1:03d}</wscn:DocumentName>"
                    "</wscn:DocumentDescription>"
                    f"<wscn:JobId>{escape(job_id)}</wscn:JobId>"
                    f"<wscn:JobToken>{escape(token)}</wscn:JobToken>"
                    "</wscn:RetrieveImageRequest>")
            try:
                _, image = call_with_retry(service, "RetrieveImage", body, SCAN_TIMEOUT)
            except SoapFault as e:
                if e.code == "ClientErrorNoImagesAvailable":
                    break  # feeder empty, job done
                if not pages:
                    raise
                print(f"warning: scan stopped after {len(pages)} page(s): {e}",
                      file=sys.stderr)
                complete = False
                break
            if not image or image_kind(image) is None:
                die("scanner returned no JPEG or TIFF image")
            pages.append(image)
            if on_page:
                on_page(len(pages), image)
            else:
                print(f"  page {len(pages)}", file=sys.stderr)
    except BaseException:
        cancel(service, job_id)
        raise
    if not pages:
        die("no pages scanned - is paper loaded in the feeder?")
    return pages, dpi, complete


def scan_to_file(args, out, device=None, on_progress=None, should_stop=None,
                 on_page_image=None, select_pages=None):
    """Find the scanner (unless given), check the settings, scan, write the PDF,
    and run text recognition if args.ocr is set.

    on_progress(event, n) is called with these events:
      "connecting", "scanning"   n = 0
      "page"                     n = pages scanned so far
      "saving"                   n = pages that will be saved
      "ocr"                      n = pages to recognize (OCR starts)
      "ocr_page"                 n = pages recognized so far (Tesseract only)
    on_page_image(n, image) gets each scanned page's JPEG/TIFF data.
    select_pages(images) may return the indexes of the pages to keep (in
    order); None discards the scan. Called after scanning, before saving/OCR.

    Returns (pages, complete, ocr_error): if OCR fails, the PDF is kept
    without text and ocr_error says why. Raises ScanError (ScanCancelled when
    stopped or discarded).
    """
    report = on_progress or (lambda event, pages: None)
    ocr = resolve_ocr(args.ocr_engine, args.ocr_lang) if args.ocr else None  # before scanning
    if device is None:
        report("connecting", 0)
        device = find_scanner(args.host, args.model)
    try:
        caps = get_capabilities(device["service"])
    except SoapFault as e:
        die(f"could not read scanner capabilities: {e}")
    validate(args, caps)
    report("scanning", 0)

    def page_done(n, image):
        report("page", n)
        if on_page_image:
            on_page_image(n, image)

    try:
        pages, dpi, complete = scan(device["service"], args, caps, on_page=page_done,
                                    should_stop=should_stop)
    except SoapFault as e:
        die(f"scan failed: {e}")
    if select_pages:
        keep = select_pages(pages)
        if keep is None:
            raise ScanCancelled("scan discarded")
        if not keep:
            raise ScanCancelled("all pages were removed; nothing was saved")
        pages = [pages[i] for i in keep]
    report("saving", len(pages))
    write_pdf(out, pages, dpi)
    ocr_error = None
    if ocr:
        report("ocr", len(pages))
        try:
            run_ocr(*ocr[:2], out, pages, dpi, ocr[2],
                    on_page=lambda n: report("ocr_page", n))
        except ScanError as e:
            ocr_error = str(e)
    return len(pages), complete, ocr_error


def cancel(service, job_id):
    try:
        soap_call(service, service, SCAN_ACTION + "CancelJob",
                  f"<wscn:CancelJobRequest><wscn:JobId>{escape(job_id)}</wscn:JobId>"
                  "</wscn:CancelJobRequest>")
    except Exception:
        pass


# --- Check mode --------------------------------------------------------------

class ValidationUnsupported(Exception):
    pass


def validate_ticket(service, ticket):
    """Ask the scanner whether it would accept a ticket. Feeds no paper.

    Returns (valid, detail). Raises ValidationUnsupported if the scanner does
    not implement ValidateScanTicket.
    """
    body = f"<wscn:ValidateScanTicketRequest>{ticket}</wscn:ValidateScanTicketRequest>"
    try:
        root, _ = soap_call(service, service, SCAN_ACTION + "ValidateScanTicket", body)
    except SoapFault as e:
        # Bare HTTP errors or "unknown action" faults mean the operation itself
        # is missing; any other fault is the scanner rejecting this ticket.
        if e.code.startswith("HTTP") or any(m in e.code for m in UNSUPPORTED_FAULTS):
            raise ValidationUnsupported(str(e))
        return False, e.code
    valid = find_text(root, "wscn:ValidTicket", "")
    if valid in ("true", "1"):
        return True, ""
    if valid in ("false", "0"):
        return False, "rejected"
    raise ValidationUnsupported("no ValidTicket in response")


def check_combinations(caps):
    """(mode, lossless) pairs worth validating for this scanner."""
    offered = lambda mode: not caps["colors"] or COLOR_ENTRIES[mode] in caps["colors"]
    has_tiff = not caps["formats"] or TIFF_FORMAT in caps["formats"]
    has_jpeg = not caps["formats"] or any(f in caps["formats"] for f in JPEG_FORMATS)
    combos = []
    for mode in ("color", "gray"):
        if offered(mode) and has_jpeg:
            combos.append((mode, False))
        if offered(mode) and has_tiff:
            combos.append((mode, True))
    if offered("bw") and has_tiff:
        combos.append(("bw", True))
    return combos


def run_check(service, caps, args):
    """Validate every source/mode/format/resolution combination, probe the
    exposure settings, then the exact requested settings. Returns exit code."""
    sources = ["adf"] + (["duplex"] if caps["duplex"] else [])
    resolutions = caps["resolutions"] or [args.resolution]

    print()
    print("Ticket validation (ValidateScanTicket, no paper is fed):")
    print(f"  {'source':<8}{'mode':<7}{'format':<10}{'dpi':>5}  result")
    try:
        for source in sources:
            for mode, lossless in check_combinations(caps):
                for dpi in resolutions:
                    ticket = scan_ticket(source, mode, dpi, args.paper, caps, lossless=lossless)
                    ok, detail = validate_ticket(service, ticket)
                    fmt = "lossless" if lossless else "jpeg"
                    print(f"  {source:<8}{mode:<7}{fmt:<10}{dpi:>5}  "
                          f"{'ok' if ok else 'REJECTED ' + detail}")
        probe_exposure(service, caps, args)
        exact_ok, detail = validate_ticket(service, ticket_for(args, caps))
    except ValidationUnsupported as e:
        print(f"  scanner does not support ticket validation ({e})")
        return check_summary(args, caps, None)
    return check_summary(args, caps, exact_ok)


def probe_exposure(service, caps, args):
    ds = caps.get("device_settings") or {}
    names = [n for n in ("brightness", "contrast") if ds.get(n) is not False]
    if not names:
        return
    print()
    print(f"Exposure (experimental; WS-Scan range {EXPOSURE_RANGE[0]}..{EXPOSURE_RANGE[1]}):")
    for name in names:
        accepted, rejected = [], []
        for value in EXPOSURE_PROBE:
            ticket = scan_ticket(args.source, args.mode, args.resolution, args.paper, caps,
                                 lossless=args.lossless, **{name: value})
            (accepted if validate_ticket(service, ticket)[0] else rejected).append(str(value))
        line = f"accepted: {', '.join(accepted) or 'none'}"
        if rejected:
            line += f"; rejected: {', '.join(rejected)}"
        print(f"  {name:<12}{line}")


def check_summary(args, caps, exact_ok):
    """Print whether the requested (or default) settings work. Returns exit code."""
    label = f"{args.source}, {args.mode}, {args.resolution} dpi, {args.paper}"
    if args.lossless and args.mode != "bw":
        label += ", lossless"
    for name in ("brightness", "contrast"):
        if getattr(args, name) is not None:
            label += f", {name} {getattr(args, name)}"
    problems = settings_problems(args, caps)
    if exact_ok is False and not problems:
        problems.append("scanner rejected the ticket")
    print()
    if problems:
        print(f"result: settings ({label}) NOT usable: {'; '.join(problems)}")
        return 1
    print(f"result: settings ({label}) usable, format "
          f"{pick_format(caps, args.mode, args.lossless)}")
    return 0


# --- PDF writing -------------------------------------------------------------

def jpeg_info(data):
    """Return (width, height, components) from a JPEG's SOF marker."""
    i = 2
    while i + 9 < len(data):
        if data[i] != 0xFF:
            break
        marker = data[i + 1]
        if marker == 0xFF:
            i += 1
            continue
        if marker == 0x01 or 0xD0 <= marker <= 0xD8:
            i += 2
            continue
        length = int.from_bytes(data[i + 2:i + 4], "big")
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            height = int.from_bytes(data[i + 5:i + 7], "big")
            width = int.from_bytes(data[i + 7:i + 9], "big")
            return width, height, data[i + 9]
        i += 2 + length
    die("could not read JPEG dimensions")


REVERSE_BITS = bytes(int(f"{i:08b}"[::-1], 2) for i in range(256))
TIFF_TYPES = {1: "B", 3: "H", 4: "I", 6: "b", 8: "h", 9: "i", 16: "Q"}


def image_kind(data):
    if data[:2] == b"\xff\xd8":
        return "jpeg"
    if data[:4] in (b"II*\x00", b"MM\x00*"):
        return "tiff"
    return None


def tiff_image(data):
    """Decode an uncompressed TIFF into raw pixel rows for the PDF.

    Supports what scanners send as tiff-single-uncompressed: 1-bit black &
    white, 8-bit gray and 24-bit RGB, in one or more strips.
    """
    order = {b"II": "<", b"MM": ">"}[data[:2]]
    try:
        (ifd,) = struct.unpack(order + "I", data[4:8])
        (count,) = struct.unpack(order + "H", data[ifd:ifd + 2])
        tags = {}
        for i in range(count):
            entry = data[ifd + 2 + 12 * i:ifd + 14 + 12 * i]
            tag, typ, n = struct.unpack(order + "HHI", entry[:8])
            if typ not in TIFF_TYPES:
                continue  # rationals, strings: not needed
            size = struct.calcsize(TIFF_TYPES[typ]) * n
            if size <= 4:
                raw = entry[8:8 + size]
            else:
                (offset,) = struct.unpack(order + "I", entry[8:12])
                raw = data[offset:offset + size]
            tags[tag] = list(struct.unpack(order + TIFF_TYPES[typ] * n, raw))
        width, height = tags[256][0], tags[257][0]
        offsets, counts = tags[273], tags[279]
    except (KeyError, struct.error, IndexError):
        die("could not read TIFF image")
    bits = tags.get(258, [1])
    samples = tags.get(277, [1])[0]
    compression = tags.get(259, [1])[0]
    photometric = tags.get(262, [1])[0]
    if compression != 1:
        die(f"TIFF compression {compression} not supported (expected uncompressed)")
    if len(set(bits)) != 1 or (samples, bits[0]) not in ((1, 1), (1, 8), (3, 8)):
        die(f"TIFF with {samples} sample(s) of {bits} bits not supported")
    if samples > 1 and tags.get(284, [1])[0] != 1:
        die("planar TIFF not supported")
    if photometric not in (0, 1, 2):
        die(f"TIFF photometric interpretation {photometric} not supported")
    row = (width * samples * bits[0] + 7) // 8
    pixels = b"".join(data[o:o + c] for o, c in zip(offsets, counts))
    if len(pixels) < row * height:
        die("TIFF image data is truncated")
    pixels = pixels[:row * height]
    if tags.get(266, [1])[0] == 2:  # FillOrder: least significant bit first
        pixels = pixels.translate(REVERSE_BITS)
    return {
        "width": width, "height": height, "bits": bits[0],
        "colorspace": "DeviceRGB" if samples == 3 else "DeviceGray",
        "invert": photometric == 0,  # WhiteIsZero: 0 means white
        "pixels": pixels,
    }


def image_xobject(data):
    """Return (pdf_object_bytes, width, height) for one scanned page."""
    if image_kind(data) == "tiff":
        img = tiff_image(data)
        stream = zlib.compress(img["pixels"], 6)
        decode = " /Decode [1 0]" if img["invert"] else ""
        head = (f"<< /Type /XObject /Subtype /Image /Width {img['width']} "
                f"/Height {img['height']} /ColorSpace /{img['colorspace']} "
                f"/BitsPerComponent {img['bits']}{decode} /Filter /FlateDecode "
                f"/Length {len(stream)} >>\nstream\n")
        return head.encode() + stream + b"\nendstream", img["width"], img["height"]
    width, height, comps = jpeg_info(data)
    colorspace = {1: "DeviceGray", 3: "DeviceRGB", 4: "DeviceCMYK"}.get(comps)
    if not colorspace:
        die(f"unsupported JPEG with {comps} components")
    head = (f"<< /Type /XObject /Subtype /Image /Width {width} /Height {height} "
            f"/ColorSpace /{colorspace} /BitsPerComponent 8 /Filter /DCTDecode "
            f"/Length {len(data)} >>\nstream\n")
    return head.encode() + data + b"\nendstream", width, height


def pdf_bytes(images, dpi):
    """Build a PDF with one scanned image per page.

    JPEGs are embedded unchanged (DCTDecode); TIFFs are stored losslessly
    with zlib (FlateDecode).
    """
    objects = [b"", b""]  # 1 = catalog, 2 = page tree; filled in below

    def add(obj):
        objects.append(obj)
        return len(objects)

    kids = []
    for data in images:
        xobject, width, height = image_xobject(data)
        img = add(xobject)
        pw, ph = width * 72 / dpi, height * 72 / dpi
        content = f"q {pw:.2f} 0 0 {ph:.2f} 0 0 cm /Im0 Do Q".encode()
        stream = add(f"<< /Length {len(content)} >>\nstream\n".encode()
                     + content + b"\nendstream")
        kids.append(add(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {pw:.2f} {ph:.2f}] "
            f"/Resources << /XObject << /Im0 {img} 0 R >> >> /Contents {stream} 0 R >>"
            .encode()))
    objects[0] = b"<< /Type /Catalog /Pages 2 0 R >>"
    objects[1] = (f"<< /Type /Pages /Kids [{' '.join(f'{k} 0 R' for k in kids)}] "
                  f"/Count {len(kids)} >>").encode()

    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for num, obj in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{num} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{off:010d} 00000 n \n".encode() for off in offsets)
    out += (f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref}\n%%EOF\n").encode()
    return bytes(out)


def write_pdf(path, images, dpi):
    with open(path, "wb") as f:
        f.write(pdf_bytes(images, dpi))


# --- Text recognition (OCR) ----------------------------------------------------
# Optional post-processing with OCRmyPDF or Tesseract, if installed. Adds an
# invisible text layer so the PDF becomes searchable.

OCR_ENGINES = ("ocrmypdf", "tesseract")  # "auto" picks the first available
OCR_ENGINE_CHOICES = ("auto",) + OCR_ENGINES
# Locale language (ISO 639-1) -> Tesseract language code.
TESSERACT_LANGS = {
    "ar": "ara", "bg": "bul", "ca": "cat", "cs": "ces", "da": "dan", "de": "deu",
    "el": "ell", "en": "eng", "es": "spa", "et": "est", "fi": "fin", "fr": "fra",
    "he": "heb", "hr": "hrv", "hu": "hun", "it": "ita", "ja": "jpn", "ko": "kor",
    "lt": "lit", "lv": "lav", "nb": "nor", "nl": "nld", "nn": "nor", "no": "nor",
    "pl": "pol", "pt": "por", "ro": "ron", "ru": "rus", "sk": "slk", "sl": "slv",
    "sr": "srp", "sv": "swe", "tr": "tur", "uk": "ukr", "zh": "chi_sim",
}


def ocr_engines():
    """Installed OCR engines in order of preference: [(name, path)]."""
    return [(name, path) for name in OCR_ENGINES if (path := shutil.which(name))]


def tool_version(path):
    try:
        result = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return "?"
    text = (result.stdout or result.stderr).strip()
    match = re.search(r"\d+(\.\d+)+", text.splitlines()[0] if text else "")
    return match.group(0) if match else "?"


def tesseract_languages():
    """Installed Tesseract languages (OCRmyPDF uses Tesseract too); [] if unknown."""
    path = shutil.which("tesseract")
    if not path:
        return []
    try:
        result = subprocess.run([path, "--list-langs"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return []
    lines = (result.stdout or result.stderr).splitlines()
    return sorted(line.strip() for line in lines[1:]
                  if line.strip() and line.strip() != "osd" and " " not in line.strip())


# With at most this many languages installed, the default uses all of them;
# more (e.g. tesseract-ocr-all) would make Tesseract slow and less accurate.
MAX_AUTO_OCR_LANGS = 4


def default_ocr_languages(installed=None, locale_name=None):
    """Default OCR languages, e.g. 'deu+eng'.

    All installed languages (system language and English first) if only a few
    are installed: installing a language pack means you want it used.
    Otherwise the system language plus English.
    """
    installed = tesseract_languages() if installed is None else installed
    if locale_name is None:
        locale_name = (os.environ.get("LC_ALL") or os.environ.get("LC_MESSAGES")
                       or os.environ.get("LANG") or "")
    preferred = [lang for lang in (TESSERACT_LANGS.get(locale_name[:2].lower()), "eng") if lang]
    if not installed:
        return "+".join(dict.fromkeys(preferred))
    ordered = [lang for lang in dict.fromkeys(preferred + installed) if lang in installed]
    if len(installed) > MAX_AUTO_OCR_LANGS:
        ordered = [lang for lang in ordered if lang in preferred] or ordered[:1]
    return "+".join(ordered)


def resolve_ocr(engine, lang):
    """Check an OCR request before scanning. Returns (engine_name, path, languages)."""
    available = dict(ocr_engines())
    if not available:
        die("text recognition needs OCRmyPDF or Tesseract; install one of them "
            "(e.g. 'sudo apt install ocrmypdf' or 'sudo apt install tesseract-ocr')")
    if engine == "auto":
        engine = next(name for name in OCR_ENGINES if name in available)
    elif engine not in available:
        die(f"OCR engine '{engine}' is not installed; available: {', '.join(available)}")
    installed = tesseract_languages()
    lang = lang or default_ocr_languages(installed)
    missing = [part for part in lang.split("+") if installed and part not in installed]
    if missing:
        die(f"OCR language(s) not installed: {', '.join(missing)}; installed: "
            f"{', '.join(installed)} (e.g. 'sudo apt install tesseract-ocr-{missing[0]}')")
    return engine, available[engine], lang


TESSERACT_PAGE_LINE = re.compile(r"^Page (\d+)\b")


def run_command(cmd, on_line=None):
    """Run cmd; feed stderr lines to on_line as they come. Returns (code, output)."""
    log("+ " + " ".join(cmd))
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, errors="replace")
    except OSError as e:
        die(f"text recognition failed: {e}")
    lines = []
    assert proc.stdout is not None
    for line in proc.stdout:
        lines.append(line.rstrip("\n"))
        if on_line:
            on_line(line.strip())
    return proc.wait(), lines


def run_ocr(engine, path, pdf_path, images, dpi, lang, on_page=None):
    """Replace pdf_path by a searchable PDF. Raises ScanError (pdf_path is kept).

    on_page(n) reports recognized pages; only Tesseract reports them
    ("Page N : file" lines), OCRmyPDF has no per-page progress output.
    """
    with tempfile.TemporaryDirectory(prefix="wsdscan-ocr-") as tmp:
        result_pdf = os.path.join(tmp, "out.pdf")
        if engine == "ocrmypdf":
            # Plain PDF output keeps the scanned images untouched (no PDF/A conversion).
            cmd = [path, "-l", lang, "--output-type", "pdf", pdf_path, result_pdf]
        else:
            # Tesseract builds the PDF itself from the page images.
            listing = os.path.join(tmp, "pages.txt")
            names = []
            for n, data in enumerate(images, 1):
                name = os.path.join(tmp, f"page{n:03d}.{'tif' if image_kind(data) == 'tiff' else 'jpg'}")
                with open(name, "wb") as f:
                    f.write(data)
                names.append(name)
            with open(listing, "w", encoding="utf-8") as f:
                f.write("\n".join(names) + "\n")
            cmd = [path, listing, result_pdf[:-4], "-l", lang, "--dpi", str(dpi), "pdf"]
        done = []

        def on_line(line):
            match = TESSERACT_PAGE_LINE.match(line)
            if match and on_page and engine == "tesseract":
                done.append(int(match.group(1)))
                on_page(len(done))

        code, output = run_command(cmd, on_line)
        if code != 0 or not os.path.exists(result_pdf):
            details = [line for line in output if line.strip()][-3:]
            die(f"text recognition with {engine} failed (exit {code})"
                + (": " + " / ".join(details) if details else ""))
        shutil.copyfile(result_pdf, pdf_path)


# --- Configuration -----------------------------------------------------------
# One INI file shared by the CLI and the GUI. Section [scan] holds defaults for
# the scan options; command-line options and environment variables override
# them. Other sections (e.g. [gui]) belong to other programs and are kept.

CONFIG_DEFAULTS = {
    "host": "",
    "model": "",
    "source": "duplex",
    "mode": "color",
    "resolution": 300,
    "paper": "a4",
    "lossless": False,
    "brightness": None,  # None = scanner default
    "contrast": None,
    "outdir": "",  # "" = current directory (CLI) / Documents folder (GUI)
    "filename": "scan_{date}_{time}.pdf",
    "ocr": False,
    "ocr_engine": "auto",
    "ocr_lang": "",  # "" = system language + English
    "scanner": "",  # name of the default [scanner NAME] profile
}
SCANNER_SECTION = "scanner "  # profiles: [scanner Office], [scanner Home], ...
CONFIG_CHOICES = {
    "ocr_engine": OCR_ENGINE_CHOICES,
    "source": ("adf", "duplex"),
    "mode": tuple(COLOR_ENTRIES),
    "paper": tuple(PAPER_SIZES),
}
CONFIG_HEADER = ("# wsdscan settings, shared by the wsdscan command and the Scan to PDF app.\n"
                 "# [scan] holds the defaults, [scanner NAME] sections configure scanners\n"
                 "# (host/model and optional own defaults). Command-line options and\n"
                 "# WSDSCAN_SCANNER / WSDSCAN_HOST / WSDSCAN_MODEL override them.\n")


def config_path():
    if os.environ.get("WSDSCAN_CONFIG"):
        return os.environ["WSDSCAN_CONFIG"]
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "wsdscan", "config.ini")


def parse_config_value(key, text):
    """Convert a config string to its typed value. Raises ValueError."""
    text = text.strip()
    if key in ("brightness", "contrast"):
        if text in ("", "default"):
            return None
        try:
            return exposure_value(text)
        except argparse.ArgumentTypeError as e:
            raise ValueError(str(e))
    if key == "ocr_lang" and text and not re.fullmatch(r"[a-z_]+(\+[a-z_]+)*", text):
        raise ValueError("expected Tesseract language codes like deu+eng")
    if key in ("lossless", "ocr"):
        if text.lower() not in ("true", "false", "yes", "no", "1", "0", "on", "off"):
            raise ValueError("expected true or false")
        return text.lower() in ("true", "yes", "1", "on")
    if key == "resolution":
        if not text.isdigit() or not 50 <= int(text) <= 2400:
            raise ValueError("expected a resolution in dpi, e.g. 300")
        return int(text)
    if key in CONFIG_CHOICES and text not in CONFIG_CHOICES[key]:
        raise ValueError(f"expected one of {', '.join(CONFIG_CHOICES[key])}")
    if key == "filename" and (not text or "/" in text):
        raise ValueError("expected a file name without '/'")
    if key == "outdir" and text:
        return os.path.expanduser(text)
    return text


def format_config_value(value):
    if value is None:
        return "default"
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def read_config_file(path):
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read(path, encoding="utf-8")
    except (configparser.Error, UnicodeDecodeError) as e:
        die(f"{path}: {e}")
    return parser


def check_scanner_name(name):
    """Validate a scanner profile name. Raises ValueError."""
    if not name or name != name.strip() or len(name) > 64 or any(c in name for c in "[]\n\r"):
        raise ValueError(f"invalid scanner name {name!r}: 1-64 characters, no brackets, "
                         "no leading/trailing spaces")
    return name


def _typed_section(parser, section, path, allowed):
    values = {}
    for key, text in parser.items(section):
        if key not in allowed:
            die(f"{path}: unknown setting '{key}' in [{section}]")
        try:
            values[key] = parse_config_value(key, text)
        except ValueError as e:
            die(f"{path}: [{section}] {key}: {e}")
    return values


def load_scanners(path=None):
    """Configured scanner profiles in file order: {name: {key: typed value}}."""
    path = path or config_path()
    parser = read_config_file(path)
    allowed = set(CONFIG_DEFAULTS) - {"scanner"}
    return {section[len(SCANNER_SECTION):]: _typed_section(parser, section, path, allowed)
            for section in parser.sections() if section.startswith(SCANNER_SECTION)}


def default_scanner_name(values, scanners):
    """The profile to use when none is requested: [scan] scanner, else the first
    profile unless [scan] itself names a host or model."""
    if values.get("scanner"):
        return values["scanner"]
    if scanners and not values.get("host") and not values.get("model"):
        return next(iter(scanners))
    return ""


def load_config(path=None, scanner=None, apply_profile=True):
    """Typed settings: built-in defaults < [scan] < the selected [scanner NAME].

    scanner: profile name (None = the default profile). The result's
    "scanner" key names the profile that was applied ("" = none).
    apply_profile=False returns just [scan] (the shared defaults).
    """
    path = path or config_path()
    values = dict(CONFIG_DEFAULTS)
    parser = read_config_file(path)
    if parser.has_section("scan"):
        values.update(_typed_section(parser, "scan", path, CONFIG_DEFAULTS))
    if not apply_profile:
        return values
    scanners = load_scanners(path)
    name = scanner if scanner is not None else default_scanner_name(values, scanners)
    if name:
        if name not in scanners:
            die(f"unknown scanner '{name}'; configured: {', '.join(scanners) or 'none'} "
                f"(see [scanner NAME] sections in {path})")
        values.update(scanners[name])
    values["scanner"] = name
    return values


def load_config_section(section, path=None):
    """Raw string values of another section, e.g. [gui]."""
    parser = read_config_file(path or config_path())
    return dict(parser.items(section)) if parser.has_section(section) else {}


def save_config(values, path=None, sections=None, scanners=None):
    """Write [scan] values (validated) and other sections; keep the rest of the file.

    scanners: {name: {key: value}} replaces all [scanner NAME] sections.
    """
    path = path or config_path()
    parser = read_config_file(path)
    if not parser.has_section("scan"):
        parser.add_section("scan")
    for key, value in values.items():
        if key not in CONFIG_DEFAULTS:
            raise ValueError(f"unknown setting: {key}")
        text = format_config_value(value)
        parse_config_value(key, text)  # validate before writing
        parser.set("scan", key, text)
    if scanners is not None:
        for section in parser.sections():
            if section.startswith(SCANNER_SECTION):
                parser.remove_section(section)
        for name, entries in scanners.items():
            section = SCANNER_SECTION + check_scanner_name(name)
            parser.add_section(section)
            for key, value in entries.items():
                if key not in CONFIG_DEFAULTS or key == "scanner":
                    raise ValueError(f"unknown scanner setting: {key}")
                text = format_config_value(value)
                parse_config_value(key, text)
                parser.set(section, key, text)
    for name, entries in (sections or {}).items():
        if not parser.has_section(name):
            parser.add_section(name)
        for key, value in entries.items():
            parser.set(name, key, format_config_value(value))
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(CONFIG_HEADER)
        parser.write(f)
    os.replace(tmp, path)


def render_filename(template, now=None):
    """'scan_{date}_{time}.pdf' -> 'scan_2026-10-03_14-30-05.pdf'."""
    now = now or datetime.datetime.now()
    name = (template.replace("{date}", now.strftime("%Y-%m-%d"))
            .replace("{time}", now.strftime("%H-%M-%S")).replace("/", "_").strip())
    if not name.lower().endswith(".pdf"):
        name += ".pdf"
    return name


def unique_path(path):
    """`path`, or 'name (2).pdf', 'name (3).pdf', ... if it exists."""
    if not os.path.exists(path):
        return path
    base, ext = os.path.splitext(path)
    n = 2
    while os.path.exists(f"{base} ({n}){ext}"):
        n += 1
    return f"{base} ({n}){ext}"


# --- CLI ---------------------------------------------------------------------

def exposure_value(text):
    if text == "default":
        return None
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a number: {text!r}")
    if not EXPOSURE_RANGE[0] <= value <= EXPOSURE_RANGE[1]:
        raise argparse.ArgumentTypeError(
            f"must be between {EXPOSURE_RANGE[0]} and {EXPOSURE_RANGE[1]}")
    return value


def parse_args(argv=None):
    # The scanner profile decides the other defaults, so read it first.
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--scanner", default=os.environ.get("WSDSCAN_SCANNER") or None)
    requested = pre.parse_known_args(argv)[0].scanner
    cfg = load_config(scanner=requested)
    p = argparse.ArgumentParser(
        description="Scan from a WSD network scanner (e.g. Epson ES-580W) and save "
                    "the result as PDF.",
        epilog=f"Defaults come from {config_path()} (if it exists); options given "
               "here override them.")
    p.add_argument("output", nargs="?",
                   help="output PDF (default: file name template in --outdir, "
                        f"currently {cfg['filename']})")
    p.add_argument("--scanner", default=requested, metavar="NAME",
                   help="use the [scanner NAME] profile from the config file "
                        f"(default: $WSDSCAN_SCANNER or config; currently "
                        f"{cfg['scanner'] or 'none'})")
    p.add_argument("--host",
                   default=os.environ.get("WSDSCAN_HOST") or cfg["host"] or None,
                   help="scanner IP or hostname (default: $WSDSCAN_HOST or config, "
                        "else multicast discovery)")
    p.add_argument("--model", default=os.environ.get("WSDSCAN_MODEL") or cfg["model"] or None,
                   help="only use a scanner whose manufacturer/model contains this text, "
                        "e.g. ES-580W (case-insensitive; default: $WSDSCAN_MODEL or config)")
    p.add_argument("-L", "--list", action="store_true",
                   help="list the WSD scanners found (respects --host and --model), then exit")
    p.add_argument("--outdir", default=cfg["outdir"] or ".",
                   help="directory for the default file name (default: %(default)s)")
    p.add_argument("-f", "--force", action="store_true", help="overwrite existing output")
    p.add_argument("-s", "--source", choices=("adf", "duplex"), default=cfg["source"],
                   help="one-sided (adf) or two-sided (duplex); default: %(default)s")
    p.add_argument("-m", "--mode", choices=tuple(COLOR_ENTRIES), default=cfg["mode"],
                   help="color, gray or bw (black & white, always lossless); "
                        "default: %(default)s")
    p.add_argument("-l", "--lossless", action=argparse.BooleanOptionalAction,
                   default=cfg["lossless"],
                   help="transfer color/gray pages uncompressed (TIFF) and store them "
                        "losslessly instead of as JPEG; much larger files "
                        "(default: %(default)s)")
    p.add_argument("--brightness", type=exposure_value, metavar="N", default=cfg["brightness"],
                   help=f"brightness {EXPOSURE_RANGE[0]}..{EXPOSURE_RANGE[1]} or 'default' "
                        "(experimental; default: %(default)s = scanner default)")
    p.add_argument("--contrast", type=exposure_value, metavar="N", default=cfg["contrast"],
                   help=f"contrast {EXPOSURE_RANGE[0]}..{EXPOSURE_RANGE[1]} or 'default' "
                        "(experimental; default: %(default)s = scanner default)")
    p.add_argument("--ocr", action=argparse.BooleanOptionalAction, default=cfg["ocr"],
                   help="recognize text (OCR) so the PDF is searchable; needs OCRmyPDF "
                        "or Tesseract (default: %(default)s)")
    p.add_argument("--ocr-engine", choices=OCR_ENGINE_CHOICES, default=cfg["ocr_engine"],
                   help="auto prefers OCRmyPDF over Tesseract (default: %(default)s)")
    p.add_argument("--ocr-lang", default=cfg["ocr_lang"] or None, metavar="LANGS",
                   help="Tesseract languages, e.g. deu+eng (default: system language + "
                        "English, if installed)")
    p.add_argument("-r", "--resolution", type=int, default=cfg["resolution"],
                   help="dpi (default: %(default)s)")
    p.add_argument("-p", "--paper", choices=tuple(PAPER_SIZES), default=cfg["paper"],
                   help="paper size (default: %(default)s)")
    p.add_argument("-i", "--info", action="store_true",
                   help="show scanner capabilities and status, then exit")
    p.add_argument("-c", "--check", action="store_true",
                   help="test mode: list all features and let the scanner validate every "
                        "source/mode/resolution combination, without scanning")
    p.add_argument("--show-config", action="store_true",
                   help="show the config file location and the effective defaults, then exit")
    p.add_argument("-v", "--verbose", action="store_true", help="show protocol steps")
    args = p.parse_args(argv)
    args.filename = cfg["filename"]
    args.scanner = cfg["scanner"]
    return args


def show_config(args):
    path = config_path()
    print(f"config file: {path} ({'exists' if os.path.exists(path) else 'not created yet'})")
    for key in CONFIG_DEFAULTS:
        value = args.filename if key == "filename" else getattr(args, key)
        print(f"  {key:<11} {format_config_value(value) if value is not None else '(none)'}")
    scanners = load_scanners()
    default = default_scanner_name(load_config(apply_profile=False), scanners)
    if scanners:
        print("configured scanners:")
        for name, entries in scanners.items():
            where = entries.get("host") or "automatic"
            extra = ", ".join(f"{k}={format_config_value(v)}" for k, v in entries.items()
                              if k != "host")
            marks = " (default)" if name == default else ""
            print(f"  {name}{marks}: {where}" + (f"; {extra}" if extra else ""))
    engines = ocr_engines()
    print("OCR engines: " + (", ".join(f"{name} {tool_version(path)}" for name, path in engines)
                             or "none installed (install ocrmypdf or tesseract-ocr)"))
    if engines:
        print(f"OCR languages: {', '.join(tesseract_languages()) or '?'} "
              f"(automatic: {default_ocr_languages()})")


def main(argv=None):
    global verbose
    args = parse_args(argv)
    verbose = args.verbose
    report_only = args.info or args.check
    if args.show_config:
        show_config(args)
        return
    if args.list:
        list_scanners(args.host, args.model)
        return

    out = args.output or os.path.join(args.outdir, render_filename(args.filename))
    if not report_only and os.path.exists(out) and not args.force:
        die(f"{out} already exists (use --force to overwrite)")

    device = find_scanner(args.host, args.model)
    service = device["service"]
    if report_only:
        try:
            caps = get_capabilities(service)
        except SoapFault as e:
            die(f"could not read scanner capabilities: {e}")
        print_info(device, caps)
        if args.check:
            sys.exit(run_check(service, caps, args))
        return

    print(f"using scanner: {device['model']} ({service})", file=sys.stderr)

    def progress(event, pages):
        if event == "scanning":
            print("scanning...", file=sys.stderr)
        elif event == "page":
            print(f"  page {pages}", file=sys.stderr)
        elif event == "ocr":
            print("recognizing text...", file=sys.stderr)

    pages, complete, ocr_error = scan_to_file(args, out, device=device, on_progress=progress)
    print(f"saved {pages} page(s) to {out}")
    if not complete:
        sys.exit(2)
    if ocr_error:
        print(f"warning: {ocr_error}; the PDF was saved without text", file=sys.stderr)
        sys.exit(3)


if __name__ == "__main__":
    try:
        main()
    except ScanError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)
