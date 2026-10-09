"""Tests for the translations: the .po loader and the shipped catalogs."""

import contextlib
import io
import os
import re
import string
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
GUI = os.path.dirname(HERE)
sys.path[:0] = [GUI, os.path.join(GUI, "tools"), os.path.dirname(GUI)]

import i18n  # noqa: E402
import update_po  # noqa: E402

PO = '''# A comment
msgid ""
msgstr ""
"Language: de\\n"
"Plural-Forms: nplurals=2; plural=(n != 1);\\n"

#: x.py:1
msgid "Scan"
msgstr "Scannen"

msgid "Two "
"lines \\"quoted\\"\\n"
msgstr "Zwei "
"Zeilen „zitiert“\\n"

#, fuzzy
msgid "Unsure"
msgstr "Unsicher"

msgid "Missing"
msgstr ""

msgctxt "setting"
msgid "Color"
msgstr "Farbmodus"

msgid "Color"
msgstr "Farbe"

msgid "{n} page"
msgid_plural "{n} pages"
msgstr[0] "{n} Seite"
msgstr[1] "{n} Seiten"
'''


def load(po_text, language="de"):
    with tempfile.TemporaryDirectory() as d:
        with open(os.path.join(d, f"{language}.po"), "w", encoding="utf-8") as f:
            f.write(po_text)
        return i18n.translation(d, {"LANG": f"{language}_DE.UTF-8"})


def fields(text):
    return sorted(name for _lit, name, _spec, _conv in string.Formatter().parse(text) if name)


class LoaderTest(unittest.TestCase):
    def test_parse_and_compile(self):
        t = load(PO)
        self.assertEqual(t.gettext("Scan"), "Scannen")
        self.assertEqual(t.gettext('Two lines "quoted"\n'), "Zwei Zeilen „zitiert“\n")
        self.assertEqual(t.gettext("Unsure"), "Unsure", "fuzzy entries are not used")
        self.assertEqual(t.gettext("Missing"), "Missing")
        self.assertEqual(t.gettext("Unknown"), "Unknown")
        self.assertEqual(t.ngettext("{n} page", "{n} pages", 1), "{n} Seite")
        self.assertEqual(t.ngettext("{n} page", "{n} pages", 3), "{n} Seiten")
        self.assertEqual(t.ngettext("{n} file", "{n} files", 3), "{n} files")
        self.assertEqual(t.pgettext("setting", "Color"), "Farbmodus")
        self.assertEqual(t.gettext("Color"), "Farbe")

    def test_languages_from_environment(self):
        self.assertEqual(i18n.wanted_languages({"LANG": "de_DE.UTF-8"}), ["de_DE", "de"])
        self.assertEqual(i18n.wanted_languages({"LANGUAGE": "de_AT:en", "LANG": "fr_FR.UTF-8"}),
                         ["de_AT", "de", "en"])
        self.assertEqual(i18n.wanted_languages({"LC_ALL": "de_CH.UTF-8@euro", "LANG": "en_US"}),
                         ["de_CH", "de"])
        self.assertEqual(i18n.wanted_languages({"LANG": "C.UTF-8"}), [])
        self.assertEqual(i18n.wanted_languages({}), [])

    def test_english_first_or_unknown_language(self):
        self.assertEqual(load(PO).gettext("Scan"), "Scannen")
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "de.po"), "w", encoding="utf-8") as f:
                f.write(PO)
            english = i18n.translation(d, {"LANGUAGE": "en:de", "LANG": "de_DE.UTF-8"})
            self.assertEqual(english.gettext("Scan"), "Scan")
            fallback = i18n.translation(d, {"LANGUAGE": "fr:de", "LANG": "fr_FR.UTF-8"})
            self.assertEqual(fallback.gettext("Scan"), "Scannen", "next language in LANGUAGE")
            self.assertEqual(i18n.translation(d, {"LANG": "ja_JP.UTF-8"}).gettext("Scan"), "Scan")

    def test_broken_file_falls_back_to_english(self):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            t = load('msgid "Scan"\nmsgstr Scannen\n')
        self.assertIn("ignoring the translation", err.getvalue())
        self.assertEqual(t.gettext("Scan"), "Scan")


class CatalogTest(unittest.TestCase):
    """The shipped translations are complete and match the source code.
    After changing texts: python3 gui/tools/update_po.py, then translate."""

    def catalogs(self):
        names = sorted(n for n in os.listdir(i18n.PO_DIR) if n.endswith(".po"))
        self.assertIn("de.po", names)
        for name in names:
            path = os.path.join(i18n.PO_DIR, name)
            with open(path, encoding="utf-8") as f:
                yield name, i18n.parse_po(f.read(), path)

    def test_catalogs_complete_and_current(self):
        wanted = set(update_po.messages())
        for name, entries in self.catalogs():
            with self.subTest(catalog=name):
                found = {(e["msgctxt"], e["msgid"], e["msgid_plural"]): e
                         for e in entries if e["msgid"]}
                self.assertEqual(sorted(wanted - set(found), key=str), [],
                                 "missing: run gui/tools/update_po.py")
                self.assertEqual(sorted(set(found) - wanted, key=str), [], "no longer used")
                for (_context, msgid, _plural), e in found.items():
                    self.assertFalse(e["fuzzy"], msgid)
                    self.assertTrue(e["msgstr"] and all(e["msgstr"]), f"untranslated: {msgid!r}")
                    for form in e["msgstr"]:
                        self.assertEqual(fields(form), fields(msgid), f"placeholders of {msgid!r}")
                    self.assertEqual(msgid.endswith("…"), all(f.endswith("…") for f in e["msgstr"]),
                                     msgid)

    def test_app_texts_in_german(self):
        code = ("import scanform; print(scanform.SOURCES[0][1]); "
                "print(scanform.pages_summary(3)); print(scanform.pages_summary(1))")
        env = dict(os.environ, LANGUAGE="", LC_ALL="", LC_MESSAGES="", LANG="de_DE.UTF-8")
        out = subprocess.run([sys.executable, "-c", code], cwd=GUI, env=env, check=True,
                             capture_output=True, text=True).stdout.splitlines()
        self.assertEqual(out, ["Beidseitig", "3 Seiten", "1 Seite"])

    def test_desktop_file_translated(self):
        with open(os.path.join(GUI, "data", "io.github.wsdscan.ScanToPdf.desktop"),
                  encoding="utf-8") as f:
            text = f.read()
        for key in ("GenericName", "Comment", "Keywords"):
            self.assertRegex(text, re.compile(rf"^{key}\[de\]=.+$", re.M))


if __name__ == "__main__":
    unittest.main()
