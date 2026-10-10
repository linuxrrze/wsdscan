"""In-process fake WSD scanner for tests.

Answers WS-Discovery probes over UDP and WS-Scan SOAP requests over HTTP, both
on ephemeral localhost ports. Its capabilities and failure behavior are
configurable so tests can model specific devices, e.g. the real ES-580W.
"""

import base64
import math
import re
import socket
import struct
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from xml.sax.saxutils import escape

# 24x16 JPEGs: baseline JFIF RGB, grayscale, and RGB with an EXIF APP1 segment.
JPEG_RGB = base64.b64decode(
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDABALDA4MChAODQ4SERATGCgaGBYWGDEjJR0oOjM9PDkzODdASFxOQERXRTc4UG1RV19iZ2hnPk1xeXBkeFxlZ2P/2wBDARESEhgVGC8aGi9jQjhCY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2P/wAARCAAQABgDASIAAhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEAAwEBAQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREAAgECBAQDBAcFBAQAAQJ3AAECAxEEBSExBhJBUQdhcRMiMoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElKU1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPwDkqKKK5T3gooooA//Z")
JPEG_GRAY = base64.b64decode(
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDABALDA4MChAODQ4SERATGCgaGBYWGDEjJR0oOjM9PDkzODdASFxOQERXRTc4UG1RV19iZ2hnPk1xeXBkeFxlZ2P/wAALCAAQABgBAREA/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/9oACAEBAAA/ACiiiiiv/9k=")
JPEG_EXIF = base64.b64decode(
    "/9j/4AAQSkZJRgABAQAAAQABAAD/4QA8RXhpZgAATU0AKgAAAAgAAgEPAAIAAAAGAAAAJgEQAAIAAAAIAAAALAAAAABFUFNPTgBFUy01ODBXAP/bAEMAEAsMDgwKEA4NDhIREBMYKBoYFhYYMSMlHSg6Mz08OTM4N0BIXE5ARFdFNzhQbVFXX2JnaGc+TXF5cGR4XGVnY//bAEMBERISGBUYLxoaL2NCOEJjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY//AABEIABAAGAMBIgACEQEDEQH/xAAfAAABBQEBAQEBAQAAAAAAAAAAAQIDBAUGBwgJCgv/xAC1EAACAQMDAgQDBQUEBAAAAX0BAgMABBEFEiExQQYTUWEHInEUMoGRoQgjQrHBFVLR8CQzYnKCCQoWFxgZGiUmJygpKjQ1Njc4OTpDREVGR0hJSlNUVVZXWFlaY2RlZmdoaWpzdHV2d3h5eoOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3uLm6wsPExcbHyMnK0tPU1dbX2Nna4eLj5OXm5+jp6vHy8/T19vf4+fr/xAAfAQADAQEBAQEBAQEBAAAAAAAAAQIDBAUGBwgJCgv/xAC1EQACAQIEBAMEBwUEBAABAncAAQIDEQQFITEGEkFRB2FxEyIygQgUQpGhscEJIzNS8BVictEKFiQ04SXxFxgZGiYnKCkqNTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqCg4SFhoeIiYqSk5SVlpeYmZqio6Slpqeoqaqys7S1tre4ubrCw8TFxsfIycrS09TV1tfY2dri4+Tl5ufo6ery8/T19vf4+fr/2gAMAwEAAhEDEQA/AOSooorlPeCiiigD/9k=")
JPEG_SIZE = (24, 16)

REVERSE_BITS = bytes(int(f"{i:08b}"[::-1], 2) for i in range(256))
TIFF_LAYOUT = {"BlackAndWhite1": (1, 1), "Grayscale8": (1, 8), "RGB24": (3, 8)}


def tiff_pixels(color, width=JPEG_SIZE[0], height=JPEG_SIZE[1]):
    """Deterministic test pattern as packed rows (what the PDF should contain)."""
    samples, bits = TIFF_LAYOUT[color]
    row = (width * samples * bits + 7) // 8
    return bytes((i * 31 + j * 7) & 0xFF for i in range(height) for j in range(row))


def make_tiff(color, width=JPEG_SIZE[0], height=JPEG_SIZE[1], order="<",
              photometric_bw=0, fill_order=1, rows_per_strip=5, pixels=None):
    """Uncompressed single-page TIFF as a scanner sends it. `pixels`: packed
    rows instead of the test pattern."""
    samples, bits = TIFF_LAYOUT[color]
    photometric = {"BlackAndWhite1": photometric_bw, "Grayscale8": 1, "RGB24": 2}[color]
    row = (width * samples * bits + 7) // 8
    if pixels is None:
        pixels = tiff_pixels(color, width, height)
    if fill_order == 2:
        pixels = pixels.translate(REVERSE_BITS)
    strips = [pixels[y * row:(y + rows_per_strip) * row] for y in range(0, height, rows_per_strip)]

    out = bytearray(b"II*\x00" if order == "<" else b"MM\x00*")
    out += b"\0\0\0\0"  # IFD offset, patched below
    offsets = []
    for strip in strips:
        offsets.append(len(out))
        out += strip

    def array(fmt, values):
        """Write values out of line if they don't fit in 4 bytes."""
        data = struct.pack(order + fmt * len(values), *values)
        if len(data) <= 4:
            return data.ljust(4, b"\0")
        pos = len(out)
        out.extend(data)
        return struct.pack(order + "I", pos)

    entries = [  # (tag, type, values); type 3 = SHORT, 4 = LONG
        (256, 4, [width]), (257, 4, [height]), (258, 3, [bits] * samples),
        (259, 3, [1]), (262, 3, [photometric]), (266, 3, [fill_order]),
        (273, 4, offsets), (277, 3, [samples]), (278, 4, [rows_per_strip]),
        (279, 4, [len(s) for s in strips]), (284, 3, [1]),
    ]
    packed = [(tag, typ, len(vals), array("H" if typ == 3 else "I", vals))
              for tag, typ, vals in entries]
    if len(out) % 2:
        out += b"\0"
    struct.pack_into(order + "I", out, 4, len(out))
    out += struct.pack(order + "H", len(packed))
    for tag, typ, count, value in packed:
        out += struct.pack(order + "HHI", tag, typ, count) + value
    out += b"\0\0\0\0"
    return bytes(out)



# --- test pages and a minimal JPEG encoder -----------------------------------
# For blank page detection: no JPEG encoder in the standard library, so this
# writes baseline JPEGs itself (uniform quantizer, one fixed-length Huffman
# table per class). Decoders such as libjpeg read them like any other JPEG.

ZIGZAG = sorted(range(64), key=lambda i: (i // 8 + i % 8,
                                          i // 8 if (i // 8 + i % 8) % 2 else i % 8))
JPEG_QUANT = 16
COS = [[(0.5 ** 0.5 if u == 0 else 1) * math.cos((2 * x + 1) * u * math.pi / 16) / 2
        for x in range(8)] for u in range(8)]


def test_page(width, height, paper=235, marks=(), noise=3):
    """Gray page as rows: paper with block-wise noise, plus (x, y, w, h, value)
    rectangles in pixels. Deterministic."""
    rows = []
    for y in range(height):
        rows.append(bytearray(
            max(0, min(255, paper + ((x // 8 * 7919 + y // 8 * 104729) % (2 * noise + 1)) - noise))
            for x in range(width)))
    for x0, y0, w, h, value in marks:
        for y in range(max(0, y0), min(height, y0 + h)):
            rows[y][max(0, x0):min(width, x0 + w)] = bytes([value]) * (min(width, x0 + w) - max(0, x0))
    return rows


def scanned_sheet(width, height, sheet, backing=180, paper=235, pad_from=None, marks=(),
                  noise=3):
    """Gray scan of a sheet on the feeder's backing, as rows. sheet: (cx, cy,
    w, h, angle): the sheet's center, size in pixels and clockwise tilt in
    degrees. marks: (x, y, w, h, value) rectangles on the sheet, in its own
    upright coordinates. Rows from pad_from on are white padding (as the
    ES-580W sends after the sheet's end)."""
    cx, cy, sw, sh, angle = sheet
    cos, sin = math.cos(math.radians(angle)), math.sin(math.radians(angle))
    rows = []
    for y in range(height):
        if pad_from is not None and y >= pad_from:
            rows.append(bytearray([255]) * width)
            continue
        row = bytearray(width)
        for x in range(width):
            dx, dy = x + 0.5 - cx, y + 0.5 - cy
            u, v = dx * cos + dy * sin + sw / 2, -dx * sin + dy * cos + sh / 2
            jitter = (x // 8 * 7919 + y // 8 * 104729) % (2 * noise + 1) - noise
            if 0 <= u < sw and 0 <= v < sh:
                value = paper
                for mx, my, mw, mh, mvalue in marks:
                    if mx <= u < mx + mw and my <= v < my + mh:
                        value = mvalue
                        break
            else:
                value = backing
            row[x] = max(0, min(255, value + jitter))
        rows.append(row)
    return rows


def with_comment(jpeg, text):
    """The JPEG with a comment segment, e.g. b"ROTATE=90" for the fake Tesseract's
    orientation detection."""
    return jpeg[:2] + b"\xff\xfe" + (len(text) + 2).to_bytes(2, "big") + text + jpeg[2:]


def _fdct(block):
    """Quantized coefficients of an 8x8 block (64 values, row by row)."""
    if min(block) == max(block):  # flat: DC only
        coeffs = [0] * 64
        coeffs[0] = round((block[0] - 128) * 8 / JPEG_QUANT)
        return coeffs
    rows = [[sum(COS[u][x] * (block[y * 8 + x] - 128) for x in range(8)) for u in range(8)]
            for y in range(8)]
    return [round(sum(COS[v][y] * rows[y][u] for y in range(8)) / JPEG_QUANT)
            for v in range(8) for u in range(8)]


class _Bits:
    def __init__(self):
        self.out, self.acc, self.n = bytearray(), 0, 0

    def put(self, value, length):
        self.acc = (self.acc << length) | (value & ((1 << length) - 1))
        self.n += length
        while self.n >= 8:
            self.n -= 8
            byte = (self.acc >> self.n) & 0xFF
            self.out.append(byte)
            if byte == 0xFF:
                self.out.append(0)
        self.acc &= (1 << self.n) - 1

    def flush(self):
        if self.n:
            self.put(0x7F, 8 - self.n)  # pad with 1 bits


def _magnitude(value):
    size = abs(value).bit_length()
    return size, value if value >= 0 else value + (1 << size) - 1


def make_jpeg(rows, color=False, restart=0, tint=(128, 128)):
    """Baseline JPEG of gray rows: 1 component, or YCbCr 4:2:0 with constant
    chroma `tint` (color). `restart`: restart interval in MCUs."""
    height, width = len(rows), len(rows[0])
    ac_symbols = [0x00, 0xF0] + [r << 4 | size for r in range(16) for size in range(1, 11)]
    ac_code = {sym: i for i, sym in enumerate(ac_symbols)}  # all 8 bits long
    bits = _Bits()

    def block(x0, y0):  # edges repeat the last row/column
        return [rows[min(height - 1, y0 + y)][min(width - 1, x0 + x)]
                for y in range(8) for x in range(8)]

    def encode(coeffs, comp, preds):
        diff = coeffs[0] - preds[comp]
        preds[comp] = coeffs[0]
        size, value = _magnitude(diff)
        bits.put(size, 4)  # DC codes: 4 bits each
        if size:
            bits.put(value, size)
        run = 0
        for k in ZIGZAG[1:]:
            c = coeffs[k]
            if c == 0:
                run += 1
                continue
            while run > 15:
                bits.put(ac_code[0xF0], 8)
                run -= 16
            size, value = _magnitude(c)
            bits.put(ac_code[run << 4 | size], 8)
            bits.put(value, size)
            run = 0
        if run:
            bits.put(ac_code[0x00], 8)

    if color:
        mcus = [(mx * 16, my * 16) for my in range(-(-height // 16)) for mx in range(-(-width // 16))]
    else:
        mcus = [(mx * 8, my * 8) for my in range(-(-height // 8)) for mx in range(-(-width // 8))]
    preds = [0, 0, 0]
    data = bytearray()
    for n, (x0, y0) in enumerate(mcus):
        if restart and n and n % restart == 0:
            bits.flush()
            data += bits.out + bytes([0xFF, 0xD0 + (n // restart - 1) % 8])
            bits.out = bytearray()
            preds = [0, 0, 0]
        if color:
            for dy in (0, 8):
                for dx in (0, 8):
                    encode(_fdct(block(x0 + dx, y0 + dy)), 0, preds)
            for comp, value in ((1, tint[0]), (2, tint[1])):
                encode(_fdct([value] * 64), comp, preds)
        else:
            encode(_fdct(block(x0, y0)), 0, preds)
    bits.flush()
    data += bits.out

    def segment(marker, payload):
        return bytes([0xFF, marker]) + (len(payload) + 2).to_bytes(2, "big") + payload

    comps = [(1, 0x22 if color else 0x11)] + ([(2, 0x11), (3, 0x11)] if color else [])
    out = bytearray(b"\xff\xd8")
    out += segment(0xDB, bytes([0]) + bytes([JPEG_QUANT]) * 64)
    out += segment(0xC0, bytes([8]) + height.to_bytes(2, "big") + width.to_bytes(2, "big")
                   + bytes([len(comps)]) + b"".join(bytes([c, hv, 0]) for c, hv in comps))
    out += segment(0xC4, bytes([0x00]) + bytes([0, 0, 0, 12] + [0] * 12) + bytes(range(12)))
    out += segment(0xC4, bytes([0x10]) + bytes([0] * 7 + [len(ac_symbols)] + [0] * 8)
                   + bytes(ac_symbols))
    if restart:
        out += segment(0xDD, restart.to_bytes(2, "big"))
    out += segment(0xDA, bytes([len(comps)]) + b"".join(bytes([c, 0]) for c, _hv in comps)
                   + bytes([0, 63, 0]))
    return bytes(out + data + b"\xff\xd9")


def pack_bw(rows, threshold=128):
    """1-bit rows (WhiteIsZero: 1 = black), as BlackAndWhite1 TIFF pixels."""
    out = bytearray()
    for row in rows:
        for x in range(0, len(row), 8):
            byte = 0
            for bit, value in enumerate(row[x:x + 8]):
                if value < threshold:
                    byte |= 0x80 >> bit
            out.append(byte)
    return bytes(out)

NSDECL = (
    'xmlns:soap="http://www.w3.org/2003/05/soap-envelope" '
    'xmlns:wsa="http://schemas.xmlsoap.org/ws/2004/08/addressing" '
    'xmlns:wsd="http://schemas.xmlsoap.org/ws/2005/04/discovery" '
    'xmlns:wsdp="http://schemas.xmlsoap.org/ws/2006/02/devprof" '
    'xmlns:wscn="http://schemas.microsoft.com/windows/2006/08/wdp/scan" '
    'xmlns:mex="http://schemas.xmlsoap.org/ws/2004/09/mex" '
    'xmlns:xop="http://www.w3.org/2004/08/xop/include"'
)
COLOR_ENTRIES = {"RGB24", "Grayscale8"}

# Optional DeviceSettings children as a feature-rich scanner reports them,
# including one vendor element the tool does not know.
FULL_DEVICE_SETTINGS = (
    "<wscn:CompressionQualityFactorSupported><wscn:MinValue>0</wscn:MinValue>"
    "<wscn:MaxValue>100</wscn:MaxValue></wscn:CompressionQualityFactorSupported>"
    "<wscn:ContentTypesSupported><wscn:ContentTypeValue>Auto</wscn:ContentTypeValue>"
    "<wscn:ContentTypeValue>Text</wscn:ContentTypeValue>"
    "<wscn:ContentTypeValue>Photo</wscn:ContentTypeValue></wscn:ContentTypesSupported>"
    "<wscn:DocumentSizeAutoDetectSupported>true</wscn:DocumentSizeAutoDetectSupported>"
    "<wscn:AutoExposureSupported>false</wscn:AutoExposureSupported>"
    "<wscn:BrightnessSupported>true</wscn:BrightnessSupported>"
    "<wscn:ContrastSupported>true</wscn:ContrastSupported>"
    "<wscn:ScalingRangeSupported>"
    "<wscn:ScalingWidth><wscn:MinValue>1</wscn:MinValue><wscn:MaxValue>1000</wscn:MaxValue></wscn:ScalingWidth>"
    "<wscn:ScalingHeight><wscn:MinValue>1</wscn:MinValue><wscn:MaxValue>1000</wscn:MaxValue></wscn:ScalingHeight>"
    "</wscn:ScalingRangeSupported>"
    "<wscn:RotationsSupported><wscn:RotationValue>0</wscn:RotationValue>"
    "<wscn:RotationValue>180</wscn:RotationValue></wscn:RotationsSupported>"
    '<epson:BlankPageSkip xmlns:epson="urn:example:epson"><epson:Level>1</epson:Level>'
    "<epson:Level>2</epson:Level></epson:BlankPageSkip>"
)


def envelope(body):
    return (f'<?xml version="1.0" encoding="utf-8"?><soap:Envelope {NSDECL}>'
            f"<soap:Header/><soap:Body>{body}</soap:Body></soap:Envelope>").encode()


def tag(xml, name):
    m = re.search(rf"<wscn:{name}>([^<]*)</wscn:{name}>", xml)
    return m.group(1) if m else None


class FakeScanner:
    """Configurable fake. Pass keyword arguments to override the defaults below.

    behavior: "ok", "busy" (CreateScanJob refused `busy_count` times first),
              or "jam" (job fails after `jam_after` images).
    sheets:   sheets in the feeder; each yields 1 image (ADF) or 2 (duplex).
    """

    def __init__(self, **config):
        self.config = {
            "manufacturer": "EPSON",
            "model": "ES-580W Series",
            "firmware": "1.00",
            "serial": "X123456",
            "service_path": "/WSD/SCANNER",
            "formats": ["jfif", "pdf-a"],
            "colors": ["BlackAndWhite1", "Grayscale8", "RGB24"],
            "resolutions": [100, 150, 200, 300, 600],
            "duplex": True,
            "platen": False,
            "min_size": (2000, 2000),
            "max_size": (8500, 14000),
            "state": "Idle",
            "device_settings": FULL_DEVICE_SETTINGS,  # extra DeviceSettings children
            "optical": (600, 600),        # ADFOpticalResolution, None = omit
            "description": None,          # {"name", "info", "location"}
            "conditions": [],             # [(name, component, severity)]
            "sheets": 2,
            "behavior": "ok",
            "busy_count": 2,
            "jam_after": 2,
            "validate": True,             # implements ValidateScanTicket
            "reject": set(),              # {(source, color, dpi)} the scanner refuses
            "final_dpi": None,            # report a different dpi than requested
            "exposure_range": (-1000, 1000),  # accepted brightness/contrast; None = none
            "tiff": {},                   # make_tiff options, e.g. {"order": ">"}
            # Misbehavior, for security tests:
            "xaddrs": None,               # device URLs announced in discovery
            "hosted_address": None,       # scan service URL announced by Get
            "redirect_to": None,          # answer every SOAP request with a 302 there
            "endless": False,             # never run out of pages
            "image": None,                # bytes returned by RetrieveImage instead
            "images": None,               # list of page images, used in turn
            "doctype": False,             # put a DTD into the Get response
            "udp_port": 0,                # discovery port (0 = any free one)
            "end_with_empty": False,      # end the job with an empty image (ES-580W, long sizes)
        }
        unknown = set(config) - set(self.config)
        if unknown:
            raise TypeError(f"unknown FakeScanner options: {unknown}")
        self.config.update(config)
        self.endpoint = f"urn:uuid:{uuid.uuid4()}"
        self.requests = []   # (action, request_xml)
        self.tickets = []    # CreateScanJob request bodies
        self._sent_empty = False
        self._images_left = 0
        self._images_sent = 0
        self._busy_left = self.config["busy_count"]
        self._job_format = None
        self._job_color = None

    # --- lifecycle -----------------------------------------------------------

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.stop()

    def start(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                fake._handle(self)

        self.http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.http_port = self.http.server_address[1]
        self.udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.udp.bind(("127.0.0.1", self.config["udp_port"]))
        self.udp_port = self.udp.getsockname()[1]
        self.device_url = f"http://127.0.0.1:{self.http_port}/WSD/DEVICE"
        self.service_url = f"http://127.0.0.1:{self.http_port}{self.config['service_path']}"
        self._threads = [threading.Thread(target=self.http.serve_forever, daemon=True),
                         threading.Thread(target=self._serve_udp, daemon=True)]
        for t in self._threads:
            t.start()

    def stop(self):
        self.http.shutdown()
        self.http.server_close()
        self.udp.close()

    @property
    def host(self):
        """Value for the tool's --host option (IP:discovery-port)."""
        return f"127.0.0.1:{self.udp_port}"

    def actions(self):
        return [a for a, _ in self.requests]

    # --- discovery -----------------------------------------------------------

    def _serve_udp(self):
        while True:
            try:
                data, addr = self.udp.recvfrom(65535)
            except OSError:
                return
            if b"Probe" not in data:
                continue
            self.requests.append(("Probe", data.decode(errors="replace")))
            reply = envelope(
                "<wsd:ProbeMatches><wsd:ProbeMatch>"
                f"<wsa:EndpointReference><wsa:Address>{self.endpoint}</wsa:Address>"
                "</wsa:EndpointReference>"
                "<wsd:Types>wsdp:Device wscn:ScanDeviceType</wsd:Types>"
                f"<wsd:XAddrs>{self.config['xaddrs'] or self.device_url}</wsd:XAddrs>"
                "</wsd:ProbeMatch></wsd:ProbeMatches>")
            try:
                self.udp.sendto(reply, addr)
            except OSError:
                return  # stopped while answering

    # --- SOAP ----------------------------------------------------------------

    def _handle(self, h):
        req = h.rfile.read(int(h.headers["Content-Length"])).decode()
        action = re.search(r"<wsa:Action>([^<]+)", req).group(1).rsplit("/", 1)[-1]
        to = re.search(r"<wsa:To>([^<]+)", req).group(1)
        self.requests.append((action, req))
        if self.config["redirect_to"]:
            h.send_response(302)
            h.send_header("Location", self.config["redirect_to"])
            h.send_header("Content-Length", "0")
            h.end_headers()
            return
        expected_to = self.endpoint if action == "Get" else self.service_url
        if to != expected_to:
            return self._fault(h, "ClientErrorInvalidTo")
        handler = getattr(self, f"_op_{action}", None)
        if handler is None:
            return self._fault(h, "ActionNotSupported", code=500)
        handler(h, req)

    def _send(self, h, body, ctype="application/soap+xml; charset=utf-8", code=200):
        h.send_response(code)
        h.send_header("Content-Type", ctype)
        h.send_header("Content-Length", str(len(body)))
        h.end_headers()
        h.wfile.write(body)

    def _fault(self, h, subcode, code=400):
        self._send(h, envelope(
            "<soap:Fault><soap:Code><soap:Value>soap:Sender</soap:Value>"
            f"<soap:Subcode><soap:Value>wscn:{subcode}</soap:Value></soap:Subcode>"
            "</soap:Code><soap:Reason>"
            f'<soap:Text xml:lang="en">{subcode}</soap:Text></soap:Reason></soap:Fault>'),
            code=code)

    def _op_Get(self, h, req):
        c = self.config
        body = envelope(
            "<mex:Metadata>"
            '<mex:MetadataSection Dialect="http://schemas.xmlsoap.org/ws/2006/02/devprof/ThisModel">'
            f"<wsdp:ThisModel><wsdp:Manufacturer>{c['manufacturer']}</wsdp:Manufacturer>"
            f"<wsdp:ModelName>{c['model']}</wsdp:ModelName></wsdp:ThisModel></mex:MetadataSection>"
            '<mex:MetadataSection Dialect="http://schemas.xmlsoap.org/ws/2006/02/devprof/ThisDevice">'
            f"<wsdp:ThisDevice><wsdp:FirmwareVersion>{c['firmware']}</wsdp:FirmwareVersion>"
            f"<wsdp:SerialNumber>{c['serial']}</wsdp:SerialNumber></wsdp:ThisDevice>"
            "</mex:MetadataSection>"
            '<mex:MetadataSection Dialect="http://schemas.xmlsoap.org/ws/2006/02/devprof/Relationship">'
            "<wsdp:Relationship><wsdp:Hosted><wsa:EndpointReference>"
            f"<wsa:Address>{c['hosted_address'] or self.service_url}</wsa:Address>"
            "</wsa:EndpointReference>"
            "<wsdp:Types>wscn:ScannerServiceType</wsdp:Types></wsdp:Hosted>"
            "</wsdp:Relationship></mex:MetadataSection></mex:Metadata>")
        if c["doctype"]:
            body = body.replace(b"<soap:Envelope", b'<!DOCTYPE x [<!ENTITY a "a">]><soap:Envelope', 1)
        self._send(h, body)

    def _op_GetScannerElements(self, h, req):
        c = self.config

        def size(name, s):
            return (f"<wscn:{name}><wscn:Width>{s[0]}</wscn:Width>"
                    f"<wscn:Height>{s[1]}</wscn:Height></wscn:{name}>")

        def side(name):
            widths = "".join(f"<wscn:Width>{r}</wscn:Width>" for r in c["resolutions"])
            heights = "".join(f"<wscn:Height>{r}</wscn:Height>" for r in c["resolutions"])
            colors = "".join(f"<wscn:ColorEntry>{e}</wscn:ColorEntry>" for e in c["colors"])
            optical = size("ADFOpticalResolution", c["optical"]) if c["optical"] else ""
            return (f"<wscn:{name}>{optical}{size('ADFMinimumSize', c['min_size'])}"
                    f"{size('ADFMaximumSize', c['max_size'])}"
                    f"<wscn:ADFResolutions><wscn:Widths>{widths}</wscn:Widths>"
                    f"<wscn:Heights>{heights}</wscn:Heights></wscn:ADFResolutions>"
                    f"<wscn:ADFColor>{colors}</wscn:ADFColor></wscn:{name}>")

        formats = "".join(f"<wscn:FormatValue>{f}</wscn:FormatValue>" for f in c["formats"])
        description = ""
        if c["description"]:
            description = (
                '<wscn:ElementData Name="wscn:ScannerDescription" Valid="true">'
                "<wscn:ScannerDescription>" + "".join(
                    f"<wscn:Scanner{k.title()}>{v}</wscn:Scanner{k.title()}>"
                    for k, v in c["description"].items())
                + "</wscn:ScannerDescription></wscn:ElementData>")
        conditions = "".join(
            f"<wscn:DeviceCondition><wscn:Time>2026-10-02T10:00:00Z</wscn:Time>"
            f"<wscn:Name>{n}</wscn:Name><wscn:Component>{comp}</wscn:Component>"
            f"<wscn:Severity>{sev}</wscn:Severity></wscn:DeviceCondition>"
            for n, comp, sev in c["conditions"])
        adf = (f"<wscn:ADF><wscn:ADFSupportsDuplex>{'true' if c['duplex'] else 'false'}"
               f"</wscn:ADFSupportsDuplex>{side('ADFFront')}"
               f"{side('ADFBack') if c['duplex'] else ''}</wscn:ADF>")
        platen = "<wscn:Platen><wscn:PlatenColor/></wscn:Platen>" if c["platen"] else ""
        self._send(h, envelope(
            "<wscn:GetScannerElementsResponse><wscn:ScannerElements>"
            '<wscn:ElementData Name="wscn:ScannerConfiguration" Valid="true">'
            "<wscn:ScannerConfiguration><wscn:DeviceSettings>"
            f"<wscn:FormatsSupported>{formats}</wscn:FormatsSupported>"
            f"{c['device_settings']}</wscn:DeviceSettings>"
            f"{platen}{adf}</wscn:ScannerConfiguration></wscn:ElementData>"
            '<wscn:ElementData Name="wscn:ScannerStatus" Valid="true"><wscn:ScannerStatus>'
            f"<wscn:ScannerState>{c['state']}</wscn:ScannerState>"
            f"<wscn:ActiveConditions>{conditions}</wscn:ActiveConditions></wscn:ScannerStatus>"
            f"</wscn:ElementData>{description}</wscn:ScannerElements></wscn:GetScannerElementsResponse>"))

    def _ticket_problem(self, req):
        """Return a fault name if the real device would refuse this ticket."""
        c = self.config
        fmt, color = tag(req, "Format"), tag(req, "ColorProcessing")
        source = tag(req, "InputSource")
        dpi = re.search(r"<wscn:Resolution><wscn:Width>(\d+)", req)
        dpi = int(dpi.group(1)) if dpi else None
        if fmt not in c["formats"]:
            return "ClientErrorFormatNotSupported"
        if color not in c["colors"]:
            return "ClientErrorColorProcessingNotSupported"
        if dpi not in c["resolutions"]:
            return "ClientErrorResolutionNotSupported"
        if source == "ADFDuplex" and not c["duplex"]:
            return "ClientErrorInputSourceNotSupported"
        source_name = "duplex" if source == "ADFDuplex" else "adf"
        if (source_name, color, dpi) in c["reject"]:
            return "ClientErrorInvalidScanTicket"
        for name in ("Brightness", "Contrast"):
            value = tag(req, name)
            if value is not None:
                rng = c["exposure_range"]
                if rng is None or not rng[0] <= int(value) <= rng[1]:
                    return "ClientErrorInvalidScanTicket"
        return None

    def _op_ValidateScanTicket(self, h, req):
        if not self.config["validate"]:
            return self._fault(h, "ActionNotSupported", code=500)
        valid = self._ticket_problem(req) is None
        self._send(h, envelope(
            "<wscn:ValidateScanTicketResponse><wscn:ValidationInfo>"
            f"<wscn:ValidTicket>{'true' if valid else 'false'}</wscn:ValidTicket>"
            "</wscn:ValidationInfo></wscn:ValidateScanTicketResponse>"))

    def _op_CreateScanJob(self, h, req):
        c = self.config
        self.tickets.append(req)
        if c["behavior"] == "busy" and self._busy_left > 0:
            self._busy_left -= 1
            return self._fault(h, "ServerErrorNotAcceptingJobs", code=503)
        problem = self._ticket_problem(req)
        if problem:
            return self._fault(h, problem)
        per_sheet = 2 if tag(req, "InputSource") == "ADFDuplex" else 1
        self._images_left = c["sheets"] * per_sheet
        self._images_sent = 0
        self._job_format = tag(req, "Format")
        self._job_color = tag(req, "ColorProcessing")
        dpi = c["final_dpi"] or int(re.search(r"<wscn:Resolution><wscn:Width>(\d+)", req).group(1))
        self._send(h, envelope(
            "<wscn:CreateScanJobResponse><wscn:JobId>7</wscn:JobId>"
            f"<wscn:JobToken>{escape('tok&1')}</wscn:JobToken>"
            "<wscn:DocumentFinalParameters><wscn:MediaSides><wscn:MediaFront>"
            f"<wscn:Resolution><wscn:Width>{dpi}</wscn:Width><wscn:Height>{dpi}</wscn:Height>"
            "</wscn:Resolution></wscn:MediaFront></wscn:MediaSides>"
            "</wscn:DocumentFinalParameters></wscn:CreateScanJobResponse>"))

    def _op_RetrieveImage(self, h, req):
        if "<wscn:JobId>7</wscn:JobId>" not in req or "tok&amp;1" not in req:
            return self._fault(h, "ClientErrorJobIdNotFound")
        if self.config["behavior"] == "jam" and self._images_sent >= self.config["jam_after"]:
            self._images_left = 0
            return self._fault(h, "ServerErrorJobFailed", code=500)
        empty = False
        if self._images_left == 0 and not self.config["endless"]:
            if not self.config["end_with_empty"] or self._sent_empty:
                return self._fault(h, "ClientErrorNoImagesAvailable")
            self._sent_empty = empty = True
        else:
            self._images_left = max(0, self._images_left - 1)
            self._images_sent += 1
        if empty:
            image = b""
        elif self.config["images"]:
            images = self.config["images"]
            image = images[(self._images_sent - 1) % len(images)]
        elif self.config["image"] is not None:
            image = self.config["image"]
        elif self._job_format == "tiff-single-uncompressed":
            image = make_tiff(self._job_color, **self.config["tiff"])
        elif self._job_format == "exif":
            image = JPEG_EXIF
        else:
            image = JPEG_GRAY if self._job_color == "Grayscale8" else JPEG_RGB
        boundary = "MIMEBoundary_" + uuid.uuid4().hex
        xml = envelope("<wscn:RetrieveImageResponse><wscn:ScanData>"
                       '<xop:Include href="cid:image"/></wscn:ScanData>'
                       "</wscn:RetrieveImageResponse>")
        body = (f"--{boundary}\r\n"
                'Content-Type: application/xop+xml; charset=UTF-8; type="application/soap+xml"\r\n'
                "Content-ID: <root>\r\n\r\n").encode() + xml + (
                f"\r\n--{boundary}\r\nContent-Type: application/binary\r\n"
                "Content-Transfer-Encoding: binary\r\nContent-ID: <image>\r\n\r\n"
                ).encode() + image + f"\r\n--{boundary}--\r\n".encode()
        self._send(h, body, f'multipart/related; type="application/xop+xml"; '
                            f'boundary={boundary}; start="<root>"')

    def _op_CancelJob(self, h, req):
        self._send(h, envelope("<wscn:CancelJobResponse/>"))
