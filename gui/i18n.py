"""Translations of the app's texts, from po/<language>.po next to this file.

The .po files are compiled in memory when the app starts, so neither the
gettext tools nor a build step are needed. Languages are picked like GNU
gettext does: LANGUAGE (a colon-separated list), else LC_ALL, LC_MESSAGES
or LANG. Untranslated or fuzzy entries fall back to English.
"""

import ast
import gettext
import io
import os
import struct
import sys

PO_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "po")


class PoError(ValueError):
    pass


def _string(line, path, number):
    try:
        value = ast.literal_eval(line)
    except (ValueError, SyntaxError):
        value = None
    if not isinstance(value, str):
        raise PoError(f"{path}:{number}: expected a quoted string")
    return value


def parse_po(text, path="<po>"):
    """Entries of a .po file as dicts: msgctxt (or None), msgid, msgid_plural (or None),
    msgstr (a list: one item, or one per plural form), fuzzy, comments
    (the lines starting with "#", kept for rewriting the file)."""
    entries = []
    entry = None
    field = None

    def new_entry():
        return {"msgctxt": None, "msgid": None, "msgid_plural": None, "msgstr": [],
                "fuzzy": False, "comments": []}

    def finish():
        if entry is not None and entry["msgid"] is not None:
            entries.append(entry)

    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            if entry is not None and entry["msgid"] is not None:
                finish()
                entry = None
            entry = entry or new_entry()
            if line.startswith("#,") and "fuzzy" in (f.strip() for f in line[2:].split(",")):
                entry["fuzzy"] = True
            entry["comments"].append(raw)
            continue
        keyword, _sep, rest = line.partition(" ")
        if keyword.startswith('"'):
            if entry is None or field is None:
                raise PoError(f"{path}:{number}: string without a keyword")
            value = _string(line, path, number)
            if field == "msgstr":
                entry["msgstr"][-1] += value
            else:
                entry[field] += value
            continue
        value = _string(rest, path, number)
        if keyword == "msgctxt":
            if entry is not None and entry["msgid"] is not None:
                finish()
                entry = None
            entry = entry or new_entry()
            entry["msgctxt"], field = value, "msgctxt"
        elif keyword == "msgid":
            if entry is not None and entry["msgid"] is not None:
                finish()
                entry = None
            entry = entry or new_entry()
            entry["msgid"], field = value, "msgid"
        elif keyword == "msgid_plural" and entry is not None:
            entry["msgid_plural"], field = value, "msgid_plural"
        elif (keyword == "msgstr" or keyword.startswith("msgstr[")) and entry is not None:
            entry["msgstr"].append(value)
            field = "msgstr"
        else:
            raise PoError(f"{path}:{number}: unexpected {keyword!r}")
    finish()
    return entries


def compile_mo(entries):
    """GNU .mo bytes for the translated, non-fuzzy entries (and the header)."""
    catalog = {}
    header = next((e["msgstr"][0] for e in entries if e["msgid"] == "" and e["msgstr"]), "")
    if "charset=" not in header:  # .po files are UTF-8; gettext would assume ASCII
        header += "Content-Type: text/plain; charset=UTF-8\n"
    catalog[b""] = header.encode()
    for e in entries:
        if e["msgid"] == "":
            continue
        if (e["fuzzy"] and e["msgid"]) or not any(e["msgstr"]):
            continue
        key = e["msgid"] if e["msgid_plural"] is None else f"{e['msgid']}\0{e['msgid_plural']}"
        if e["msgctxt"] is not None:
            key = f"{e['msgctxt']}\x04{key}"  # as pgettext looks it up
        catalog[key.encode()] = "\0".join(e["msgstr"]).encode()
    keys = sorted(catalog)
    header = 7 * 4
    originals, translations = header, header + 8 * len(keys)
    offset = translations + 8 * len(keys)
    table_o, table_t, data = [], [], b""
    for key in keys:
        table_o += [len(key), offset + len(data)]
        data += key + b"\0"
    for key in keys:
        value = catalog[key]
        table_t += [len(value), offset + len(data)]
        data += value + b"\0"
    return (struct.pack("<7I", 0x950412DE, 0, len(keys), originals, translations, 0, 0)
            + struct.pack(f"<{len(table_o)}I", *table_o)
            + struct.pack(f"<{len(table_t)}I", *table_t) + data)


def wanted_languages(environ=None):
    """Language codes to try, most wanted first, e.g. ["de_DE", "de"]."""
    environ = os.environ if environ is None else environ
    for variable in ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG"):
        value = environ.get(variable)
        if value:
            break
    else:
        return []
    languages = []
    for item in value.split(":"):
        item = item.split(".")[0].split("@")[0]
        if item in ("C", "POSIX"):
            break  # English from here on
        for code in (item, item.split("_")[0]):
            if code and code not in languages:
                languages.append(code)
    return languages


def translation(po_dir=PO_DIR, environ=None):
    for language in wanted_languages(environ):
        if language == "en":
            break  # English is built in
        path = os.path.join(po_dir, f"{language}.po")
        if not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8") as f:
                mo = compile_mo(parse_po(f.read(), path))
            return gettext.GNUTranslations(io.BytesIO(mo))
        except (OSError, PoError, UnicodeDecodeError) as e:
            print(f"warning: ignoring the translation {path}: {e}", file=sys.stderr)
    return gettext.NullTranslations()
