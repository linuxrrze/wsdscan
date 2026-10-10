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
import http.client
import math
import re
import secrets
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import unicodedata
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
    "auto": None,  # the scanner's whole scan area; the sheet is found in the image
    "a4": (8268, 11693),
    "a5": (5827, 8268),
    "letter": (8500, 11000),
    "legal": (8500, 14000),
}

AUTO_SIZE_FALLBACK = (8500, 14000)  # paper "auto" if the scanner reports no maximum: legal

# Faults that mean "try again shortly" rather than "give up".
RETRY_FAULTS = ("ServerErrorTemporaryError", "ServerErrorNotAcceptingJobs", "Busy")
# Faults meaning the scanner does not implement the requested operation.
UNSUPPORTED_FAULTS = ("ActionNotSupported", "UnknownAction", "OperationNotSupported",
                      "UnsupportedOperation")
RETRY_COUNT = 10
RETRY_DELAY = 1.0
TIMEOUT = 10
SCAN_TIMEOUT = 120  # RetrieveImage blocks while the page is being scanned

# Limits against a malicious or broken device on the network.
MAX_RESPONSE_BYTES = 300 * 2 ** 20  # one page; 600 dpi legal color is ~130 MB uncompressed
MAX_IMAGE_BYTES = MAX_RESPONSE_BYTES  # decoded pixels of one page
MAX_IMAGE_SIDE = 60000  # pixels
MAX_PAGES = 1000  # per scan job
MAX_SCAN_BYTES = 2 * 2 ** 30  # all pages of one scan job
DEADLINE_FACTOR = 3  # a whole response may take this many times the read timeout

verbose = False


class SoapFault(Exception):
    def __init__(self, code, reason=""):
        super().__init__(f"{code}: {reason}" if reason else code)
        self.code = code


class ScanError(Exception):
    """A user-facing error; the CLI prints it and exits with status 1."""


class ScanCancelled(ScanError):
    """The scan was stopped on request (should_stop returned True)."""


class ScannerNotFound(ScanError):
    """No scanner answered: switched off, not reachable, or none on the network."""


class ScannerChoiceNeeded(ScanError):
    """Several scanners answered and nothing says which one to use."""


def die(msg):
    raise ScanError(msg)


def log(msg):
    if verbose:
        print(msg, file=sys.stderr)


def q(tag):
    """'wscn:Foo' -> '{namespace}Foo' for ElementTree lookups."""
    prefix, name = tag.split(":")
    return f"{{{NS[prefix]}}}{name}"


def clean(text):
    """Text from the device or network without control characters.

    Terminal escape sequences, C1 controls, bidi overrides and line breaks in
    model names, fault texts etc. could fake or garble the output.
    """
    return "".join("?" if unicodedata.category(c)[0] == "C" or unicodedata.category(c) in ("Zl", "Zp")
                   else c for c in str(text))


def find_text(root, tag, default=None):
    el = root.find(f".//{q(tag)}")
    return clean(el.text.strip()) if el is not None and el.text else default


def find_all_text(root, tag):
    return [clean(el.text.strip()) for el in root.iter(q(tag)) if el.text]


def parse_int(text, low, high):
    """int(text) if it is plain ASCII digits within low..high, else None."""
    if isinstance(text, str) and re.fullmatch(r"[0-9]{1,9}", text):
        value = int(text)
        if low <= value <= high:
            return value
    return None


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
    values = [clean(v.text.strip()) for v in fault.iter(q("soap:Value")) if v.text]
    code = values[-1].split(":")[-1] if values else "UnknownFault"
    return SoapFault(code, find_text(fault, "soap:Text", ""))


# --- HTTP / SOAP ------------------------------------------------------------

def _make_opener():
    """HTTP(S) only: no file:/data:/ftp: URLs, no redirects, no proxy.

    URLs come from the device, so a malicious device must not be able to send
    the client elsewhere (or make it read local files), and scans should not
    leave the local network through a proxy.
    """
    opener = urllib.request.OpenerDirector()
    for handler in (urllib.request.ProxyHandler({}), urllib.request.HTTPHandler(),
                    urllib.request.HTTPSHandler(), urllib.request.HTTPDefaultErrorHandler(),
                    urllib.request.HTTPErrorProcessor()):
        opener.add_handler(handler)  # no redirect handler: 3xx becomes an HTTPError
    return opener


OPENER = _make_opener()


def url_parts(url):
    """urlsplit() that returns None for malformed URLs instead of raising."""
    try:
        parts = urlsplit(url)
        parts.port  # raises ValueError for a bad port
        return parts
    except ValueError:
        return None


def is_http_url(url):
    parts = url_parts(url)
    return bool(parts and parts.scheme in ("http", "https") and parts.hostname)


def check_url(url):
    if not is_http_url(url):
        die(f"refusing scanner URL {clean(url)!r}: only http and https are allowed")


def read_limited(resp, deadline):
    """Read a response body, refusing endless or oversized responses."""
    chunks, total = [], 0
    while True:
        if time.monotonic() > deadline:
            die("the scanner's response took too long")
        chunk = resp.read(2 ** 20)
        if not chunk:
            return b"".join(chunks)
        total += len(chunk)
        if total > MAX_RESPONSE_BYTES:
            die(f"the scanner's response is larger than {MAX_RESPONSE_BYTES // 2 ** 20} MB")
        chunks.append(chunk)


def parse_xml(data):
    if b"<!DOCTYPE" in data or b"<!ENTITY" in data:
        die("refusing an XML response with a DTD")  # SOAP never uses one
    try:
        return ET.fromstring(data)
    except ET.ParseError as e:
        die(f"invalid XML from the scanner: {e}")


def soap_call(url, to, action, body="", timeout=TIMEOUT):
    """POST a SOAP request. Returns (xml_root, attachment_bytes_or_None)."""
    check_url(url)
    log(f"> {action.rsplit('/', 1)[-1]} -> {clean(url)}")
    deadline = time.monotonic() + timeout * DEADLINE_FACTOR
    try:
        req = urllib.request.Request(
            url, data=envelope(to, action, body),
            headers={"Content-Type": "application/soap+xml; charset=utf-8"})
        with OPENER.open(req, timeout=timeout) as resp:
            ctype, data = resp.headers.get("Content-Type", ""), read_limited(resp, deadline)
    except urllib.error.HTTPError as e:
        with e:
            data = read_limited(e, deadline)
        try:
            fault = parse_fault(parse_xml(data))
        except ScanError:
            fault = None
        raise fault or SoapFault(f"HTTP{e.code}", clean(e.reason))
    except (urllib.error.URLError, OSError, http.client.HTTPException, ValueError) as e:
        die(f"cannot reach scanner at {clean(url)}: {clean(e)}")

    attachment = None
    if ctype.lower().startswith("multipart/"):
        data, attachment = split_multipart(ctype, data)
    root = parse_xml(data)
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

def parse_host(host):
    """'scanner', '192.168.2.13' or 'name:3702' -> (name, port)."""
    name, sep, port = host.rpartition(":")
    if not sep or not name:
        name, port = host, ""
    port_number = parse_int(port, 1, 65535) if port else WSD_PORT
    if not name or port_number is None or any(c in name for c in " /[]"):
        die(f"invalid scanner address {clean(host)!r} (expected a host name or IPv4 address)")
    return name, port_number


def resolve(name):
    """IPv4 addresses of a host name (empty set if it cannot be resolved)."""
    try:
        return {info[4][0] for info in socket.getaddrinfo(name, None, socket.AF_INET)}
    except (OSError, UnicodeError):
        return set()


def host_matches(url, addresses):
    """True if the URL's host is one of `addresses` or resolves to one of them."""
    parts = url_parts(url)
    host = parts.hostname if parts else None
    return bool(host) and (host in addresses or bool(resolve(host) & addresses))


