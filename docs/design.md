# mallard-gui design notes

For developers. The user guide is [guide.md](guide.md).

## Architecture

- `mallard-gui` is a trame server on `127.0.0.1` with a per-session token; the page is its client. The desktop app
  is a Tauri shell around the same server.
- Solver knowledge comes only from Mallard: `mallard list keys`, `mallard check`, `mallard session`, the run log
  `mallard.jsonl`, and `mallard post`/`diff`/`info`/`install verify`.
- Input is edited as a document (tomlkit) that preserves comments and layout and is written atomically.
- The project directory holds committed identity only (`mallard-project.toml`, `mallard-runs/`). Where copies
  of a run's files are is per-machine knowledge in the cache's `index.sqlite`, rebuilt from the locations on
  Refresh.

### Data identity

- Objects (snapshots, restarts, inputs) are known by SHA-256, from the run log's output records or hashed where
  they are. Streams (run log, probe, force and integral CSV files) are known by length and the SHA-256 of that
  prefix, and are appended after the prefix is checked.
- Objects recorded with a chunk tree (`sha256_tree`, `chunk_bytes`, `chunks`) move in chunks, eight at a time,
  each verified where it lands and retried once; a resumed copy moves only the chunks that differ.
- Direct transfers between two SSH remotes use the forwarded agent. The first time two remotes meet, a
  background probe tries `ssh` each way and looks for a marker file of one in the other's storage; the result
  is kept in `remotes.toml` and redone after a direct transfer fails.
- The run log is read incrementally; histories of long runs are held as min/max/last bins at a few resolutions,
  and a zoom past what is held rereads only its byte range.

## Interface to Mallard

The GUI reads check report version 1 and run log versions 2 to 3, and says so when a build writes a newer one.

It keeps one `mallard session` per case (started with the case's input, so the front end picks the dimension's
solver). What a build answers is read from `initialize`'s `methods`, never from its version; a build without a
request gets the fallback.

| Request or field | Used for | Fallback |
|---|---|---|
| `initialize` | live-settable keys (Control tab), control files (Stop, Checkpoint), socket commands, devices | `check --help`, `ctl --help` |
| `units.catalog` | units, prefixes, exact factors, each dimension's selector | the bundled table in `units.py` |
| `case.boundary_values` | inflow arrows at every face, with the editor's unsaved text | the entry's own values; `u` expressions sampled here; estimates from the initial state |
| `case.view` frames | where a boundary's u starts and which way it grows | start points parsed from the check's text |
| check `key`, `line`, `column` | each diagnostic's key and position | keys and lines parsed from the message; setup warnings from stderr |
| check `unassigned`, `periodic` | boundary rows; unassigned zones as case-tree nodes | none |
| `file.info` | Continue enabled by a restart's `continuable`; its `incompatibilities` shown | Continue for any stopped run with a restart |
| `case.files` | the Files tab | the tab says the build does not list them |
| key-catalog `models` | the (i) button and each model's source in the dropdown | no button |
| key-catalog `form` | arrays of tables, named entries and tables in the tree and editor | guessed from types and `name`/`region` keys |

Where Mallard does not yet provide something, a `TODO(#issue)` in the source marks the workaround:

| Issue | Workaround |
|---|---|
| #1256 | catalog defaults unquoted; section order and file roles built in; the files an input names, for staging |
| #1311 | a partial input fails `list keys`: the last good catalog is kept |
| #1313 | case-view block names parsed for their input path; 3D cell zones not drawn |
| #1314 | version parsed from `--version` |
| #1316 | the run's input copy sets `run.control_file_interval` |
| #1317 | manifests built from the run log; files listed and hashed by a POSIX shell script on the location |
| #1318 | restart compatibility judged by comparing `mallard info` at home and at the destination |
| #1320 | a Mallard without `install verify` gets the environment check alone |
| #1322 | a release without `release-manifest.json`: archives named by convention, checked against `.sha256` files |
| #1360, #1361 | a wall is extracted through a copy of the run's input with a boundary `[[probes]]` entry added |
| #1362 | spectra, autocorrelation, window statistics and coefficients computed in the GUI (`signals.py`) |
| #1363 | a nondimensional run's CSV units lines say SI; plots go by the input |

