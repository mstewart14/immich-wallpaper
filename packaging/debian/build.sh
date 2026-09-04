#!/usr/bin/env bash
# Builds immich-wallpaper_<version>_all.deb from the repo root.
# Usage: packaging/debian/build.sh [version]
set -euo pipefail

VERSION="${1:-1.0.0}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/../.." && pwd)"
BUILD_DIR="$(mktemp -d)"
PKG_DIR="$BUILD_DIR/immich-wallpaper_${VERSION}_all"

mkdir -p "$PKG_DIR/DEBIAN" "$PKG_DIR/usr/share/immich-wallpaper/assets" \
         "$PKG_DIR/usr/bin" "$PKG_DIR/etc/xdg/autostart"

cp "$REPO_ROOT/config_ui.py" "$REPO_ROOT/index.html" "$REPO_ROOT/rotate.py" "$REPO_ROOT/tray_app.py" \
   "$PKG_DIR/usr/share/immich-wallpaper/"
cp "$REPO_ROOT/assets/immich-flower.png" "$PKG_DIR/usr/share/immich-wallpaper/assets/"

declare -A entry_points=([tray]=tray_app.py [config]=config_ui.py [rotate]=rotate.py)
for name in "${!entry_points[@]}"; do
cat > "$PKG_DIR/usr/bin/immich-wallpaper-$name" <<EOF
#!/bin/sh
exec python3 /usr/share/immich-wallpaper/${entry_points[$name]} "\$@"
EOF
done
chmod 755 "$PKG_DIR"/usr/bin/*

cat > "$PKG_DIR/etc/xdg/autostart/immich-wallpaper-tray.desktop" <<'EOF'
[Desktop Entry]
Type=Application
Name=Immich Wallpaper Tray
Comment=Status icon and controls for the Immich wallpaper rotator
Exec=immich-wallpaper-tray
Icon=preferences-desktop-wallpaper
Terminal=false
Categories=Utility;
X-GNOME-Autostart-enabled=true
EOF

INSTALLED_SIZE=$(du -sk "$PKG_DIR" | cut -f1)

cat > "$PKG_DIR/DEBIAN/control" <<EOF
Package: immich-wallpaper
Version: $VERSION
Section: utils
Priority: optional
Architecture: all
Installed-Size: $INSTALLED_SIZE
Depends: python3 (>= 3.8), python3-pil, python3-pystray, python3-gi, gir1.2-ayatanaappindicator3-0.1
Recommends: kdialog | zenity
Maintainer: mstewart14 <46582721+mstewart14@users.noreply.github.com>
Homepage: https://github.com/mstewart14/immich-wallpaper
Description: Rotate desktop wallpaper from a self-hosted Immich library
 Pulls random photos from a configured Immich server (optionally filtered
 by album or person), sets them as the desktop wallpaper, and keeps a
 bounded number of recent images on disk with back/forward history. Ships
 a browser-based settings page and a system tray icon for status and
 quick controls. Supports KDE Plasma and XFCE.
EOF

OUT="$REPO_ROOT/immich-wallpaper_${VERSION}_all.deb"
dpkg-deb --build --root-owner-group "$PKG_DIR" "$OUT"
echo "Built: $OUT"