def probe(host, timeout=3.0):
    """WS-Discovery Probe. Returns [(endpoint_uuid, [device_urls])] for scanners.

    Device URLs are only accepted if they point to the host that answered
    (with --host: only answers from that host count), and the first answer
    per device wins. This keeps another machine on the network from
    redirecting the client by answering for, or on behalf of, a scanner.
    """
    allowed = None
    if host:
        name, port = parse_host(host)
        allowed = resolve(name)
        if not allowed:
            raise ScannerNotFound(f"cannot resolve scanner address {clean(name)!r}")
        target = (name, port)
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
        try:
            sock.sendto(msg, target)
        except OSError as e:
            raise ScannerNotFound(f"cannot send the discovery request to "
                                  f"{clean(target[0])}: {clean(e)}")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            sock.settimeout(max(0.1, deadline - time.monotonic()))
            try:
                data, (source, _port) = sock.recvfrom(65535)
            except socket.timeout:
                break
            if allowed is not None and source not in allowed:
                log(f"  ignoring an answer from {source}")
                continue
            if b"<!DOCTYPE" in data or b"<!ENTITY" in data:
                continue
            try:
                root = ET.fromstring(data)
            except ET.ParseError:
                continue
            for match in root.iter(q("wsd:ProbeMatch")):
                if "ScanDeviceType" not in (find_text(match, "wsd:Types") or ""):
                    continue
                addr = find_text(match, "wsa:Address")
                xaddrs = [url for url in (find_text(match, "wsd:XAddrs") or "").split()
                          if is_http_url(url) and host_matches(url, {source})]
                if addr and xaddrs and addr not in found:  # first answer wins
                    found[addr] = xaddrs
                elif addr and not xaddrs:
                    log(f"  ignoring {source}: its device address points elsewhere")
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
        except (SoapFault, ScanError) as e:  # one broken device must not stop discovery
            log(f"  metadata from {clean(url)} failed: {e}")
            continue
        device = {
            "manufacturer": find_text(root, "wsdp:Manufacturer", "?"),
            "model": find_text(root, "wsdp:ModelName", "?"),
            "firmware": find_text(root, "wsdp:FirmwareVersion", "?"),
            "serial": find_text(root, "wsdp:SerialNumber", "?"),
            "device_url": url,
            "service": None,
        }
        device_host = urlsplit(url).hostname
        same_host = {device_host} | resolve(device_host)
        for hosted in root.iter(q("wsdp:Hosted")):
            if "ScannerServiceType" in (find_text(hosted, "wsdp:Types") or ""):
                service = find_text(hosted, "wsa:Address") or ""
                if is_http_url(service) and host_matches(service, same_host):
                    device["service"] = service
                else:
                    log(f"  ignoring a scan service on another host: {service}")
                break
        return device
    return None


