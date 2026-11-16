#!/bin/bash
# Build a native macOS app; no package manager or GUI framework dependency.
set -euo pipefail
SOURCE_DIR="$(cd "$(dirname "$0")" && pwd)"
DESTINATION="${1:-$(dirname "$SOURCE_DIR")/录音转写.app}"
VERSION="$(cat "$SOURCE_DIR/VERSION")"
STAGING="$(mktemp -d "${TMPDIR:-/tmp}/transcriber-app.XXXXXX")"
trap 'rm -rf "$STAGING"' EXIT
APP="$STAGING/录音转写.app"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources/程序文件"
xcrun swiftc -swift-version 5 -parse-as-library -O -target arm64-apple-macosx14.0 \
  "$SOURCE_DIR/TranscriberApp.swift" -o "$APP/Contents/MacOS/录音转写"
cat > "$APP/Contents/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleExecutable</key><string>录音转写</string>
<key>CFBundleIdentifier</key><string>local.kk.audio-transcriber.simple</string>
<key>CFBundleName</key><string>录音转写</string>
<key>CFBundleDisplayName</key><string>录音转写</string>
<key>CFBundleIconFile</key><string>AppIcon</string>
<key>CFBundlePackageType</key><string>APPL</string>
<key>CFBundleShortVersionString</key><string>$VERSION</string>
<key>CFBundleVersion</key><string>$VERSION</string>
<key>LSMinimumSystemVersion</key><string>14.0</string>
<key>NSHighResolutionCapable</key><true/>
<key>NSDesktopFolderUsageDescription</key><string>选择并在本机转写桌面中的录音。</string>
<key>NSDocumentsFolderUsageDescription</key><string>选择并在本机转写文稿中的录音。</string>
<key>NSDownloadsFolderUsageDescription</key><string>选择并在本机转写下载中的录音。</string>
</dict></plist>
EOF
# Render a local vector icon at Retina resolution; no downloaded image assets.
cat > "$STAGING/icon.swift" <<'SWIFT'
import AppKit
let bitmap = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: 1024, pixelsHigh: 1024,
    bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
    colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
NSGraphicsContext.saveGraphicsState()
NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: bitmap)
let shape = NSBezierPath(roundedRect: NSRect(x: 40, y: 40, width: 944, height: 944), xRadius: 216, yRadius: 216)
let gradient = NSGradient(starting: NSColor(calibratedWhite: 0.12, alpha: 1),
                          ending: NSColor(calibratedWhite: 0.21, alpha: 1))!
gradient.draw(in: shape, angle: 75)
NSColor.white.setStroke()
let heights: [CGFloat] = [110, 240, 380, 180, 280, 130]
for (index, height) in heights.enumerated() {
    let bar = NSBezierPath()
    bar.lineWidth = 30
    bar.lineCapStyle = .round
    let x: CGFloat = 267 + CGFloat(index) * 98
    bar.move(to: NSPoint(x: x, y: 512 - height / 2))
    bar.line(to: NSPoint(x: x, y: 512 + height / 2))
    bar.stroke()
}
NSGraphicsContext.restoreGraphicsState()
try bitmap.representation(using: .png, properties: [:])!.write(to: URL(fileURLWithPath: CommandLine.arguments[1]))
SWIFT
xcrun swift "$STAGING/icon.swift" "$STAGING/icon.png"
ICONSET="$STAGING/AppIcon.iconset"
mkdir -p "$ICONSET"
for size in 16 32 128 256 512; do
  sips -z "$size" "$size" "$STAGING/icon.png" --out "$ICONSET/icon_${size}x${size}.png" >/dev/null
  retina=$((size * 2))
  sips -z "$retina" "$retina" "$STAGING/icon.png" --out "$ICONSET/icon_${size}x${size}@2x.png" >/dev/null
done
iconutil -c icns "$ICONSET" -o "$APP/Contents/Resources/AppIcon.icns"
# Only top-level runtime files: keep test fixtures, caches and app bundles out.
for resource in "$SOURCE_DIR"/*; do
  [ -f "$resource" ] || continue
  case "$resource" in *.pyc|*.swift|*/build_app.sh|*/build_release.sh|*/build_audio_runtime.sh|*/package_release.py|*/test_portable_release.py) continue ;; esac
  cp -p "$resource" "$APP/Contents/Resources/程序文件/"
done
codesign --force --sign - "$APP" >/dev/null
codesign --verify --deep --strict "$APP"
mkdir -p "$(dirname "$DESTINATION")"
if [ -e "$DESTINATION" ]; then
  [ -d "$DESTINATION/Contents" ] || { echo "目标不是应用目录：$DESTINATION" >&2; exit 1; }
  rm -rf "$DESTINATION"
fi
mv "$APP" "$DESTINATION"
echo "已构建：$DESTINATION"
