#!/usr/bin/env python3
"""Update the translation files gui/po/*.po from the app's source code.

    python3 gui/tools/update_po.py          # update all .po files
    python3 gui/tools/update_po.py fr       # also start po/fr.po

Collects the texts marked with _("..."), ngettext("...", "...", n) and
pgettext("context", "...") (for a word used in two meanings) in the
app's modules, keeps existing translations, adds new texts with an empty
translation and drops texts that are gone. Then lists what is untranslated.
No gettext tools needed.
"""

import ast
import os
import sys

GUI = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, GUI)
import i18n  # noqa: E402

SOURCES = ["wsdscan_gui.py", "scanform.py", "tray.py"]
PLURAL_FORMS = {"de": "nplurals=2; plural=(n != 1);", "fr": "nplurals=2; plural=(n > 1);"}


def messages(sources=SOURCES, gui=GUI):
    """{(msgctxt, msgid, msgid_plural): ["file:line", ...]} in source order
    (msgctxt and msgid_plural are None if not used)."""
    found = {}
    for name in sources:
        path = os.path.join(gui, name)
        with open(path, encoding="utf-8") as f:
            tree = ast.parse(f.read(), path)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            fname = func.id if isinstance(func, ast.Name) else (
                func.attr if isinstance(func, ast.Attribute) else None)
            args = node.args
            if fname == "_" and len(args) == 1:
                strings, shape = args[:1], "s"
            elif fname == "ngettext" and len(args) == 3:
                strings, shape = args[:2], "sp"
            elif fname == "pgettext" and len(args) == 2:
                strings, shape = args[:2], "cs"
            else:
                continue
            if not all(isinstance(a, ast.Constant) and isinstance(a.value, str) for a in strings):
                continue  # e.g. _(scanform.APP_NAME): translated where it is defined
            values = dict(zip(shape, (a.value for a in strings)))
            key = (values.get("c"), values["s"], values.get("p"))
            found.setdefault(key, []).append(f"{name}:{node.lineno}")
    return dict(sorted(found.items(), key=lambda item: _location_key(item[1][0])))


def _location_key(location):
    name, line = location.rsplit(":", 1)
    return SOURCES.index(name) if name in SOURCES else len(SOURCES), int(line)


def quote(text):
    escaped = (text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
               .replace("\t", "\\t"))
    return f'"{escaped}"'


def render(language, header, entries):
    lines = [f"# Scan to PDF: translation ({language}).", "#", 'msgid ""', 'msgstr ""']
    lines += [quote(f"{line}\n") for line in header.splitlines() if line]
    for (context, msgid, plural), (locations, msgstr, fuzzy) in entries.items():
        lines.append("")
        lines.append("#: " + " ".join(locations))
        if fuzzy:
            lines.append("#, fuzzy")
        if context is not None:
            lines.append(f"msgctxt {quote(context)}")
        lines.append(f"msgid {quote(msgid)}")
        if plural is None:
            lines.append(f"msgstr {quote(msgstr[0] if msgstr else '')}")
        else:
            lines.append(f"msgid_plural {quote(plural)}")
            forms = msgstr if len(msgstr) >= 2 else ["", ""]
            lines += [f"msgstr[{i}] {quote(form)}" for i, form in enumerate(forms)]
    return "\n".join(lines) + "\n"


def update(language, po_dir=i18n.PO_DIR):
    path = os.path.join(po_dir, f"{language}.po")
    old = {}
    header = (f"Language: {language}\nMIME-Version: 1.0\n"
              "Content-Type: text/plain; charset=UTF-8\nContent-Transfer-Encoding: 8bit\n"
              f"Plural-Forms: {PLURAL_FORMS.get(language, 'nplurals=2; plural=(n != 1);')}\n")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for e in i18n.parse_po(f.read(), path):
                if e["msgid"] == "":
                    header = e["msgstr"][0]
                else:
                    old[(e["msgctxt"], e["msgid"], e["msgid_plural"])] = (e["msgstr"], e["fuzzy"])
    entries = {}
    for key, locations in messages().items():
        msgstr, fuzzy = old.get(key, ([], False))
        entries[key] = (locations, msgstr, fuzzy)
    os.makedirs(po_dir, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(render(language, header, entries))
    missing = [key[1] for key, (_loc, msgstr, fuzzy) in entries.items()
               if fuzzy or not msgstr or not all(msgstr)]
    gone = len(set(old) - set(entries))
    print(f"{path}: {len(entries)} texts, {len(missing)} untranslated, {gone} removed")
    for msgid in missing:
        print(f"  untranslated: {msgid!r}")


def main():
    languages = {name[:-3] for name in os.listdir(i18n.PO_DIR) if name.endswith(".po")} \
        if os.path.isdir(i18n.PO_DIR) else set()
    for language in sorted(languages | set(sys.argv[1:])):
        update(language)


if __name__ == "__main__":
    main()