def discover(host):
    """All WSD scanners that answer, with their metadata. Dies if none does."""
    devices = probe(host)
    if not devices:
        where = host or "the local network (multicast)"
        raise ScannerNotFound(f"no WSD scanner answered on {where}. Check that WSD is "
                              "enabled on the scanner" + ("" if host else ", or pass --host <ip>"))
    found = []
    for endpoint, xaddrs in devices:
        device = get_device(endpoint, xaddrs)
        if device and device["service"]:
            log(f"  found {describe_device(device)}")
            found.append(device)
    if not found:
        raise ScannerNotFound(f"none of the {len(devices)} WSD device(s) offers a scan service")
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
        raise ScannerChoiceNeeded(f"{len(matches)} scanners found ("
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
    width, height = PAPER_SIZES[paper] or caps["max_size"] or AUTO_SIZE_FALLBACK
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
    if final_res is not None:
        dpi = parse_int(find_text(final_res, "wscn:Width"), 50, 2400) or dpi
    log(f"  job {job_id} created ({dpi} dpi)")
    expected = "tiff" if pick_format(caps, args.mode, args.lossless) == TIFF_FORMAT else "jpeg"

    pages, complete, total = [], True, 0
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
                print(f"warning: scan stopped after {len(pages)} page(s): {clean(e)}",
                      file=sys.stderr)
                complete = False
                break
            if not image and pages:
                break  # some scanners end the job with an empty image instead of a fault
            if not image or image_kind(image) != expected:
                die(f"scanner returned no {expected.upper()} image")
            pages.append(image)
            total += len(image)
            if on_page:
                on_page(len(pages), image)
            else:
                print(f"  page {len(pages)}", file=sys.stderr)
            if len(pages) >= MAX_PAGES or total > MAX_SCAN_BYTES:
                print(f"warning: stopping after {len(pages)} page(s): more than one scan "
                      "job should hold", file=sys.stderr)
                complete = False
                cancel(service, job_id)
                break
    except BaseException:
        cancel(service, job_id)
        raise
    if not pages:
        die("no pages scanned - is paper loaded in the feeder?")
    return pages, dpi, complete


def scan_to_file(args, out, device=None, on_progress=None, should_stop=None,
                 on_page_image=None, select_pages=None, overwrite=False):
    """Find the scanner (unless given), check the settings, scan, write the PDF,
    and run text recognition if args.ocr is set.

    on_progress(event, n) is called with these events:
      "connecting", "scanning"   n = 0
      "page"                     n = pages scanned so far
      "blank"                    n = number of a page found blank (args.skip_blank)
      "corrected"                n = number of a page with corrections (crop, skew, rotate)
      "all_blank"                n = pages kept because all of them were blank
      "saving"                   n = pages that will be saved
      "ocr"                      n = pages to recognize (OCR starts)
      "ocr_page"                 n = pages recognized so far (Tesseract only)
    on_page_image(n, image, info) gets each scanned page's JPEG/TIFF data and
    its analyze_page() result: whether it is blank (args.skip_blank) and the
    corrections found (paper "auto", args.deskew, args.auto_rotate).
    select_pages(images, infos) may return the indexes of the pages to keep
    (in order); None discards the scan. It may also switch corrections off
    in an info's "use". Called after scanning, before saving/OCR. Without it,
    blank pages are removed (args.skip_blank) unless all pages are blank.
    `out` is the PDF path, or a function returning it: then it is called
    right before saving, so the name and folder can still change while
    scanning and during page selection.

    Returns (pages, complete, ocr_error): if OCR fails, the PDF is kept
    without text and ocr_error says why. Raises ScanError (ScanCancelled when
    stopped or discarded).
    """
    report = on_progress or (lambda event, pages: None)
    ocr = resolve_ocr(args.ocr_engine, args.ocr_lang) if args.ocr else None  # before scanning
    osd = resolve_osd(args, ocr)
    if device is None:
        report("connecting", 0)
        device = find_scanner(args.host, args.model)
    try:
        caps = get_capabilities(device["service"])
    except SoapFault as e:
        die(f"could not read scanner capabilities: {e}")
    validate(args, caps)
    report("scanning", 0)
    infos = []

    def page_done(n, image):
        report("page", n)
        infos.append(analyze_page(image, args, args.resolution, osd))
        if infos[-1]["blank"]:
            report("blank", n)
        elif corrections_done(infos[-1]):
            report("corrected", n)
        if on_page_image:
            on_page_image(n, image, infos[-1])

    try:
        pages, dpi, complete = scan(device["service"], args, caps, on_page=page_done,
                                    should_stop=should_stop)
    except SoapFault as e:
        die(f"scan failed: {e}")
    if select_pages:
        keep = select_pages(pages, infos)
        if keep is None:
            raise ScanCancelled("scan discarded")
        if not keep:
            raise ScanCancelled("all pages were removed; nothing was saved")
    elif any(info["blank"] for info in infos):
        if all(info["blank"] for info in infos):
            report("all_blank", len(pages))  # rather than saving nothing
            keep = range(len(pages))
        else:
            keep = [i for i, info in enumerate(infos) if not info["blank"]]
    else:
        keep = range(len(pages))
    pages, infos = [pages[i] for i in keep], [infos[i] for i in keep]
    if callable(out):
        out = out()
    report("saving", len(pages))
    write_pdf(out, pages, dpi, overwrite, pages=infos)
    ocr_error = None
    if ocr:
        report("ocr", len(pages))
        try:
            run_ocr(*ocr[:2], out, pages, dpi, ocr[2],
                    on_page=lambda n: report("ocr_page", n), pages=infos)
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
            check_image_size(width, height)
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


def check_image_size(width, height):
    if not (0 < width <= MAX_IMAGE_SIDE and 0 < height <= MAX_IMAGE_SIDE):
        die(f"image size {width} x {height} is not plausible")


def tiff_tags(data):
    """(byte order, {tag: [values]}) of the first TIFF directory, bounds-checked."""
    order = {b"II": "<", b"MM": ">"}.get(data[:2])
    if order is None:
        die("could not read TIFF image")
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
            if size > len(data):
                die("TIFF image data is out of range")
            if size <= 4:
                raw = entry[8:8 + size]
            else:
                (offset,) = struct.unpack(order + "I", entry[8:12])
                raw = data[offset:offset + size]
            tags[tag] = list(struct.unpack(f"{order}{n}{TIFF_TYPES[typ]}", raw))
    except (struct.error, IndexError):
        die("could not read TIFF image")
    return order, tags


def image_size(data):
    """(width, height) of a scanned JPEG or TIFF page, read from its header."""
    if image_kind(data) == "jpeg":
        width, height, _components = jpeg_info(data)
        return width, height
    _order, tags = tiff_tags(data)
    try:
        width, height = tags[256][0], tags[257][0]
    except (KeyError, IndexError):
        die("could not read TIFF image")
    check_image_size(width, height)
    return width, height


def tiff_image(data):
    """Decode an uncompressed TIFF into raw pixel rows for the PDF.

    Supports what scanners send as tiff-single-uncompressed: 1-bit black &
    white, 8-bit gray and 24-bit RGB, in one or more strips.
    """
    order, tags = tiff_tags(data)
    try:
        width, height = tags[256][0], tags[257][0]
        offsets, counts = tags[273], tags[279]
    except (KeyError, IndexError):
        die("could not read TIFF image")
    check_image_size(width, height)
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
    needed = row * height
    if needed > MAX_IMAGE_BYTES:
        die("TIFF image is too large")
    if len(offsets) != len(counts):
        die("could not read TIFF image")
    strips, collected = [], 0
    for offset, count in zip(offsets, counts):
        if offset < 0 or count < 0 or offset + count > len(data):
            die("TIFF image data is out of range")
        strips.append(data[offset:offset + count])
        collected += count
        if collected >= needed:
            break  # strips may not repeat data to make the image larger
    if collected < needed:
        die("TIFF image data is truncated")
    pixels = b"".join(strips)[:needed]
    if tags.get(266, [1])[0] == 2:  # FillOrder: least significant bit first
        pixels = pixels.translate(REVERSE_BITS)
    return {
        "width": width, "height": height, "bits": bits[0],
        "colorspace": "DeviceRGB" if samples == 3 else "DeviceGray",
        "invert": photometric == 0,  # WhiteIsZero: 0 means white
        "pixels": pixels,
    }



# --- blank page detection ------------------------------------------------------
# A page is reduced to the average brightness (0-255) of each 8x8 pixel
# block: for JPEG straight from the DC coefficients, without a full decode.
# Ignore this much of each edge: shadows, feeder marks and, at the sides,
# filing holes (12 mm from the edge on A4, on either side of the sheet).
BLANK_MARGIN_X = 0.09
BLANK_MARGIN_Y = 0.05
BLANK_MIN_PAPER = 100  # darker "paper" means the page is not blank
BLANK_INK_DELTA = 32  # a block this much darker than the paper holds ink;
# light show-through from the back of the sheet stays below it
# At most this share of ink blocks (at least 2): blank. At 300 dpi that is
# about 2 mm² of ink: a speck of dust, but not a page number or initials.
BLANK_MAX_INK = 0.00004
# A JPEG this large per pixel holds too much detail for a blank page (a blank
# page compresses to a fraction of it), so it is not decoded at all.
BLANK_MAX_JPEG_BITS = 4  # bits per pixel
# Larger pages are not analyzed (A4 at 1200 dpi has 140 million pixels): a
# crafted header must not keep the scan busy for minutes.
BLANK_MAX_PIXELS = 150_000_000


def page_is_blank(image):
    """True if a scanned JPEG/TIFF page is (nearly) empty paper. Pages that
    cannot be analyzed count as not blank: they are never dropped by mistake."""
    try:
        width, height = image_size(image)
        if width * height > BLANK_MAX_PIXELS:
            return False
        if image_kind(image) == "tiff":
            grid = tiff_block_grid(image)
        else:
            if len(image) * 8 > BLANK_MAX_JPEG_BITS * width * height:
                return False
            grid = jpeg_block_grid(image)
    except ScanError:
        return False
    return grid is not None and grid_is_blank(*grid)


def grid_is_blank(columns, rows, values):
    """Decide on block brightness values (row by row, columns x rows)."""
    mx, my = round(columns * BLANK_MARGIN_X), round(rows * BLANK_MARGIN_Y)
    inner = [v for y in range(my, rows - my)
             for v in values[y * columns + mx:(y + 1) * columns - mx]]
    if len(inner) < 16:
        return False
    paper = sorted(inner)[int(len(inner) * 0.9)]
    if paper < BLANK_MIN_PAPER:
        return False
    limit = paper - BLANK_INK_DELTA
    ink = sum(1 for v in inner if v < limit)
    return ink <= max(2, BLANK_MAX_INK * len(inner))


def block_sums(pixels, width, height, stride, offset=0, step=1, group=8):
    """Sums of the blocks of 8 rows and `group` samples of one 8-bit channel,
    row by row. Edge blocks are smaller; callers divide by block_areas()."""
    rows = (height + 7) // 8
    sums = []
    for by in range(rows):
        acc = [0] * width
        for y in range(by * 8, min(by * 8 + 8, height)):
            start = y * stride + offset
            acc = list(map(int.__add__, acc, pixels[start:start + width * step:step]))
        sums.extend(sum(acc[x:x + group]) for x in range(0, width, group))
    return sums


def block_areas(width, height):
    columns, rows = (width + 7) // 8, (height + 7) // 8
    return [min(8, width - 8 * bx) * min(8, height - 8 * by)
            for by in range(rows) for bx in range(columns)]


BIT_COUNTS = bytes(bin(i).count("1") for i in range(256))


def tiff_block_grid(data):
    img = tiff_image(data)
    width, height, pixels = img["width"], img["height"], img["pixels"]
    areas = block_areas(width, height)
    if img["bits"] == 1:
        # One byte = 8 pixels side by side: count the set bits per block.
        stride = (width + 7) // 8
        ones = block_sums(pixels.translate(BIT_COUNTS), stride, height, stride, group=1)
        ones_are_black = img["invert"]  # WhiteIsZero; else BlackIsZero: 1 = white
        # (min/max: padding bits at the end of a row may be set)
        values = [min(255, max(0, 255 * (area - n if ones_are_black else n) / area))
                  for n, area in zip(ones, areas)]
    elif img["colorspace"] == "DeviceGray":
        sums = block_sums(pixels, width, height, width)
        values = [s / area for s, area in zip(sums, areas)]
        if img["invert"]:
            values = [255 - v for v in values]
    else:
        channels = [block_sums(pixels, width, height, width * 3, c, 3) for c in range(3)]
        values = [(0.299 * r + 0.587 * g + 0.114 * b) / area
                  for r, g, b, area in zip(*channels, areas)]
    return (width + 7) // 8, (height + 7) // 8, values


def huffman_lookup(counts, symbols):
    """Table indexed by the next 16 bits: (code length, symbol)."""
    table = [None] * 65536
    code, k = 0, 0
    for length in range(1, 17):
        for _ in range(counts[length - 1]):
            if k >= len(symbols) or code >= 1 << length:
                return None
            first = code << (16 - length)
            table[first:first + (1 << (16 - length))] = [(length, symbols[k])] * (1 << (16 - length))
            code += 1
            k += 1
        code <<= 1
    return table


def jpeg_block_grid(data):
    """Average brightness of each 8x8 block of a baseline JPEG's first (luma
    or gray) component, from the DC coefficients: (columns, rows, values).
    None for JPEGs this does not read (progressive, arithmetic, 12-bit,
    several scans); raises ScanError for broken data."""
    quant, dc_tables, ac_tables = {}, {}, {}
    frame, restart = None, 0
    i = 2
    while True:
        while i < len(data) and data[i] == 0xFF and i + 1 < len(data) and data[i + 1] == 0xFF:
            i += 1
        if i + 4 > len(data) or data[i] != 0xFF:
            die("could not read JPEG image")
        marker = data[i + 1]
        length = int.from_bytes(data[i + 2:i + 4], "big")
        segment = data[i + 4:i + 2 + length]
        if length < 2 or len(segment) != length - 2:
            die("could not read JPEG image")
        i += 2 + length
        if marker == 0xDB:  # quantization tables: only the DC value is needed
            j = 0
            while j < len(segment):
                precision, table_id = segment[j] >> 4, segment[j] & 15
                size = 128 if precision else 64
                if j + 1 + size > len(segment):
                    die("could not read JPEG image")
                quant[table_id] = (int.from_bytes(segment[j + 1:j + 3], "big") if precision
                                   else segment[j + 1])
                j += 1 + size
        elif marker == 0xC4:  # Huffman tables
            j = 0
            while j < len(segment):
                table_class, table_id = segment[j] >> 4, segment[j] & 15
                counts = segment[j + 1:j + 17]
                total = sum(counts)
                symbols = segment[j + 17:j + 17 + total]
                if len(counts) != 16 or len(symbols) != total:
                    die("could not read JPEG image")
                table = huffman_lookup(counts, symbols)
                if table is None:
                    die("could not read JPEG image")
                (ac_tables if table_class else dc_tables)[table_id] = table
                j += 17 + total
        elif marker == 0xDD and len(segment) >= 2:
            restart = int.from_bytes(segment[:2], "big")
        elif marker in (0xC0, 0xC1):  # baseline / extended sequential, Huffman
            if len(segment) < 6 or segment[0] != 8:
                return None
            height = int.from_bytes(segment[1:3], "big")
            width = int.from_bytes(segment[3:5], "big")
            check_image_size(width, height)
            count = segment[5]
            comps = [segment[6 + 3 * k:9 + 3 * k] for k in range(count)]
            if not comps or any(len(c) != 3 for c in comps):
                die("could not read JPEG image")
            frame = (width, height, [(c[0], c[1] >> 4, c[1] & 15, c[2]) for c in comps])
        elif 0xC2 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            return None  # progressive, lossless, hierarchical or arithmetic coding
        elif marker == 0xDA:
            break
        elif marker == 0xD9:
            die("could not read JPEG image")
    if frame is None or not segment:
        die("could not read JPEG image")
    width, height, comps = frame
    scan = [segment[1 + 2 * k:3 + 2 * k] for k in range(segment[0])]
    if len(scan) != len(comps):
        return None  # components in separate scans
    hmax = max(c[1] for c in comps)
    vmax = max(c[2] for c in comps)
    if not all(1 <= c[1] <= 4 and 1 <= c[2] <= 4 for c in comps):
        die("could not read JPEG image")
    try:
        units = []  # per component: (blocks h, blocks v, DC table, AC table)
        for (_id, h, v, _tq), (_sid, tables) in zip(comps, scan):
            units.append((h, v, dc_tables[tables >> 4], ac_tables[tables & 15]))
        q0 = quant[comps[0][3]]
    except KeyError:
        die("could not read JPEG image")
    if len(comps) == 1:  # non-interleaved: one block per MCU
        units = [(1, 1) + units[0][2:]]
        mcu_columns, mcu_rows = (width + 7) // 8, (height + 7) // 8
        h0 = v0 = 1
    else:
        mcu_columns = -(-width // (8 * hmax))
        mcu_rows = -(-height // (8 * vmax))
        h0, v0 = comps[0][1], comps[0][2]
    grid_columns, grid_rows = mcu_columns * h0, mcu_rows * v0
    dc = [0] * (grid_columns * grid_rows)

    # Entropy-coded data up to the end marker, split at restart markers.
    end = data.find(b"\xff\xd9", i)
    coded = data[i:end if end >= 0 else len(data)]
    parts = re.split(rb"\xff[\xd0-\xd7]", coded)
    total = mcu_columns * mcu_rows
    per_part = restart or total
    mcu = 0
    for part in parts:
        if mcu >= total:
            break
        # Pad with 1 bits, as JPEG does, so reading ahead never runs out.
        bits = part.replace(b"\xff\x00", b"\xff") + b"\xff" * 8
        limit = len(bits) - 4
        acc = nbits = p = 0
        preds = [0] * len(units)
        for _ in range(min(per_part, total - mcu)):
            row, column = divmod(mcu, mcu_columns)
            base = row * v0 * grid_columns + column * h0
            for u, (h, v, dc_table, ac_table) in enumerate(units):
                for block in range(h * v):
                    if nbits < 32:  # at most 16 code + 16 value bits per symbol
                        if p > limit:
                            die("JPEG image data is truncated")
                        acc = ((acc & ((1 << nbits) - 1)) << 32) | int.from_bytes(
                            bits[p:p + 4], "big")
                        p += 4
                        nbits += 32
                    entry = dc_table[(acc >> (nbits - 16)) & 0xFFFF]
                    if entry is None:
                        die("could not read JPEG image")
                    nbits -= entry[0]
                    size = entry[1]
                    if size:
                        diff = (acc >> (nbits - size)) & ((1 << size) - 1)
                        if diff < 1 << (size - 1):
                            diff -= (1 << size) - 1
                        nbits -= size
                        preds[u] += diff
                    if u == 0:
                        dc[base + (block // h) * grid_columns + block % h] = preds[0]
                    k = 1
                    while k < 64:  # the AC coefficients are only skipped
                        if nbits < 32:
                            if p > limit:
                                die("JPEG image data is truncated")
                            acc = ((acc & ((1 << nbits) - 1)) << 32) | int.from_bytes(
                                bits[p:p + 4], "big")
                            p += 4
                            nbits += 32
                        entry = ac_table[(acc >> (nbits - 16)) & 0xFFFF]
                        if entry is None:
                            die("could not read JPEG image")
                        rs = entry[1]
                        nbits -= entry[0] + (rs & 15)
                        if rs == 0:
                            break
                        k += (rs >> 4) + 1
            mcu += 1
    if mcu < total:
        die("JPEG image data is truncated")
    columns, rows = -(-width // 8), -(-height // 8)  # blocks inside the image
    values = [min(255, max(0, dc[y * grid_columns + x] * q0 / 8 + 128))
              for y in range(rows) for x in range(columns)]
    return columns, rows, values


# --- Page corrections: paper size, straightening, orientation -----------------
# A scan with paper "auto" covers the scanner's whole scan area. The sheet
# itself is found on the block grid: it is brighter than the feeder's backing
# (gray or black on most ADF scanners); rows of one color at the end (some
# scanners fill the rest of the length with white) are cut off. The smallest
# rectangle around the sheet gives its size and how crooked it was fed.
# Corrections are applied as page geometry in the PDF: the scanned image is
# embedded unchanged, only placed, rotated and cut to the page.

BLOCK = 8  # pixels per grid block
PAPER_MIN_CONTRAST = 20  # the sheet must be this much brighter than the backing
PAPER_MIN_AREA = 0.02  # of the image; smaller "sheets" are specks
PAPER_MIN_RECT_FILL = 0.85  # sheet outline / its rectangle; less is not a sheet
PAPER_INSET = 0.5  # blocks cut off the sheet's edges: the outline found lies about
# this much outside the sheet (edge blocks are partly backing)
EDGE_SHADOW_MM = 0.35  # and the shadow along a sheet's edge
SKEW_MIN = 0.15  # degrees; less is not worth straightening
SKEW_MAX = 20  # degrees; more is not a crooked feed
PAD_FLAT = 4  # brightness range of a padding row (the same color right to the end)
UNIFORM_ROW = 16  # brightness range of a row without a sheet edge (e.g. backing blending
# into padding); a row with paper and backing spans far more
# Orientation from Tesseract (--psm 0): at least this confidence. Pages with
# too little text (or handwriting) give none and stay as they are.
OSD_MIN_CONFIDENCE = 4.0
OSD_TIMEOUT = 60
CROP_CHOICES = ("sides", "all")  # cut the sheet's left and right edges, or all four
CORRECTIONS = ("crop", "skew", "rotate")  # page_layout() applies the ones in info["use"]


def page_grid(image, limit_jpeg_bits=False):
    """(columns, rows, values) of BLOCK x BLOCK brightness, or None if the
    image cannot (or should not) be analyzed."""
    try:
        width, height = image_size(image)
        if width * height > BLANK_MAX_PIXELS:
            return None
        if image_kind(image) == "tiff":
            return tiff_block_grid(image)
        if limit_jpeg_bits and len(image) * 8 > BLANK_MAX_JPEG_BITS * width * height:
            return None
        return jpeg_block_grid(image)
    except ScanError:
        return None


def _convex_hull(points):
    points = sorted(set(points))
    if len(points) < 3:
        return points

    def half(seq):
        out = []
        for p in seq:
            while len(out) >= 2 and ((out[-1][0] - out[-2][0]) * (p[1] - out[-2][1])
                                     - (out[-1][1] - out[-2][1]) * (p[0] - out[-2][0])) <= 0:
                out.pop()
            out.append(p)
        return out

    lower, upper = half(points), half(reversed(points))
    return lower[:-1] + upper[:-1]


def _polygon_area(hull):
    return abs(sum(x0 * y1 - x1 * y0 for (x0, y0), (x1, y1)
                   in zip(hull, hull[1:] + hull[:1]))) / 2


def _min_area_rect(hull):
    """(cx, cy, width, height, angle in degrees within [-45, 45]) of the
    smallest rectangle around a convex polygon (y axis pointing down)."""
    best = None
    for (x0, y0), (x1, y1) in zip(hull, hull[1:] + hull[:1]):
        length = math.hypot(x1 - x0, y1 - y0)
        if length == 0:
            continue
        ux, uy = (x1 - x0) / length, (y1 - y0) / length
        along = [x * ux + y * uy for x, y in hull]
        across = [-x * uy + y * ux for x, y in hull]
        area = (max(along) - min(along)) * (max(across) - min(across))
        if best is None or area < best[0]:
            best = (area, ux, uy, along, across)
    if best is None:
        return None
    _area, ux, uy, along, across = best
    a0, a1, b0, b1 = min(along), max(along), min(across), max(across)
    ca, cb = (a0 + a1) / 2, (b0 + b1) / 2
    cx, cy = ca * ux - cb * uy, ca * uy + cb * ux
    width, height = a1 - a0, b1 - b0
    angle = math.degrees(math.atan2(uy, ux))
    while angle > 45:  # the side closest to horizontal is the width
        angle -= 90
        width, height = height, width
    while angle <= -45:
        angle += 90
        width, height = height, width
    return cx, cy, width, height, angle


def scanned_rows(columns, rows, values):
    """Grid rows up to the padding after the sheet: some scanners fill the
    rest of the scan length with rows of one color once the sheet has gone
    through. All rows if there is none, or it is the paper's color (black &
    white: cannot tell)."""
    def row(r):
        return values[r * columns:(r + 1) * columns]

    def spread(r):
        return max(row(r)) - min(row(r))

    end = rows
    if rows and columns and spread(rows - 1) <= PAD_FLAT:
        pad = sum(row(rows - 1)) / columns
        while (end > 0 and spread(end - 1) <= PAD_FLAT
               and abs(sum(row(end - 1)) / columns - pad) <= PAD_FLAT):
            end -= 1
        above = sorted(values[:end * columns])
        if above and abs(above[int(len(above) * 0.9)] - pad) <= 2 * PAD_FLAT:
            end = rows
    return end


def find_paper(columns, rows, values):
    """The scanned sheet on a block grid: (cx, cy, width, height, angle) in
    blocks, angle in degrees (clockwise on screen), or None if no sheet stands
    out from the background. Padding after the sheet (rows of the last row's
    color, as some scanners fill the rest of the scan length) is not part of
    it; without a sheet to find, only the padding is cut off."""
    def row(r):
        return values[r * columns:(r + 1) * columns]

    def spread(r):
        return max(row(r)) - min(row(r))

    first, end = 0, scanned_rows(columns, rows, values)
    if end - first < 4 or columns < 4:
        return None
    trimmed = (columns / 2, end / 2, columns, end, 0.0) if end < rows else None
    inside = [v for r in range(first, end) for v in row(r)]
    border = ([values[r * columns] for r in range(first, end)]  # mostly backing
              + [values[r * columns + columns - 1] for r in range(first, end)] + row(first))
    backing = sorted(border)[len(border) // 2]
    paper = sorted(inside)[int(len(inside) * 0.9)]
    if paper - backing < PAPER_MIN_CONTRAST:
        return trimmed  # no contrast (e.g. white backing): only cut the padding
    threshold = (backing + paper) / 2

    def coverage(v):  # share of a block covered by paper, from its brightness
        return min(1.0, max(0.0, (v - backing) / (paper - backing)))

    # The last row before the padding blends into it (no edges from there).
    edge_end = end - 1 if end < rows else end
    lit = [[v > threshold for v in row(r)] if first <= r < edge_end and spread(r) > UNIFORM_ROW
           else [False] * columns for r in range(rows)]
    points = []
    for r in range(first, edge_end):  # left and right edge in each row
        cells = lit[r]
        hits = [c for c in range(columns - 1) if cells[c] and cells[c + 1]]
        if not hits:
            continue
        left, right = hits[0], hits[-1] + 1
        v = row(r)
        x0 = left - (coverage(v[left - 1]) if left > 0 else 0)
        x1 = right + 1 + (coverage(v[right + 1]) if right + 1 < columns else 0)
        points += [(x0, r + 0.5), (x1, r + 0.5)]
    for c in range(columns):  # top and bottom edge in each column
        hits = [r for r in range(first, end - 1) if lit[r][c] and lit[r + 1][c]]
        if not hits:
            continue
        top, bottom = hits[0], hits[-1] + 1
        y0 = top - (coverage(values[(top - 1) * columns + c]) if top > first else 0)
        y1 = bottom + 1 + (coverage(values[(bottom + 1) * columns + c])
                           if bottom + 1 < end else 0)
        points += [(c + 0.5, y0), (c + 0.5, y1)]
    hull = _convex_hull(points)
    if len(hull) < 4:
        return trimmed
    rect = _min_area_rect(hull)
    if rect is None:
        return trimmed
    cx, cy, width, height, angle = rect
    if (width * height < PAPER_MIN_AREA * columns * rows
            or _polygon_area(hull) < PAPER_MIN_RECT_FILL * width * height
            or abs(angle) > SKEW_MAX):
        return trimmed
    return (cx, cy, max(1.0, width - 2 * PAPER_INSET), max(1.0, height - 2 * PAPER_INSET), angle)


def sheet_in_pixels(rect, size, dpi):
    """find_paper()'s rectangle in image pixels, inside the sheet's edge
    shadow; an upright one also within the image."""
    cx, cy, width, height = (v * BLOCK for v in rect[:4])
    angle = rect[4]
    if angle:
        shadow = dpi * EDGE_SHADOW_MM / 25.4
        width, height = max(1.0, width - 2 * shadow), max(1.0, height - 2 * shadow)
    else:  # upright: within the image (the grid's last blocks may be partial)
        x0, x1 = max(0, cx - width / 2), min(size[0], cx + width / 2)
        y0, y1 = max(0, cy - height / 2), min(size[1], cy + height / 2)
        cx, cy, width, height = (x0 + x1) / 2, (y0 + y1) / 2, x1 - x0, y1 - y0
    return cx, cy, width, height, angle


def inner_region(rect, columns, rows):
    """Block range (c0, r0, c1, r1) inside a (possibly tilted) sheet."""
    cx, cy, width, height, angle = rect
    a = math.radians(abs(angle))
    half_w = width / 2 * math.cos(a) - height / 2 * math.sin(a)
    half_h = height / 2 * math.cos(a) - width / 2 * math.sin(a)
    if half_w <= 1 or half_h <= 1:
        return None
    return (max(0, math.ceil(cx - half_w)), max(0, math.ceil(cy - half_h)),
            min(columns, math.floor(cx + half_w)), min(rows, math.floor(cy + half_h)))


def tesseract_osd_available(path=None):
    path = path or shutil.which("tesseract")
    if not path:
        return False
    try:
        result = subprocess.run([path, "--list-langs"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return False
    return "osd" in {line.strip() for line in (result.stdout or result.stderr).splitlines()[1:]}


def detect_rotation(image, dpi, tesseract=None):
    """Clockwise rotation (0, 90, 180, 270) that turns the page upright, from
    Tesseract's orientation detection; 0 if unsure or not possible."""
    tesseract = tesseract or shutil.which("tesseract")
    if not tesseract:
        return 0
    with tempfile.TemporaryDirectory(prefix="wsdscan-osd-") as tmp:
        name = os.path.join(tmp, "page." + ("tif" if image_kind(image) == "tiff" else "jpg"))
        with open(name, "wb") as f:
            f.write(image)
        try:
            result = subprocess.run([tesseract, name, "-", "--psm", "0", "--dpi", str(dpi)],
                                    capture_output=True, text=True, timeout=OSD_TIMEOUT)
        except (OSError, subprocess.SubprocessError):
            return 0
    rotate = re.search(r"^Rotate:\s*(\d+)", result.stdout, re.M)
    confidence = re.search(r"^Orientation confidence:\s*([\d.]+)", result.stdout, re.M)
    if not rotate or not confidence or float(confidence.group(1)) < OSD_MIN_CONFIDENCE:
        return 0
    value = int(rotate.group(1))
    return value if value in (90, 180, 270) else 0


def analyze_page(image, args, dpi, osd=None):
    """What to do with one scanned page. Returns a dict:
      size    (width, height) in pixels
      blank   True if the page is empty paper (args.skip_blank)
      paper   (cx, cy, width, height, angle) of the sheet in pixels, or None
      crop    True if the page can be cut to the sheet (paper "auto")
      crop_edges  "sides": cut left and right only, the scanned length stays
              (the scanner finds the sheet's start and end); "all": all four.
              A straightened page is always cut to the sheet's four edges.
      length  rows of the scan up to the padding after the sheet (pixels)
      skew    degrees to straighten (args.deskew), else 0
      rotate  clockwise degrees to turn it upright (args.auto_rotate), else 0
      orientation  the same, detected also without args.auto_rotate (only
              with osd; for the direction of the OCR text)
      use     {correction: True}: page_layout() applies these; may be changed
      turn    clockwise degrees (0, 90, 180, 270) to turn the page by hand on
              top of the corrections; 0, may be changed (the app's review)
    osd: Tesseract path for orientation detection (None: not wanted).
    """
    info = {"size": (0, 0), "blank": False, "paper": None, "crop": False,
            "crop_edges": getattr(args, "crop", "sides") or "sides", "length": 0, "skew": 0.0,
            "rotate": 0, "use": {name: True for name in CORRECTIONS}, "turn": 0}
    try:
        info["size"] = image_size(image)
    except ScanError:
        return info
    info["length"] = info["size"][1]
    auto = getattr(args, "paper", "") == "auto"
    deskew = bool(getattr(args, "deskew", False))
    grid = page_grid(image, limit_jpeg_bits=not (auto or deskew))
    rect = find_paper(*grid) if grid and (auto or deskew) else None
    if grid and (auto or deskew):
        info["length"] = min(info["size"][1], scanned_rows(*grid) * BLOCK)
    if rect:
        info["paper"] = sheet_in_pixels(rect, info["size"], dpi)
        angle = rect[4]
        info["crop"] = auto
        if deskew and SKEW_MIN <= abs(angle) <= SKEW_MAX:
            info["skew"] = angle
    if args.skip_blank and grid:
        region = inner_region(rect, grid[0], grid[1]) if rect else None
        if region:
            c0, r0, c1, r1 = region
            cols = c1 - c0
            sub = [v for r in range(r0, r1) for v in grid[2][r * grid[0] + c0:r * grid[0] + c1]]
            info["blank"] = cols > 0 and grid_is_blank(cols, r1 - r0, sub)
        elif not auto:
            info["blank"] = page_is_blank(image)
    if osd and not info["blank"]:
        info["orientation"] = detect_rotation(image, dpi, osd)
        if getattr(args, "auto_rotate", False):
            info["rotate"] = info["orientation"]
    return info


def page_layout(info):
    """(page_width, page_height, place) in pixels for a page with its
    corrections in info["use"]; place(x, y) maps image pixels (y down) to
    page pixels (y down)."""
    page_w, page_h, cx, cy, angle = page_geometry(info)
    theta = math.radians(angle)
    cos, sin = math.cos(theta), math.sin(theta)

    def place(x, y):
        dx, dy = x - cx, y - cy
        return dx * cos - dy * sin + page_w / 2, dx * sin + dy * cos + page_h / 2

    return page_w, page_h, place


def page_geometry(info):
    """(page_width, page_height, cx, cy, angle): the image point (cx, cy)
    goes to the page's center, the image turned by angle degrees (clockwise
    on screen) around it."""
    width, height = info["size"]
    use = info["use"]
    skew = info["skew"] if use.get("skew") else 0.0
    if info["crop"] and use.get("crop") and info["paper"]:
        cx, cy, w, h, angle = info["paper"]
        # Straightened, the page is the sheet: the scan's first and last rows
        # are tilted against it (crop_edges "sides" would leave wedges of backing).
        if not skew:  # cut to the sheet without turning it: its upright box, in the image
            a = math.radians(angle)
            half_w = (w * abs(math.cos(a)) + h * abs(math.sin(a))) / 2
            half_h = (w * abs(math.sin(a)) + h * abs(math.cos(a))) / 2
            x0, x1 = max(0, cx - half_w), min(width, cx + half_w)
            y0, y1 = max(0, cy - half_h), min(height, cy + half_h)
            if info.get("crop_edges", "all") == "sides":  # as scanned, without the padding
                y0, y1 = 0, info.get("length") or height
            cx, cy, w, h = (x0 + x1) / 2, (y0 + y1) / 2, max(1, x1 - x0), max(1, y1 - y0)
    else:
        cx, cy, w, h = width / 2, height / 2, width, height
    rotate = page_turn(info)
    page_w, page_h = (h, w) if rotate in (90, 270) else (w, h)
    return page_w, page_h, cx, cy, rotate - skew


def page_turn(info):
    """Clockwise degrees (0, 90, 180, 270) the page is turned: upright
    (if used) plus by hand."""
    rotate = info["rotate"] if info["use"].get("rotate") else 0
    return (rotate + info.get("turn", 0)) % 360


def crop_size(info):
    """(width, height) in pixels of the part of the scan the page is cut
    to (crop on, before turning)."""
    page_w, page_h = page_geometry(dict(info, use=dict(info["use"], crop=True, rotate=False),
                                        turn=0))[:2]
    return page_w, page_h


def corrections_done(info):
    """The corrections that change this page, e.g. ["crop", "rotate"]."""
    done = []
    if info["crop"] and info["paper"]:
        done.append("crop")
    if info["skew"]:
        done.append("skew")
    if info["rotate"]:
        done.append("rotate")
    return done


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


def _affine(f):
    """PDF matrix [a b c d e f] of an affine map, from three of its points."""
    (x0, y0), (x1, y1), (x2, y2) = f(0, 0), f(1, 0), f(0, 1)
    return (x1 - x0, y1 - y0, x2 - x0, y2 - y0, x0, y0)


def _num(value):
    text = f"{value:.4f}".rstrip("0").rstrip(".")
    return "0" if text in ("-0", "") else text


def _matrix(m):
    return " ".join(_num(v) for v in m)


# Invisible text layer (Tesseract without OCRmyPDF): words are drawn with a
# glyphless font in text render mode 3, like Tesseract's own PDF output. Codes
# are Unicode code points (Identity-H), mapped back to text by a ToUnicode CMap.
TEXT_CMAP = ("/CIDInit /ProcSet findresource begin 12 dict begin begincmap\n"
             "/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def\n"
             "/CMapName /Adobe-Identity-UCS def /CMapType 2 def\n"
             "1 begincodespacerange <0000> <FFFF> endcodespacerange\n"
             + "".join(f"{min(100, 256 - start)} beginbfrange\n"
                       + "".join(f"<{h:02X}00> <{h:02X}FF> <{h:02X}00>\n"
                                 for h in range(start, min(start + 100, 256)))
                       + "endbfrange\n" for start in range(0, 256, 100))
             + "endcmap CMapName currentdict /CMap defineresource pop end end\n")


def text_layer(words, info):
    """Content stream operators (in image pixels, y down) for OCR words:
    [(text, left, top, width, height)] in the scanned image's pixels."""
    if not info:
        turn = 0
    elif info.get("turn"):  # turned by hand: that is the reading direction
        turn = page_turn(info)
    else:
        turn = info.get("orientation", info.get("rotate", 0))
    angle = info["paper"][4] if info and info.get("paper") else 0.0
    phi = math.radians(angle - turn)  # reading direction in the image
    cos, sin = math.cos(phi), math.sin(phi)
    ops = ["BT 3 Tr"]
    for text, left, top, width, height in words:
        codes = [ord(ch) if ord(ch) <= 0xFFFF and not 0xD800 <= ord(ch) <= 0xDFFF else 0xFFFD
                 for ch in text]
        if not codes or width <= 0 or height <= 0:
            continue
        if turn == 90:
            x, y, length, size = left + width, top + height, height, width
        elif turn == 180:
            x, y, length, size = left + width, top, width, height
        elif turn == 270:
            x, y, length, size = left, top, height, width
        else:
            x, y, length, size = left, top + height, width, height
        scale = max(1.0, min(1000.0, 100 * length / (0.5 * size * len(codes))))
        ops.append(f"/F1 {_num(size)} Tf {_num(scale)} Tz "
                   f"{_matrix((cos, sin, sin, -cos, x, y))} Tm "
                   f"<{''.join(f'{c:04X}' for c in codes)}> Tj")
    ops.append("ET")
    return " ".join(ops)


def pdf_bytes(images, dpi, pages=None, words=None, font_file=None):
    """Build a PDF with one scanned image per page.

    JPEGs are embedded unchanged (DCTDecode); TIFFs are stored losslessly
    with zlib (FlateDecode). pages: analyze_page() results; their corrections
    (in "use") place, turn and cut the image on its page. words: per page,
    OCR words for an invisible text layer (see text_layer); font_file: a
    glyphless TrueType font to embed for it (Tesseract's pdf.ttf).
    """
    objects = [b"", b""]  # 1 = catalog, 2 = page tree; filled in below
    scale = 72 / dpi

    def add(obj):
        objects.append(obj)
        return len(objects)

    font = None
    if words and any(words):
        descriptor = ("<< /Type /FontDescriptor /FontName /GlyphLessFont /Flags 5 "
                      "/FontBBox [0 0 500 1000] /ItalicAngle 0 /Ascent 1000 /Descent 0 "
                      "/CapHeight 1000 /StemV 80")
        if font_file:
            data = zlib.compress(font_file, 6)
            embedded = add(f"<< /Length {len(data)} /Length1 {len(font_file)} "
                           f"/Filter /FlateDecode >>\nstream\n".encode() + data + b"\nendstream")
            descriptor += f" /FontFile2 {embedded} 0 R"
        descriptor = add((descriptor + " >>").encode())
        gids = zlib.compress(b"\x00\x01" * 65536, 9)  # every code shows glyph 1 (none)
        gid_map = add(f"<< /Length {len(gids)} /Filter /FlateDecode >>\nstream\n".encode()
                      + gids + b"\nendstream")
        cid = add(f"<< /Type /Font /Subtype /CIDFontType2 /BaseFont /GlyphLessFont "
                  f"/CIDSystemInfo << /Registry (Adobe) /Ordering (Identity) /Supplement 0 >> "
                  f"/FontDescriptor {descriptor} 0 R /DW 500 /CIDToGIDMap {gid_map} 0 R >>"
                  .encode())
        cmap = zlib.compress(TEXT_CMAP.encode(), 9)
        to_unicode = add(f"<< /Length {len(cmap)} /Filter /FlateDecode >>\nstream\n".encode()
                         + cmap + b"\nendstream")
        font = add(f"<< /Type /Font /Subtype /Type0 /BaseFont /GlyphLessFont /Encoding /Identity-H "
                   f"/DescendantFonts [{cid} 0 R] /ToUnicode {to_unicode} 0 R >>".encode())

    kids = []
    for n, data in enumerate(images):
        xobject, width, height = image_xobject(data)
        img = add(xobject)
        info = pages[n] if pages else None
        if info:
            page_w, page_h, place = page_layout(dict(info, size=(width, height)))
        else:
            page_w, page_h, place = width, height, lambda x, y: (x, y)
        pw, ph = page_w * scale, page_h * scale

        def to_pdf(x, y):  # image pixels (y down) -> PDF points (y up)
            px, py = place(x, y)
            return px * scale, (page_h - py) * scale

        image_matrix = _affine(lambda u, v: to_pdf(u * width, (1 - v) * height))
        content = f"q {_matrix(image_matrix)} cm /Im0 Do Q"
        page_words = words[n] if words and n < len(words) else None
        if page_words:
            content += f" q {_matrix(_affine(to_pdf))} cm {text_layer(page_words, info)} Q"
        content = content.encode()
        stream = add(f"<< /Length {len(content)} >>\nstream\n".encode()
                     + content + b"\nendstream")
        fonts = f" /Font << /F1 {font} 0 R >>" if font and page_words else ""
        kids.append(add(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {_num(pw)} {_num(ph)}] "
            f"/Resources << /XObject << /Im0 {img} 0 R >>{fonts} >> /Contents {stream} 0 R >>"
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


def write_file(path, data, overwrite=False):
    """Write `data` to `path` without ever writing through a symlink.

    The data goes to a new temporary file next to `path` first. Then it is
    moved into place (overwrite) or hard-linked, which fails if `path`
    exists in any form, including a dangling symlink planted by someone else.
    """
    directory = os.path.dirname(os.path.abspath(path)) or "."
    tmp = os.path.join(directory, f".wsdscan-{secrets.token_hex(8)}.part")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                     0o666)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        if overwrite:
            os.replace(tmp, path)
            return
        try:
            os.link(tmp, path)
        except FileExistsError:
            die(f"{path} already exists")
        except OSError:
            # File systems without hard links: create exclusively instead.
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                         | getattr(os, "O_NOFOLLOW", 0), 0o666)
            with os.fdopen(fd, "wb") as f:
                f.write(data)
    except FileExistsError:
        die(f"{path} already exists")
    except OSError as e:
        die(f"cannot write {path}: {e.strerror or e}")
    finally:
        if os.path.lexists(tmp):
            os.unlink(tmp)


def write_pdf(path, images, dpi, overwrite=False, pages=None, words=None, font_file=None):
    write_file(path, pdf_bytes(images, dpi, pages, words, font_file), overwrite)


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
    if not re.fullmatch(r"[a-z_]+(\+[a-z_]+)*", lang):
        die(f"invalid OCR languages {clean(lang)!r}: expected Tesseract codes like deu+eng")
    missing = [part for part in lang.split("+") if installed and part not in installed]
    if missing:
        die(f"OCR language(s) not installed: {', '.join(missing)}; installed: "
            f"{', '.join(installed)} (e.g. 'sudo apt install tesseract-ocr-{missing[0]}')")
    return engine, available[engine], lang


def resolve_osd(args, ocr=None):
    """Tesseract for orientation detection, or None: for args.auto_rotate
    (checked before scanning), and for the direction of Tesseract's text."""
    wanted = bool(getattr(args, "auto_rotate", False))
    if not wanted and not (ocr and ocr[0] == "tesseract"):
        return None
    tesseract = shutil.which("tesseract")
    if tesseract and tesseract_osd_available(tesseract):
        return tesseract
    if wanted:
        die("turning pages upright needs Tesseract with orientation data; install it "
            "(e.g. 'sudo apt install tesseract-ocr tesseract-ocr-osd')")
    return None


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


def tessdata_dir(tesseract):
    """Tesseract's data folder (from --list-langs), or None."""
    try:
        result = subprocess.run([tesseract, "--list-langs"], capture_output=True, text=True,
                                timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.search(r'"([^"]+)"', (result.stdout or result.stderr).split("\n", 1)[0])
    return match.group(1) if match else None


def tesseract_font(tesseract):
    """Tesseract's glyphless font for invisible text (tessdata/pdf.ttf), or None."""
    folder = tessdata_dir(tesseract)
    path = os.path.join(folder, "pdf.ttf") if folder else None
    try:
        with open(path, "rb") as f:
            data = f.read(2 ** 20 + 1)
    except (OSError, TypeError):
        return None
    return data if 0 < len(data) <= 2 ** 20 else None


def parse_tsv(text):
    """Words from Tesseract's TSV output: {page number: [(text, left, top, width, height)]}."""
    lines = text.splitlines()
    if not lines:
        return {}
    header = lines[0].split("\t")
    try:
        col = {name: header.index(name) for name in
               ("level", "page_num", "left", "top", "width", "height", "conf", "text")}
    except ValueError:
        die("text recognition gave unreadable results (TSV without the expected columns)")
    words = {}
    for line in lines[1:]:
        fields = line.split("\t")
        if len(fields) < len(header) or fields[col["level"]] != "5":
            continue
        word = fields[col["text"]].strip()
        try:
            box = [int(fields[col[k]]) for k in ("page_num", "left", "top", "width", "height")]
        except ValueError:
            continue
        if word and box[3] > 0 and box[4] > 0:
            words.setdefault(box[0], []).append((word, *box[1:]))
    return words


def run_ocr(engine, path, pdf_path, images, dpi, lang, on_page=None, pages=None):
    """Replace pdf_path by a searchable PDF. Raises ScanError (pdf_path is kept).

    on_page(n) reports recognized pages; only Tesseract reports them
    ("Page N : file" lines), OCRmyPDF has no per-page progress output.
    pages: analyze_page() results, so Tesseract's text follows the page
    corrections (OCRmyPDF reads the already corrected PDF).
    """
    with tempfile.TemporaryDirectory(prefix="wsdscan-ocr-") as tmp:
        result_pdf = os.path.join(tmp, "out.pdf")
        if engine == "ocrmypdf":
            # Plain PDF output keeps the scanned images untouched (no PDF/A conversion).
            # Absolute paths: a file name starting with "-" must not look like an option.
            cmd = [path, "-l", lang, "--output-type", "pdf", os.path.abspath(pdf_path), result_pdf]
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
            # Words with their positions (TSV) go into this tool's own PDF, so
            # the text layer follows the page corrections. With orientation
            # data, Tesseract also reads pages that are sideways or upside down.
            psm = "1" if tesseract_osd_available(path) else "3"
            cmd = [path, listing, result_pdf[:-4], "-l", lang, "--dpi", str(dpi), "--psm", psm, "tsv"]
        done = []

        def on_line(line):
            match = TESSERACT_PAGE_LINE.match(line)
            if match and on_page and engine == "tesseract":
                done.append(int(match.group(1)))
                on_page(len(done))

        code, output = run_command(cmd, on_line)
        if engine == "tesseract":
            result_pdf = result_pdf[:-4] + ".tsv"
        if code != 0 or not os.path.exists(result_pdf):
            details = [clean(line) for line in output if line.strip()][-3:]
            die(f"text recognition with {engine} failed (exit {code})"
                + (": " + " / ".join(details) if details else ""))
        with open(result_pdf, "rb") as f:
            result = f.read()
        if engine == "tesseract":
            words = parse_tsv(result.decode("utf-8", "replace"))
            result = pdf_bytes(images, dpi, pages, [words.get(n, []) for n in range(1, len(images) + 1)],
                               tesseract_font(path))
        write_file(pdf_path, result, overwrite=True)  # replaces our own file atomically


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
    "review_pages": False,  # desktop app: review pages before saving (ignored by the CLI)
    "skip_blank": False,  # remove blank pages
    "deskew": False,  # straighten pages that were fed crooked
    "auto_rotate": False,  # turn pages upright (Tesseract orientation detection)
    "crop": "sides",  # paper "auto": cut the sheet's left and right edges, or "all" four
}
SCANNER_SECTION = "scanner "  # profiles: [scanner Office], [scanner Home], ...
CONFIG_CHOICES = {
    "ocr_engine": OCR_ENGINE_CHOICES,
    "source": ("adf", "duplex"),
    "mode": tuple(COLOR_ENTRIES),
    "paper": tuple(PAPER_SIZES),
    "crop": CROP_CHOICES,
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
    if key in ("lossless", "ocr", "review_pages", "skip_blank", "deskew", "auto_rotate"):
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

def resolution_value(text):
    value = parse_int(text, 50, 2400)
    if value is None:
        raise argparse.ArgumentTypeError("expected a resolution from 50 to 2400 dpi")
    return value


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
    p.add_argument("--skip-blank", action=argparse.BooleanOptionalAction,
                   default=cfg["skip_blank"],
                   help="remove blank pages, e.g. the empty backs of a duplex scan; if "
                        "all pages are blank, they are kept (default: %(default)s)")
    p.add_argument("--deskew", action=argparse.BooleanOptionalAction, default=cfg["deskew"],
                   help="straighten pages that were fed crooked; finds the sheet's edges, "
                        "so it works best with --paper auto (default: %(default)s)")
    p.add_argument("--auto-rotate", action=argparse.BooleanOptionalAction,
                   default=cfg["auto_rotate"],
                   help="turn pages that are sideways or upside down upright; needs "
                        "Tesseract with orientation data (default: %(default)s)")
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
    p.add_argument("-r", "--resolution", type=resolution_value, default=cfg["resolution"],
                   help="dpi (default: %(default)s)")
    p.add_argument("-p", "--paper", choices=tuple(PAPER_SIZES), default=cfg["paper"],
                   help="paper size; auto scans the whole scan area and cuts each page "
                        "to its sheet (default: %(default)s)")
    p.add_argument("--crop", choices=CROP_CHOICES, default=cfg["crop"],
                   help="with --paper auto: cut the sheet's left and right edges only and "
                        "keep the scanned length, as the scanner finds the sheet's start "
                        "and end (sides), or cut all four edges (all); straightened pages "
                        "are cut to all four (default: %(default)s)")
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
    args.review_pages = cfg["review_pages"]  # desktop app only; shown by --show-config
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
    print("Orientation detection (--auto-rotate): "
          + ("available" if tesseract_osd_available()
             else "not installed (install tesseract-ocr and tesseract-ocr-osd)"))


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

    blank = []

    def page_image(n, image, info):
        done = []
        if info["crop"] and info["paper"]:
            w, h = crop_size(info)
            done.append(f"{w / args.resolution * 25.4:.0f} x {h / args.resolution * 25.4:.0f} mm")
        if info["skew"]:
            done.append(f"straightened by {abs(info['skew']):.1f}°")
        if info["rotate"]:
            done.append(f"turned {info['rotate']}°")
        if done and not info["blank"]:
            print(f"  page {n}: {', '.join(done)}", file=sys.stderr)

    def progress(event, pages):
        if event == "scanning":
            print("scanning...", file=sys.stderr)
        elif event == "page":
            print(f"  page {pages}", file=sys.stderr)
        elif event == "blank":
            print(f"  page {pages} is blank", file=sys.stderr)
            blank.append(pages)
        elif event == "all_blank":
            print("warning: all pages are blank; they were kept", file=sys.stderr)
            blank.clear()
        elif event == "ocr":
            print("recognizing text...", file=sys.stderr)

    pages, complete, ocr_error = scan_to_file(args, out, device=device, on_progress=progress,
                                              on_page_image=page_image, overwrite=args.force)
    removed = f" ({len(blank)} blank page(s) removed)" if blank else ""
    print(f"saved {pages} page(s) to {out}{removed}")
    if not complete:
        sys.exit(2)
    if ocr_error:
        print(f"warning: {ocr_error}; the PDF was saved without text", file=sys.stderr)
        sys.exit(3)


if __name__ == "__main__":
    try:
        main()
    except ScanError as e:
        print(f"error: {clean(e)}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)
