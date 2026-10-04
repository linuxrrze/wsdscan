#!/bin/sh
# Install "Scan to PDF" (GUI) and the wsdscan command.
#
#   ./install.sh              for the current user (~/.local)
#   sudo ./install.sh --system   for all users (/usr/local)
#   ./install.sh --uninstall  (add --system for a system-wide install)
#   ./install.sh --dist       build scan-to-pdf-<version>.tar.gz to copy to other machines
#
# Needs Python 3.9+, GTK 4.12+ and libadwaita 1.5+ with their Python bindings.
set -eu

APP_ID=io.github.wsdscan.ScanToPdf
HERE=$(cd "$(dirname "$0")" && pwd)
PREFIX=$HOME/.local
MODE=install
SKIP_CHECK=no

die() { echo "error: $*" >&2; exit 1; }

while [ $# -gt 0 ]; do
	case $1 in
		--system) PREFIX=/usr/local ;;
		--prefix) [ $# -ge 2 ] || die "--prefix needs a value"; PREFIX=$2; shift ;;
		--uninstall) MODE=uninstall ;;
		--no-check) SKIP_CHECK=yes ;;
		--dist) MODE=dist ;;
		-h|--help) sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
		*) die "unknown option: $1" ;;
	esac
	shift
done

LIB=$PREFIX/share/wsdscan
BIN=$PREFIX/bin
APPS=$PREFIX/share/applications
ICONS=$PREFIX/share/icons/hicolor
META=$PREFIX/share/metainfo

refresh_caches() {
	command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database -q "$APPS" 2>/dev/null || true
	for tool in gtk4-update-icon-cache gtk-update-icon-cache; do
		if command -v $tool >/dev/null 2>&1; then $tool -q -t -f "$ICONS" 2>/dev/null || true; break; fi
	done
	for tool in kbuildsycoca6 kbuildsycoca5; do  # KDE menu cache
		if command -v $tool >/dev/null 2>&1; then $tool >/dev/null 2>&1 || true; break; fi
	done
}

# wsdscan.py: next to this script (link or copy), else in the repository root.
SRC_CLI=""
for candidate in "$HERE/wsdscan.py" "$HERE/../wsdscan.py"; do
	if [ -r "$candidate" ]; then SRC_CLI=$candidate; break; fi
done

if [ "$MODE" = dist ]; then
	[ -n "$SRC_CLI" ] || die "wsdscan.py not found"
	VERSION=$(sed -n 's/^VERSION = "\(.*\)"/\1/p' "$HERE/scanform.py")
	NAME=scan-to-pdf-$VERSION
	STAGE=$(mktemp -d)
	trap 'rm -rf "$STAGE"' EXIT
	mkdir -p "$STAGE/$NAME/data"
	cp -L "$SRC_CLI" "$HERE/wsdscan_gui.py" "$HERE/scanform.py" "$HERE/tray.py" "$HERE/install.sh" \
		"$HERE/README.md" "$STAGE/$NAME/"
	cp "$HERE/data/"* "$STAGE/$NAME/data/"
	tar -C "$STAGE" -czf "$PWD/$NAME.tar.gz" "$NAME"
	echo "Created $PWD/$NAME.tar.gz"
	echo "On the target machine: tar -xzf $NAME.tar.gz && cd $NAME && ./install.sh"
	exit 0
fi

if [ "$MODE" = uninstall ]; then
	rm -rf "$LIB"
	rm -f "$BIN/wsdscan-gui" "$BIN/wsdscan" "$APPS/$APP_ID.desktop" \
		"$ICONS/scalable/apps/$APP_ID.svg" "$ICONS/symbolic/apps/$APP_ID-symbolic.svg" \
		"$META/$APP_ID.metainfo.xml"
	# A per-user autostart entry would start the removed app at every login.
	rm -f "${XDG_CONFIG_HOME:-$HOME/.config}/autostart/$APP_ID.desktop"
	refresh_caches
	echo "Scan to PDF removed from $PREFIX. Your settings (~/.config/wsdscan) were kept."
	exit 0
fi

