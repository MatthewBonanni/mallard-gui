#!/bin/sh
# Builds the unsigned desktop app with its server frozen inside, so it runs without uv or mallard-gui:
#
#   sh desktop/package.sh
#
# macOS: desktop/dist/Mallard-<version>-macos-<arch>.zip and .dmg, ad-hoc signed (Apple Silicon runs no
# unsigned code; there is no Developer ID signature or notarization), and SHA256SUMS. Needs uv, Rust and
# `cargo install tauri-cli --version "^2"`.
set -eu

desktop=$(cd "$(dirname "$0")" && pwd)
root=$(dirname "$desktop")
out="$desktop/dist"
[ "$(uname -s)" = Darwin ] || { echo "package.sh: macOS only for now" >&2; exit 2; }

version=$(sed -n 's/^ *"version": *"\(.*\)",*$/\1/p' "$desktop/src-tauri/tauri.conf.json" | head -n 1)
arch=$(uname -m)
name="Mallard-$version-macos-$arch"

echo "package.sh: freezing the server"
(cd "$root" && uv run --group bundle pyinstaller --noconfirm --log-level WARN \
    --distpath "$desktop/sidecar/dist" --workpath "$desktop/sidecar/build" \
    "$desktop/sidecar/mallard-gui-server.spec")
server="$desktop/sidecar/dist/mallard-gui-server"
"$server/mallard-gui-server" --version

echo "package.sh: building the shell"
(cd "$desktop/src-tauri" && cargo tauri build --bundles app)

# The frozen server goes in by ditto, not Tauri's bundle.resources, which would copy its symlinks as files
# (Python.framework, the libraries): twice the size and a framework codesign rejects.
rm -rf "$out"
mkdir -p "$out"
app="$out/Mallard.app"
ditto "$desktop/src-tauri/target/release/bundle/macos/Mallard.app" "$app"
ditto "$server" "$app/Contents/Resources/mallard-gui-server"

echo "package.sh: ad-hoc signing"
# PyInstaller has ad-hoc signed the server's own executable and libraries; this signs the shell and seals
# the bundle.
codesign --force --deep -s - "$app"
codesign --verify --deep --strict "$app"

echo "package.sh: packaging"
ditto -c -k --keepParent "$app" "$out/$name.zip"
staging="$out/dmg"
mkdir -p "$staging"
ditto "$app" "$staging/Mallard.app"
ln -s /Applications "$staging/Applications"
hdiutil create -quiet -volname Mallard -srcfolder "$staging" -ov -format UDZO "$out/$name.dmg"
rm -rf "$staging"
(cd "$out" && shasum -a 256 "$name.zip" "$name.dmg" > SHA256SUMS && cat SHA256SUMS)
