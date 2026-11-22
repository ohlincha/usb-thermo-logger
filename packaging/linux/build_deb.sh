#!/bin/sh
# Build an architecture-independent .deb: dist/temper-monitor_<version>_all.deb
set -eu
cd "$(dirname "$0")/../.."
VERSION=$(python3 -c "import re;print(re.search(r'__version__ = \"(.+?)\"', open('temper.py').read()).group(1))")
PKG=build/deb/temper-monitor_${VERSION}_all
rm -rf "$PKG"
install -Dm644 temper.py          "$PKG/usr/lib/temper-monitor/temper.py"
install -Dm644 temper_gui.py      "$PKG/usr/lib/temper-monitor/temper_gui.py"
install -Dm644 70-temper.rules    "$PKG/usr/lib/udev/rules.d/70-temper.rules"
install -Dm644 packaging/linux/temper-monitor.desktop "$PKG/usr/share/applications/temper-monitor.desktop"
install -Dm644 LICENSE            "$PKG/usr/share/doc/temper-monitor/copyright"
install -Dm644 README.md          "$PKG/usr/share/doc/temper-monitor/README.md"
mkdir -p "$PKG/usr/bin" "$PKG/DEBIAN"
printf '#!/bin/sh\nexec python3 /usr/lib/temper-monitor/temper_gui.py "$@"\n' > "$PKG/usr/bin/temper-monitor"
printf '#!/bin/sh\nexec python3 /usr/lib/temper-monitor/temper.py "$@"\n'     > "$PKG/usr/bin/temper"
chmod 755 "$PKG/usr/bin/temper-monitor" "$PKG/usr/bin/temper"
cat > "$PKG/DEBIAN/control" <<CTRL
Package: temper-monitor
Version: $VERSION
Architecture: all
Maintainer: temper-monitor maintainers <noreply@github.com>
Depends: python3 (>= 3.7), python3-tk
Section: science
Priority: optional
Homepage: https://github.com/ohlincha/temper-monitor
Description: Live display and logging for PCsensor TEMPer USB sensors
 Reads TEMPer (0c45:7401), TEMPerHUM (0c45:7402) and TH1000 (0c45:7403)
 USB thermometers/hygrometers, shows live values and a plot, and logs to CSV.
 Includes a udev rule so the sensors can be used without root.
CTRL
cat > "$PKG/DEBIAN/postinst" <<'POST'
#!/bin/sh
set -e
if [ "$1" = configure ] && command -v udevadm >/dev/null 2>&1; then
    udevadm control --reload || true
    udevadm trigger --subsystem-match=hidraw --action=add || true
fi
POST
chmod 755 "$PKG/DEBIAN/postinst"
mkdir -p dist
fakeroot dpkg-deb --build --root-owner-group "$PKG" dist/ 2>/dev/null || dpkg-deb --build --root-owner-group "$PKG" dist/
ls -l dist/*.deb