# --- dependencies ---------------------------------------------------------------
hint() {
	ID=""; ID_LIKE=""
	[ -r /etc/os-release ] && . /etc/os-release
	case " $ID $ID_LIKE " in
		*" ubuntu "*|*" debian "*) echo "  sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1" ;;
		*" fedora "*|*" rhel "*) echo "  sudo dnf install python3-gobject gtk4 libadwaita" ;;
		*" opensuse"*|*" suse "*) echo "  sudo zypper install python3-gobject-Gdk typelib-1_0-Gtk-4_0 typelib-1_0-Adw-1" ;;
		*" arch "*) echo "  sudo pacman -S python-gobject gtk4 libadwaita" ;;
		*) echo "  Install the Python GObject bindings (PyGObject), GTK 4 and libadwaita with your package manager." ;;
	esac
}

command -v python3 >/dev/null 2>&1 || die "python3 is required"
python3 -c 'import sys; sys.exit(sys.version_info < (3, 9))' || die "Python 3.9 or newer is required"
if [ "$SKIP_CHECK" = no ]; then
	if ! problem=$(python3 - 2>&1 <<'PY'
import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk
if (Gtk.get_major_version(), Gtk.get_minor_version()) < (4, 12):
    raise SystemExit(f"GTK {Gtk.get_major_version()}.{Gtk.get_minor_version()} found, 4.12+ needed")
if (Adw.get_major_version(), Adw.get_minor_version()) < (1, 5):
    raise SystemExit(f"libadwaita {Adw.get_major_version()}.{Adw.get_minor_version()} found, 1.5+ needed")
PY
	); then
		echo "Scan to PDF needs GTK 4 and libadwaita for Python:" >&2
		echo "  $(printf '%s\n' "$problem" | tail -n 1)" >&2
		echo "Install them with:" >&2
		hint >&2
		echo "(Or install anyway with --no-check.)" >&2
		exit 1
	fi
fi

# --- files ------------------------------------------------------------------------
if [ -z "$SRC_CLI" ]; then
	echo "error: wsdscan.py not found (looked in $HERE and $(dirname "$HERE"))." >&2
	echo "The installer needs the whole repository, not just the gui folder. Either:" >&2
	echo "  - run it from a complete copy of the repository (gui/install.sh), or" >&2
	echo "  - build a self-contained package there with 'gui/install.sh --dist' and" >&2
	echo "    copy that archive instead, or" >&2
	echo "  - copy wsdscan.py next to install.sh." >&2
	exit 1
fi

mkdir -p "$LIB" "$BIN" "$APPS" "$ICONS/scalable/apps" "$ICONS/symbolic/apps" "$META"
cp -L "$SRC_CLI" "$HERE/wsdscan_gui.py" "$HERE/scanform.py" "$HERE/tray.py" "$LIB/"
PYTHON=$(command -v python3)
printf '#!/bin/sh\nexec %s %s/wsdscan_gui.py "$@"\n' "$PYTHON" "$LIB" > "$BIN/wsdscan-gui"
printf '#!/bin/sh\nexec %s %s/wsdscan.py "$@"\n' "$PYTHON" "$LIB" > "$BIN/wsdscan"
chmod 755 "$BIN/wsdscan-gui" "$BIN/wsdscan"
# Absolute Exec path: ~/.local/bin is not always on the desktop session's PATH.
sed "s|^Exec=.*|Exec=$BIN/wsdscan-gui|" "$HERE/data/$APP_ID.desktop" > "$APPS/$APP_ID.desktop"
cp "$HERE/data/$APP_ID.svg" "$ICONS/scalable/apps/"
cp "$HERE/data/$APP_ID-symbolic.svg" "$ICONS/symbolic/apps/"
cp "$HERE/data/$APP_ID.metainfo.xml" "$META/"
refresh_caches

echo "Scan to PDF installed in $PREFIX."
echo "  Start it from the application menu (\"Scan to PDF\") or with: $BIN/wsdscan-gui"
echo "  Command-line tool: $BIN/wsdscan"
case ":$PATH:" in *":$BIN:"*) ;; *) echo "  Note: $BIN is not on your PATH." ;; esac