## Desktop shell

- `desktop/src-tauri` starts `mallard-gui PROJECT --no-browser --url-file FILE` and shows the page once the URL
  is written; it stops the server on quit. The server is `$MALLARD_GUI_SERVER`, else the frozen server in the
  app's resources, else `mallard-gui` on the login `PATH` or in `~/.local/bin`.
- Until the first frame, the window shows `assets/loading.html` with the server's stages
  (`$MALLARD_GUI_PROGRESS_FILE`), or why it failed, with Retry and Show Log.
- Native menus map to the page's document commands (`ui/document_panel.py`), so a browser has every command too.
  The shell points the server at its settings with `$MALLARD_GUI_SETTINGS`, so Open Recent reads the same
  `recent` list.
- The page reaches the shell through `window.mallardShell` (absent in a browser; allowed only for the GUI's
  origin), version 3:

| Call | Effect |
|---|---|
| `notify({title, body, level})` | a native notification; `level` is `info`, `warning` or `error` (the last two sound) |
| `setDocument({title, path, edited, canUndo, canRedo, mesh})` | window title, edited marker, proxy icon, menu state |
| `choosePath({mode, title, directory, name, filters})` | native open or save panel; resolves to a path or `null` |
| `openProject(path)`, `closeProject()`, `quit()` | after the page has saved or released its edits |
| `reveal(path)`, `openUrl(url)` | reveal in the file manager; open an https URL |

- Tests: `MALLARD_DESKTOP_NOTIFICATIONS=FILE` appends notifications to FILE as JSON lines instead of posting them;
  `MALLARD_DESKTOP_DATA=DIR` keeps the shell's state and the server's `settings.toml` in DIR.

### Packaging

- `desktop/package.py` freezes the server with PyInstaller (`desktop/sidecar/mallard-gui-server.spec`, a
  `--onedir` build), builds the shell, and adds the server to the app's resources as `mallard-gui-server/`.
- macOS: the server is copied in with `ditto` (Tauri's `bundle.resources` copies symlinks as files, doubling the
  size and breaking `Python.framework`'s signature), the bundle is ad-hoc signed (`codesign --force --deep -s -`),
  then zipped and put in a dmg. Linux and Windows pass the server to Tauri's `bundle.resources` for the
  AppImage, deb and NSIS installer.
- `.github/workflows/desktop-release.yml` runs `package.py` on macOS (arm64), Ubuntu 22.04 (x86-64) and Windows
  for a `v*` tag and attaches the bundles and `SHA256SUMS` to its release.
- A shell that uses an installed `mallard-gui` instead of a frozen server:
  `cd desktop/src-tauri && cargo tauri build --bundles app` (`deb` or `nsis` elsewhere); `cargo tauri dev` runs
  it from the checkout.
- Signing with a Developer ID (untested): `codesign --force --options runtime -s "Developer ID Application: …"`
  on the server's libraries, then the app (the frozen Python needs
  `com.apple.security.cs.disable-library-validation`); `xcrun notarytool submit --wait` the zip;
  `xcrun stapler staple Mallard.app`.

## Development

```bash
uv sync
uv run ruff check && uv run ruff format --check
uv run mypy
uv run pytest                                  # tests marked `mallard` skip without a build
MALLARD=/path/to/mallard uv run pytest -m mallard
```

- SSH is tested without a server: `tests/fixtures/fake-ssh` stands in for OpenSSH and runs the remote command
  with the local shell (`$MALLARD_GUI_SSH` selects the client). Transfers, interruptions and resumption run
  through it.
- Tests marked `ui` drive every page through Playwright in Chrome and WebKit, in both themes, with a layout check
  (`tests/ui/alignment.js`: alignment, sizes, clipping, overflow, WCAG AA contrast, visible focus, hover).
- `python -m tests.ui.screenshots DIR` shoots every page, panel, dialog and state at 2880×1800 in both themes and
  engines.
- Design tokens (`src/mallard_gui/assets/tokens.json`) copy the website's `design/tokens.json`; `tokens.py`
  records the source commit. The UI font is Inter (OFL, bundled). The duck mark comes from the website's
  `content/assets/icon.png` and `favicon.png`.
