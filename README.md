# mallard-gui

A desktop GUI for the [Mallard](https://github.com/MatthewBonanni/mallard) compressible-flow solver.

**Status:** in active development; interfaces and file formats may change.

- Case setup: case tree, key inspector and `input.toml` editor, all checked live by `mallard check`
- Case view: mesh, boundaries, regions and probes; initial state and mesh-quality overlays
- Runs: cost and memory estimate before launch; live monitor with probes, forces and health
- Results: snapshot viewer, probes and lines over time, export to ParaView
- Local runs, plus SSH (Slurm, PBS, LSF) and Kubernetes remotes
- Drives the `mallard` command line; never re-implements solver logic

## Screenshots

| | |
|---|---|
| ![Case setup](docs/images/case-setup.png) | ![Case preview](docs/images/case-preview.png) |
| Case setup | Case preview |
| ![Run dialog](docs/images/run-dialog.png) | ![Results](docs/images/results.png) |
| Run dialog | Results |

Cylinder at Re = 100, Mach 0.2 (Mallard's `examples/cylinder`, coarsened to 12,288 cells).

## Install and run

- **Desktop app (recommended):** download from [Releases](https://github.com/MatthewBonanni/mallard-gui/releases);
  each bundle carries its own server, so no Python or uv
  - macOS (Apple Silicon): `Mallard-<version>-macos-arm64.dmg` (drag to Applications) or `.zip` (`Mallard.app`)
  - Linux (x86-64): `Mallard-<version>-linux-x86_64.AppImage` (`chmod +x`, run) or `.deb` (`sudo apt install ./…deb`)
  - Windows (x86-64): `Mallard-<version>-windows-x86_64-setup.exe`, an installer
  - `SHA256SUMS`: check with `sha256sum -c SHA256SUMS` (`shasum -a 256 -c` on macOS)
  - Unsigned: macOS (ad-hoc signed, not notarized) blocks the first open until System Settings > Privacy & Security >
    Open Anyway; Windows SmartScreen warns until More info > Run anyway
- **Browser mode:** the same GUI served to a browser, for remote or headless machines and for development
  - Installs the GUI server only, not the desktop app; needs Python ≥ 3.12 and
    [uv](https://docs.astral.sh/uv/getting-started/installation/)
  - `uv tool install git+https://github.com/MatthewBonanni/mallard-gui`
  - `mallard-gui path/to/case` opens a browser; over SSH, `mallard-gui --no-browser --port 8080` there and
    `ssh -L 8080:127.0.0.1:8080 host` here
- **Mallard:** either way, the GUI drives a `mallard` build on `PATH` (or `--mallard PATH`, or Settings > Mallard)
  - Install it from Mallard's [installation docs](https://matthewbonanni.github.io/mallard/docs/installation/):
    container, Spack or from source
  - Mallard itself runs on Linux and macOS, not Windows
- **Building the desktop app from source:** [uv](https://docs.astral.sh/uv/getting-started/installation/), Rust,
  `cargo install tauri-cli --version "^2"` and, on Linux, [Tauri's packages](https://v2.tauri.app/start/prerequisites/)
  - `uv run --group bundle desktop/package.py` writes this OS's bundles and `SHA256SUMS` to `desktop/dist/`
  - Building only the shell, and signing: [guide](docs/guide.md#desktop-app)

## Docs

- [Guide](docs/guide.md): install options, every page, remotes, data, development, compatibility

## Development

```bash
uv sync
uv run ruff check && uv run mypy && uv run pytest
```

## License

[Apache-2.0](LICENSE)
