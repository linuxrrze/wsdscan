"""The installer with an awkward home folder (spaces, quotes, &, $)."""

import configparser
import os
import subprocess
import sys
import tempfile
import unittest

GUI = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class InstallerTest(unittest.TestCase):
    def test_hostile_home_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = os.path.join(tmp, "John O'Neil & Co $x; rm -rf ~")
            os.makedirs(home)
            env = dict(os.environ, HOME=home, XDG_CONFIG_HOME="",
                       WSDSCAN_CONFIG=os.path.join(tmp, "config.ini"))
            run = lambda *argv: subprocess.run(  # noqa: E731
                ["sh", os.path.join(GUI, "install.sh"), *argv], env=env,
                capture_output=True, text=True, timeout=120)
            r = run("--no-check")
            self.assertEqual(r.returncode, 0, r.stderr)
            # The wrapper runs the installed CLI despite the odd path.
            r = subprocess.run([os.path.join(home, ".local", "bin", "wsdscan"), "--show-config"],
                               env=env, capture_output=True, text=True, timeout=60)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("config file:", r.stdout)
            self.assertTrue(os.path.isdir(home), "home folder still there")
            desktop = os.path.join(home, ".local", "share", "applications",
                                   "io.github.wsdscan.ScanToPdf.desktop")
            parser = configparser.ConfigParser(interpolation=None)
            parser.optionxform = str
            parser.read(desktop)
            exec_line = parser["Desktop Entry"]["Exec"]
            self.assertTrue(exec_line.startswith('"') and exec_line.endswith('wsdscan-gui"'))
            self.assertIn("\\\\$x", exec_line)  # $ escaped, backslash doubled for the key file
            r = run("--uninstall")
            self.assertEqual(r.returncode, 0, r.stderr)
            files = [f for _d, _s, fs in os.walk(os.path.join(home, ".local")) for f in fs]
            self.assertEqual(files, [])


if __name__ == "__main__":
    unittest.main()
