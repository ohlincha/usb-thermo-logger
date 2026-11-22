#!/bin/sh
# Build an architecture-independent .deb: dist/usb-thermo-logger_<version>_all.deb
set -eu
cd "$(dirname "$0")/../.."
VERSION=$(python3 -c "import re;print(re.search(r'__version__ = \"(.+?)\"', open('thermo_logger.py').read()).group(1))")
PKG=build/deb/usb-thermo-logger_${VERSION}_all
rm -rf "$PKG"
install -Dm644 thermo_logger.py   "$PKG/usr/lib/usb-thermo-logger/thermo_logger.py"
install -Dm644 thermo_logger_gui.py "$PKG/usr/lib/usb-thermo-logger/thermo_logger_gui.py"
install -Dm644 70-usb-thermo-logger.rules "$PKG/usr/lib/udev/rules.d/70-usb-thermo-logger.rules"
install -Dm644 packaging/linux/usb-thermo-logger.desktop "$PKG/usr/share/applications/usb-thermo-logger.desktop"
install -Dm644 LICENSE            "$PKG/usr/share/doc/usb-thermo-logger/copyright"
install -Dm644 README.md          "$PKG/usr/share/doc/usb-thermo-logger/README.md"
mkdir -p "$PKG/usr/bin" "$PKG/DEBIAN"
printf '#!/bin/sh\nexec python3 /usr/lib/usb-thermo-logger/thermo_logger_gui.py "$@"\n' > "$PKG/usr/bin/thermo-logger"
printf '#!/bin/sh\nexec python3 /usr/lib/usb-thermo-logger/thermo_logger.py "$@"\n' > "$PKG/usr/bin/thermo-logger-cli"
chmod 755 "$PKG/usr/bin/thermo-logger" "$PKG/usr/bin/thermo-logger-cli"
cat > "$PKG/DEBIAN/control" <<CTRL
Package: usb-thermo-logger
Version: $VERSION
Architecture: all
Maintainer: usb-thermo-logger maintainers <noreply@github.com>
Depends: python3 (>= 3.7), python3-tk
Section: science
Priority: optional
Homepage: https://github.com/ohlincha/usb-thermo-logger
Description: Live display and logging for TEMPer-compatible USB sensors
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
