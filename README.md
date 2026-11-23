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

Requires Python ≥ 3.12, [uv](https://docs.astral.sh/uv/) and a `mallard` build on `PATH` (or `--mallard PATH`).

```bash
uv tool install git+https://github.com/MatthewBonanni/mallard-gui
mallard-gui path/to/case          # opens the GUI in a browser
```

Or `sh install.sh` (installs uv where missing; `--solver` also installs Mallard).

### Desktop app (macOS)

A Tauri shell around the same server. Needs Rust and the Tauri CLI:

```bash
cargo install tauri-cli --version "^2"
cd desktop/src-tauri && cargo tauri build --bundles app   # uses the installed mallard-gui
sh desktop/package.sh                                      # self-contained, ad-hoc-signed Mallard.app
```

## Docs

- [Guide](docs/guide.md): install options, every page, remotes, data, development, compatibility

## Development

```bash
uv sync
uv run ruff check && uv run mypy && uv run pytest
```

## License

[Apache-2.0](LICENSE)
