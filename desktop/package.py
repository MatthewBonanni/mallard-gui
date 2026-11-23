"""Builds the desktop app with its server frozen inside, so it runs without uv, Python or mallard-gui:

    uv run --group bundle desktop/package.py

Writes to desktop/dist, with SHA256SUMS:

- macOS: Mallard-<version>-macos-<arch>.zip (Mallard.app) and .dmg, ad-hoc signed (Apple Silicon runs no
  unsigned code; there is no Developer ID signature or notarization);
- Linux: Mallard-<version>-linux-<arch>.AppImage and .deb;
- Windows: Mallard-<version>-windows-<arch>-setup.exe (an NSIS installer, unsigned).

Needs Rust and `cargo install tauri-cli --version "^2"`, and on Linux Tauri's system packages
(https://v2.tauri.app/start/prerequisites/).
"""

import hashlib
import json
import platform
import shutil
import subprocess
import sys
from pathlib import Path

DESKTOP = Path(__file__).resolve().parent
ROOT = DESKTOP.parent
SHELL = DESKTOP / "src-tauri"
SIDECAR = DESKTOP / "sidecar"
OUT = DESKTOP / "dist"
SERVER = "mallard-gui-server"


def run(*cmd: str | Path, cwd: Path = ROOT) -> None:
    print("+", " ".join(map(str, cmd)), flush=True)
    subprocess.run([str(c) for c in cmd], cwd=cwd, check=True)


def arch() -> str:
    machine = platform.machine().lower()
    return {"amd64": "x86_64", "x64": "x86_64", "aarch64": "arm64"}.get(machine, machine)


def freeze() -> Path:
    print("package: freezing the server", flush=True)
    run(
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--log-level",
        "WARN",
        "--distpath",
        SIDECAR / "dist",
        "--workpath",
        SIDECAR / "build",
        SIDECAR / f"{SERVER}.spec",
    )
    server = SIDECAR / "dist" / SERVER
    run(server / (SERVER + (".exe" if sys.platform == "win32" else "")), "--version")
    return server


def build_shell(bundles: str, *config: str) -> Path:
    print(f"package: building the shell ({bundles})", flush=True)
    bundle = SHELL / "target" / "release" / "bundle"
    shutil.rmtree(bundle, ignore_errors=True)
    run("cargo", "tauri", "build", "--bundles", bundles, *config, cwd=SHELL)
    return bundle


def only(directory: Path, pattern: str) -> Path:
    found = sorted(directory.glob(pattern))
    if len(found) != 1:
        sys.exit(f"package: expected one {pattern} in {directory}, found {[f.name for f in found]}")
    return found[0]


def macos(server: Path, name: str) -> list[Path]:
    bundle = build_shell("app")
    # The frozen server goes in by ditto, not Tauri's bundle.resources, which would copy its symlinks as files
    # (Python.framework, the libraries): twice the size and a framework codesign rejects.
    app = OUT / "Mallard.app"
    run("ditto", bundle / "macos" / "Mallard.app", app)
    run("ditto", server, app / "Contents" / "Resources" / SERVER)
    print("package: ad-hoc signing", flush=True)
    # PyInstaller has ad-hoc signed the server's own executable and libraries; this signs the shell and seals
    # the bundle.
    run("codesign", "--force", "--deep", "-s", "-", app)
    run("codesign", "--verify", "--deep", "--strict", app)
    zipped, dmg = OUT / f"{name}.zip", OUT / f"{name}.dmg"
    run("ditto", "-c", "-k", "--keepParent", app, zipped)
    staging = OUT / "dmg"
    run("ditto", app, staging / "Mallard.app")
    (staging / "Applications").symlink_to("/Applications")
    run("hdiutil", "create", "-quiet", "-volname", "Mallard", "-srcfolder", staging, "-ov", "-format", "UDZO", dmg)
    shutil.rmtree(staging)
    return [zipped, dmg]


def bundled_server(server: Path) -> str:
    """Tauri config that copies the frozen server into the bundle's resources, where the shell looks for it."""
    source = server.relative_to(SHELL, walk_up=True).as_posix()
    return json.dumps({"bundle": {"resources": {source: SERVER}}})


def linux(server: Path, name: str) -> list[Path]:
    bundle = build_shell("appimage,deb", "--config", bundled_server(server))
    appimage, deb = OUT / f"{name}.AppImage", OUT / f"{name}.deb"
    shutil.copy2(only(bundle / "appimage", "*.AppImage"), appimage)
    shutil.copy2(only(bundle / "deb", "*.deb"), deb)
    return [appimage, deb]


def windows(server: Path, name: str) -> list[Path]:
    bundle = build_shell("nsis", "--config", bundled_server(server))
    setup = OUT / f"{name}-setup.exe"
    shutil.copy2(only(bundle / "nsis", "*-setup.exe"), setup)
    return [setup]


def main() -> None:
    builders = {"darwin": ("macos", macos), "linux": ("linux", linux), "win32": ("windows", windows)}
    if sys.platform not in builders:
        sys.exit(f"package: no desktop bundle for {sys.platform}")
    system, build = builders[sys.platform]
    version = json.loads((SHELL / "tauri.conf.json").read_text())["version"]
    server = freeze()
    shutil.rmtree(OUT, ignore_errors=True)
    OUT.mkdir(parents=True)
    files = build(server, f"Mallard-{version}-{system}-{arch()}")
    sums = "".join(f"{hashlib.sha256(f.read_bytes()).hexdigest()}  {f.name}\n" for f in files)
    (OUT / "SHA256SUMS").write_text(sums, newline="\n")
    print(sums, end="")


if __name__ == "__main__":
    main()
