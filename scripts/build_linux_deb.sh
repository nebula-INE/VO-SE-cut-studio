#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VERSION="$(printenv AXRAL_PACKAGE_VERSION || true)"
if [[ -z "$VERSION" ]]; then VERSION="0.1.0"; fi
ARCH="$(dpkg --print-architecture)"
PACKAGE="axral-cut-studio"
STAGE="$ROOT/release/deb-stage"
APP_SOURCE="$ROOT/dist/AxralCutStudio"

if [[ ! -x "$APP_SOURCE/AxralCutStudio" ]]; then
  echo "Packaged application was not found: $APP_SOURCE/AxralCutStudio" >&2
  exit 1
fi

rm -rf "$STAGE"
mkdir -p "$STAGE/DEBIAN" "$STAGE/opt/axral-cut-studio" "$STAGE/usr/share/applications"
cp -a "$APP_SOURCE/." "$STAGE/opt/axral-cut-studio/"

cat > "$STAGE/DEBIAN/control" <<EOF
Package: $PACKAGE
Version: $VERSION
Section: sound
Priority: optional
Architecture: $ARCH
Maintainer: Axral
Depends: ffmpeg, libgl1, libegl1, libxcb-cursor0, libxkbcommon0, libxkbcommon-x11-0, libxcb-xinerama0, libxcb-icccm4, libxcb-image0, libxcb-keysyms1, libxcb-render-util0, libdbus-1-3, libpulse0
Description: Axral Cut Studio desktop video and audio editing application
 Cross-platform script, audio, motion and video production editor.
EOF

cat > "$STAGE/usr/share/applications/axral-cut-studio.desktop" <<'EOF'
[Desktop Entry]
Type=Application
Name=Axral Cut Studio
Comment=Script, audio, motion and video production
Exec=/opt/axral-cut-studio/AxralCutStudio %F
Terminal=false
Categories=AudioVideo;Audio;Video;Editing;
EOF

dpkg-deb --build --root-owner-group "$STAGE" "$ROOT/release/axral-cut-studio-$VERSION-$ARCH.deb"
rm -rf "$STAGE"
