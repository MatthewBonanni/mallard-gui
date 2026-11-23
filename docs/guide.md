# mallard-gui guide

The full reference for installing, using and developing mallard-gui. The [README](../README.md) is the short version.

The graphical front end of [Mallard](https://github.com/MatthewBonanni/mallard): set up a case, check it,
run it and watch it, on this machine (and, later, on clusters). It drives the `mallard` command line and
never re-implements solver logic: what the GUI shows about keys, defaults, diagnostics and runs comes from
`mallard list keys`, `mallard check` and the run log `mallard.jsonl`.

The design is Mallard's `docs/design/gui.md`; this package implements its milestone M1 in progress.

## Install

- **Desktop app**: a download for macOS, Linux or Windows that carries its own server; see
  [Install the desktop app](#install-the-desktop-app).
- **Browser mode**: the server alone, which opens the GUI in a browser (no desktop shell), with
  [uv](https://docs.astral.sh/uv/getting-started/installation/) and Python ≥ 3.12:
  `uv tool install --compile-bytecode git+https://github.com/MatthewBonanni/mallard-gui` (from a checkout:
  `uv tool install --compile-bytecode .`; without the flag the first launch compiles the GUI and its
  dependencies). `uv tool update-shell` puts uv's tool directory on `PATH` if it is not already.
- **Mallard**: install it by Mallard's [installation docs](https://matthewbonanni.github.io/mallard/docs/installation/)
  (container, Spack or from source). The GUI uses `--mallard PATH`, else `$MALLARD`, else a `mallard` in its
  own environment, else `mallard` on `PATH` (the `mallard` front end with `mallard2d` and `mallard3d` beside
  it). `--with mallard-python` on the `uv tool install` puts the `mallard-python` wheel's executables (CPU) in
  the GUI's environment ([#1321](https://github.com/MatthewBonanni/mallard/issues/1321)); until that wheel is
  on PyPI, add `--find-links DIR` for a directory of wheels you built.

### Desktop app

`desktop/` is a Tauri shell: a native window on the same `mallard-gui` server. It opens the project
given as its argument, else the last one, else one you pick; starts `mallard-gui PROJECT --no-browser
--url-file FILE` (`$MALLARD_GUI_SERVER`, else the server frozen into a packaged app, else `mallard-gui`
on your login `PATH` or in `~/.local/bin`); shows the GUI once the server writes its URL; and stops the
server when you quit. Until then the window shows the startup screen (`assets/loading.html`, which the GUI's
page also shows until its first frame): the server's stages as it reports them (`$MALLARD_GUI_PROGRESS_FILE`),
or why it failed, with the log's path, Retry and Show Log.

The app has a native menu bar: Mallard (About, Settings, Hide, Quit), File (New Project, Open, Open
Recent, Close Project, Save, Save As, Revert to Saved, Import Mesh (Shift Cmd I: the mesh file through the
open panel), Open in ParaView (Shift Cmd P: the results view's series and view), Reveal in Finder, named Show in
Explorer or Show in File Manager elsewhere), Edit (Undo and Redo of the case's
edits, the system's Cut, Copy, Paste and Select All), View (Show Mesh, Zoom to Fit, the theme, full
screen), Window and Help. Its document items are the GUI page's own commands (see [Documents](#documents)),
so a browser has every one of them too; Open, Save As, New Project and every other file choice (the mesh,
mallard executables, reference data) use the system's open and save panels. Dropping a project (a directory or an input file) on the window opens it, asking about unsaved edits first;
dropping a mesh file makes it the case's mesh. Quit, the window's close button and Quit from the Dock
ask the page first. Mallard > Settings… (Cmd ,) opens the Settings page. Open Recent lists the `recent` of the
GUI's `settings.toml` (the shell points the server at it with `$MALLARD_GUI_SETTINGS`; an older shell's
`recent-projects` file is moved into it once).

In the app the page behaves as a native window: the system font, arrow cursors on controls, no text
selection on the chrome (text you read or edit stays selectable), no web context menu or Reload, Print and
Find keys, and no rubber-band scrolling of the window.

The shell gives the GUI's page one bridge, `window.mallardShell` (absent in a browser, so check for
it), allowed for the GUI's own origin and no other page:

```js
window.mallardShell?.notify({ title: "Run failed", body: "cyl-re100: NaN at step 12", level: "error" })
```

posts a native notification from the Mallard app (its name, icon and bundle identifier
`io.github.matthewbonanni.mallard`); `level` is `info`, `warning` or `error` (the last two make a
sound), and the promise rejects on anything else. Version 2 adds the document: `setDocument({title, path,
edited, canUndo, canRedo, mesh})` (the window's title, macOS's edited dot and proxy icon, the menu's
state), `openProject(path)`, `closeProject()` and `quit()` (called once the page has saved or let go of its
edits), `reveal(path)` and `openUrl(url)` (https only). Version 3 replaces version 2's `pickOpen`,
`pickSave` and `pickNewProject` with one `choosePath({mode, title, directory, name, filters})` (`mode` is
`open` or `save`, `filters` is `[{name, extensions}]`), resolving to a path or `null`; the page still uses
version 2's dialogs with an older app. Tests never post real notifications:
`MALLARD_DESKTOP_NOTIFICATIONS=FILE` makes the shell append each one to FILE as a JSON line instead,
and `MALLARD_DESKTOP_DATA=DIR` keeps the shell's memory (the last project opened) and the settings it gives the
server (`DIR/settings.toml`) in DIR instead of the user's own.
macOS asks the user once to allow notifications.

What goes wrong in the page is written to `page.log` beside `settings.toml`: `~/.config/mallard-gui/page.log`
(or `$MALLARD_GUI_CONFIG_DIR`, or `$MALLARD_DESKTOP_DATA` when the app is started with one), one JSON line per
event, the previous megabyte kept as `page.log.1`. The page reports uncaught errors and rejected promises,
errors vtk.js prints, lost and restored WebGL contexts, and the case view's mode (browser-drawn or server
images) whenever it changes; the server adds its versions at start and its own mode switches. The desktop
app has no console, so this file is the evidence to attach to a report of a view that stopped drawing.

#### Install the desktop app

**Download it** from [Releases](https://github.com/MatthewBonanni/mallard-gui/releases). Each bundle carries
its own Python and server, so it needs neither uv nor mallard-gui; you need Mallard itself too (see
[Install](#install)), and Settings > Mallard can point at it. Check a download with `sha256sum -c SHA256SUMS`
(`shasum -a 256 -c SHA256SUMS` on macOS).

| System | File | Install |
|---|---|---|
| macOS (Apple Silicon) | `Mallard-<version>-macos-arm64.dmg` or `.zip` | Move `Mallard.app` to Applications |
| Linux (x86-64) | `Mallard-<version>-linux-x86_64.AppImage` | `chmod +x` it and run it |
| Linux (x86-64), Debian or Ubuntu | `Mallard-<version>-linux-x86_64.deb` | `sudo apt install ./Mallard-<version>-linux-x86_64.deb` |
| Windows (x86-64) | `Mallard-<version>-windows-x86_64-setup.exe` | Run the installer |

None is signed by a paid certificate (an Apple Developer ID with notarization, or a Windows code-signing
certificate), which Mallard does not have. The macOS app is ad-hoc signed (Apple Silicon runs no unsigned
code at all), so its first open says Apple could not verify it is free of malware; open it once by any of:

- System Settings > Privacy & Security, Open Anyway beside the message about Mallard, then Open;
- right-click (Control-click) `Mallard.app` > Open, then Open (macOS 14 and older);
- `xattr -dr com.apple.quarantine /Applications/Mallard.app` in Terminal.

On Windows, SmartScreen says it protected your PC: More info, then Run anyway. Later opens need nothing.
Mallard itself runs on Linux and macOS only.

**Build it from source** ([uv](https://docs.astral.sh/uv/getting-started/installation/), Rust and the Tauri CLI;
on Linux also [Tauri's system packages](https://v2.tauri.app/start/prerequisites/)):

```bash
cargo install tauri-cli --version "^2"
git clone https://github.com/MatthewBonanni/mallard-gui && cd mallard-gui
uv run --group bundle desktop/package.py   # desktop/dist: this system's bundles and SHA256SUMS
```

`package.py` freezes the server with PyInstaller (`desktop/sidecar/mallard-gui-server.spec`: a `--onedir`
build of `mallard_gui` with `trame*`, the VTK modules it imports and `mallard_gui/assets`), builds the shell
and puts the server in the app's resources as `mallard-gui-server/`, where the shell looks for it. On macOS
it copies the server into `Mallard.app/Contents/Resources/` by `ditto` rather than Tauri's `bundle.resources`,
which copies its symlinks as files (twice the size, and a `Python.framework` codesign rejects), then ad-hoc
signs the bundle (`codesign --force --deep -s -`) and makes the zip and dmg. On Linux and Windows it passes
the server to Tauri's `bundle.resources` (`--config`), which builds the AppImage and deb, or the NSIS
installer. A build on your own machine is not quarantined, so it opens without the warning.

The release workflow (`.github/workflows/desktop-release.yml`) runs `package.py` on macOS (arm64), Linux
(Ubuntu 22.04, x86-64) and Windows runners for a pushed `v*` tag, or by hand for an existing tag, and
attaches the five bundles and `SHA256SUMS` to that tag's release; it fails if the tag is not
`v<version>` of `tauri.conf.json` or a bundle is missing.

For a shell that uses the `mallard-gui` you installed (no frozen server), build only the shell:

```bash
cd desktop/src-tauri && cargo tauri build --bundles app   # target/release/bundle/macos/Mallard.app
```

(`--bundles deb` or `nsis` elsewhere), and `cargo tauri dev` runs it from the checkout.

If you have a Developer ID: sign with `codesign --force --options runtime -s "Developer ID Application: …"`
in place of `-s -` (the server's libraries first, then the app; the frozen Python needs the
`com.apple.security.cs.disable-library-validation` entitlement under the hardened runtime), notarize the zip
with `xcrun notarytool submit --wait`, and `xcrun stapler staple Mallard.app`; `spctl -a -vv Mallard.app`
then accepts it and the warning above goes away. This path is not tested.

## Use

```bash
mallard-gui path/to/case            # a directory with the case's input.toml
mallard-gui case --input other.toml --mallard ~/mallard/build/bin/mallard --threads 2
```

The UI is served on `127.0.0.1` only, on a random port, and the browser opens at a URL carrying a
per-session token: other users of a shared machine can reach localhost ports, and the token keeps them
out. `--no-browser` prints the URL instead.

The first run of a case writes `mallard-project.toml` beside it. Runs go to `runs/<input>-N/` with a record
in `mallard-runs/`; the run directory's `input.toml` is the case's input with `run.control_file_interval`
turned on (so Stop and Checkpoint work) and paths to existing files made absolute.

The run monitor plots the probe, force and reduction CSV files the input names, followed by byte offset
with every sample (the run log carries a few): time series per column and point, and a boundary probe's
latest sample along the boundary. Its four plots are the Analysis page's interactive plots, live: each
follows the latest sample while the run goes; wheel to zoom (shift for y), drag a box, pan, log axes and a
crosshair readout; a zoom or pan pauses following (the plot shows paused) and reads its window again,
finer, from the run's files by byte range, once; a double click, fit or follow resumes it. A residual starts on
a log axis. A stopped run (stopped, signal or wall time limit) can Continue in its
own directory from its last restart, once that file's SHA-256 matches the run log's record: the run log,
the CSV files and the snapshot series are appended to, each part led by its own start record. Restart from
starts a new run of the case's input from another run's last restart.

### Settings

The Settings page (the rail's last button; Cmd/Ctrl , in a browser too) edits `settings.toml` in the user
config directory (`~/.config/mallard-gui/`; `$MALLARD_GUI_SETTINGS` names another file), each change
checked and saved at once:

- **Appearance**: System (follows the system's light or dark, live), Light or Dark; the topbar's theme
  button sets it too.
- **Units**: the unit an empty field starts in, per quantity (length, time, temperature, pressure, density,
  velocity, viscosity, mass flow); a catalog default is shown converted to it. A value keeps its own unit.
- **Runs**: threads of local mallard commands and runs (`--threads` overrides), and the remote the Data
  page proposes as a destination.
- **Notifications**: Notify for runs without their own setting, the least severe alert notified (warnings
  or critical only), and whether a run's end notifies.
- **Mallard**: the front end (`--mallard` overrides), and each dimension's own executable, typed or picked
  with Choose… (the system's open panel in the desktop app, else the server's file picker listing
  executables). The case's
  dimension is the front end's rule: its mesh file's (`mallard info MESH --format json`), or for a generated
  mesh 3D when `[mesh]` sets `Nz` or `Lz`. A case whose dimension has an executable set runs, checks and
  lists keys with it; others use the front end. The page says which one this case runs.
- **Recent projects**, with Clear.

Unknown keys are kept, and a file that does not parse is never overwritten (the GUI runs on the defaults
and says why).

### Documents

The case's input is a document: edits change it in memory, and the title shows `• input.toml` (in the
desktop app, the close button's dot) until Save (Cmd/Ctrl S) writes it, through a temporary file renamed
over the input so a crash leaves the old file or the new one, comments and layout kept. Undo and Redo
(Cmd/Ctrl Z, Shift Cmd/Ctrl Z; in a text field, the field's own) step through edits; Revert to Saved is one
of them. Save As writes another input: in the project it becomes one of its inputs, elsewhere the project's
files (not its runs) can be copied beside it so its relative paths hold, and the GUI opens it there. Open
(Cmd/Ctrl O), Open Recent, New Project, Close Project and Quit ask Save / Don't Save / Cancel when there are
unsaved edits, and so does a run: a run starts from the input on disk, never from edits it does not have.

While there are unsaved edits, `mallard check` reads them from `.input.unsaved.toml` beside the input
(where the input's relative paths resolve the same), which is also the recovery file: a session that ends
without saving leaves it, and the next one opens with those edits restored, unsaved, Undo going back to
the file. Don't Save removes it.

When another program (an editor, `git checkout`) changes the open input, the GUI sees it within a second:
without unsaved edits it reloads, as an undo step; with them it asks Reload, Keep Mine (your version stays
unsaved, and Save writes it over theirs) or Compare (a diff of your unsaved version against the file).

Every command is in the command palette (Save, Save As, Revert to Saved, Undo, Redo, Open, Open Recent, New
Project, Close Project, Reveal in Finder, the documentation). In a browser, Open, Save As and New Project
pick paths in the page's own file browser (the server's file system); another project opens in a new
`mallard-gui` the page moves to, and this one stops. A browser asks before leaving a page with unsaved
edits. `mallard-gui DIR --new` makes DIR a new project with a starting `input.toml`, and `mallard-gui
path/to/case.toml` opens that input.

### Case setup

Every section of the key catalog has a page in the case tree, its keys from the case's own catalog where
it has them and from the general catalog (`mallard list keys` without an input) otherwise; keys the catalog
does not describe are under OTHER KEYS. Quantities take a number and a unit: typing `1 cm` fills the value
and sets the unit selector, changing the unit converts the value exactly, and the input stores Mallard's
quantity string (`"1 cm"`), or a plain number in SI. A wrong unit is an error under its field. Nondimensional
cases show no units.

The case view previews regions and probes with drag handles (the document changes on release; a drag on the
selected shape's body moves it by its center, and in 2D the cells it holds follow the drag, Mallard's own
once the check after the release returns), a boundary's
`origin` and `toward` picked on the view (with where its u starts and which way it grows, from Mallard's
frames), and inflow velocity arrows on the selected boundary or every inflow: Mallard's own values at each face
(`case.boundary_values`; a mass-flow or stagnation inlet's estimate labeled as one), with a density control, a
cividis legend with its scale and units, and the section tool's clip. The Files tab lists the input and every
file its setup read, missing ones flagged. The command
palette (Cmd/Ctrl K) searches keys, case nodes and actions.

The browser draws the case view (vtk.js) while its surfaces fit 2 M triangles and no clip is set: orbit,
pan and zoom never reach the GUI process, at the screen's pixel density with one-pixel edges. Feature edges
(boundaries, creases) are always drawn; Show mesh adds every face's edges. Larger cases and clipped views
are rendered by the GUI process as images. Labels and drag handles sit over the view either way, dragged in
the page; the wheel and trackpad zoom by the distance scrolled, toward the cursor, and pinches too.

`mallard check` reruns after edits (debounced, never on view changes). The Check report tab arranges all of
it: every diagnostic in full with its docs, then time step, cost, memory, sizing, resolution, mesh quality (the
worst cells of a metric over its limit), boundaries (zones without a condition, periodic pairs), regions (cells,
measure, exact or extracted), environment and the report's version; a test keeps every part of the report shown
somewhere. Each diagnostic links to its key as the report gives it (a full indexed path such as
`write_data[1].time_interval`), which attaches it to that field and case-tree node, and its line and column open
the text tab with the caret there; every error of an input reported at once and the setup's warnings are listed
together. Builds before structured diagnostics get the keys their text names instead (`run.cfl`, `[les]`,
`[run] t_wall_stop`, among the catalog's keys), with setup warnings read from stderr. Each row
links to the key or node it concerns (dt to `run.cfl` or `run.dt`, steps to `run.t_stop`, a zone, a region), and
a row with a place (the cell limiting dt, a worst cell, the largest y+) marks it on the case view and frames it. Run opens a
launch dialog with ranks, memory target and a Measure button that times a few steps for the cost (wall time,
rank- or GPU-hours); once measured, the Run button carries the cost. The Control tab of a running local run
changes settable keys live, through `mallard ctl set` when the run has a control socket and the `SET` file
otherwise (when the build supports it).

### Run health

The run monitor's Health strip lists a run's alerts as they happen. Mallard's own records come first: its
warnings by code (`W-HEALTH-MARGIN` and the others after setup), the divergence report with its most likely
cause and fix (one alert, with the `diverged` event and `E-DIVERGED` folded in), and the end record. The GUI
then checks the run log's numbers: a NaN or Inf (a value turning `null`, or a non-finite value in a probe,
force or reduction file), the CFL number above the time integrator's limit from the check at setup or spiking
above twice its recent median, `dt` dropping below a tenth of its recent median, the run log stalled (no
progress record for ten times the usual interval, at least two minutes) while the job runs, a `max_*` monitor
or a conservation-ledger residual growing a hundredfold over six rows, and the wall-time limit
(`[run] t_wall_stop`) at 90 % or past the ETA. A GUI alert whose condition clears for five rows is announced
again if it returns.

New alerts show as toasts and, with Notify on, as desktop notifications shown by the page: in the desktop
app through the shell's `window.mallardShell.notify({title, body})` bridge (Tauri's notification plugin, with
the app's name and icon), in a browser through the Web Notifications API with the duck icon, after the
browser's permission prompt the first time Notify is turned on. With neither, alerts are toasts only. The
server itself never posts a notification. The per-run auto-action (Off, on a critical alert, on any alert)
stops the run with a restart through its `STOP` control file, once, while this GUI runs. The settings are
kept per run in `~/.cache/mallard-gui/health.toml`; a new run takes the last ones set in its project.

### Export

Every plot of the run monitor, the results viewer (probe and line over time) and the compare page has CSV
and SVG buttons. CSV is the plot's data, or the whole probe, force or reduction file it shows (every sample
and point, with its units line). SVG is a light and a dark file in the style of Mallard's documentation
figures (`tools/docs/figstyle.py`: transparent, real text, the docs' palettes). In the results viewer,
Export > animation writes the view over every snapshot (at most 240) as a GIF, or as an MP4 when `ffmpeg` is
on PATH, with the time and a colorbar drawn on each frame. Open in ParaView writes a `.pvsm` state of the
series (its `.pvd`, or its snapshots) with the view's time, field and component, colormap and range, camera
and section plane through ParaView's own `pvpython` and opens it (`$MALLARD_GUI_PARAVIEW`, else `paraview` on PATH, else the newest `/Applications/ParaView-*.app`);
without ParaView it writes a Python state to load by hand. Files go to `exports/<run>/` in the project.

### Analysis

The Analysis page works on a run's own files: its run log and its probe and force CSV files, read by byte
range, so a zoom or an export reaches every sample however long the run.

- Histories: residuals, CFL, dt, forces and probe columns overlaid with their units; wheel to zoom (shift
  for y), drag a box, double-click to fit, log axes, a crosshair readout and a running mean. CSV exports
  every sample in the view from the files.
- Signal: one probe or force column over a window (after the transient, by Mallard's MSER-5, or any span
  dragged on the plot): mean and RMS with confidence intervals from batch means, the integrated
  autocorrelation time and whether the mean has converged; the Welch spectrum (window, overlap, segment
  length) with its peak and Strouhal number; the autocorrelation.
- Forces: Cd, Cl (and Cs in 3D), pressure and viscous drag from the force files, against reference density,
  velocity, length, area and angle (defaults from `[output].reference`), each a number and a unit box as in
  the case's fields: typing `40 cm/s` sets both, changing the box converts the number exactly; mean, interval,
  fluctuation, extremes and the lift's Strouhal number over the window.
- Lines and walls: `mallard post extract` samples a line between two points, or a wall's faces (Cp, Cf,
  y+ against arc length), from a restart or whole-mesh snapshot; the plot's axes take any unit, or x / L.

### Compare runs

The Compare page puts two runs' inputs side by side as Mallard resolved them (from each run log's start
record, so a default that changed between builds shows too), with arrays of tables lined up by their `name`,
`zone` or `prefix`, and the keys that differ highlighted. Its overlay plots one probe, force or reduction
column of both runs, with reference or experimental data from a CSV file (first column x, a header row, `#`
comments) drawn as points.

## Remotes

The Remotes page edits `~/.config/mallard-gui/remotes.toml`: each remote by name, its host (an alias
of `~/.ssh/config`; keys, agents and MFA stay with OpenSSH), backend, run root and job defaults. Test
connection opens one ControlMaster connection, times a round trip and probes the login node in one
script: system and glibc, scheduler and partitions, modules, Apptainer and Spack, and Mallard installs
on PATH, in modules and under `~/mallard/*/` (each asked for `--version`); a found install is recorded
with Use. The queue lists your jobs (`squeue --me`, `qstat -u $USER`, `bjobs`, or the GUI's Jobs in the
namespace).

Backends: `slurm`, `pbs` (`qsub`, a `run.pbs` script with `select`/`walltime`/`-q`/`-A`) and `lsf`
(`bsub < run.lsf`, `-n`/`-W`/`-q`/`-P`) over SSH, and `k8s`. A Kubernetes remote's host is a context of
`~/.kube/config` and its install is a Mallard image; its settings are `[remotes.<name>.k8s]`:

```toml
[remotes.lab]
host = "lab"                  # kube context
backend = "k8s"
installs = [{ path = "ghcr.io/matthewbonanni/mallard:0.10.0-cpu-amd64" }]
storage = { run_root = "/data/runs" }
defaults = { cpu = "4", memory = "8Gi", gpu = "0", time = "02:00:00" }

[remotes.lab.k8s]
namespace = "me"
pvc = "mallard-data"            # the volume runs and copies live on
mount = "/data"                 # where it is mounted in every pod
prefix = "mallard-gui"          # pod and Job name prefix
node_selector = "nvidia.com/gpu.product=NVIDIA-A100-SXM4-80GB"
run_as_user = "1000"            # where the namespace's policy needs an explicit UID
idle = "120"                    # seconds the helper pod outlives its last use
```

Every run is a Job (no retries, kept a week) running the image's `mallard` on the volume, under `mpirun`
for more than one rank. Files, logs, control files and transfers go through a small helper pod that mounts
the volume, reached with `kubectl exec` and deleted when idle or when the GUI exits. Nothing listens
anywhere but on localhost. Test connection checks the context, the claim and what you may create
(`auth can-i`), and lists the GPU types of the nodes.

## Installs

Install Mallard on a remote in one click, into `<install root>/mallard/<version>-<variant>/` (the install
root defaults to your home). What to install comes from the release's `release-manifest.json`, in this
order: the relocatable archive for the login node's architecture and variant (`cpu`, `cuda12`, `cuda13`,
`rocm`), which the login node downloads and checks against the SHA-256 the manifest names (it needs glibc
at least as new as the archive's, and runs with the site's MPICH-compatible MPI module); the release image
through Apptainer, pulled by digest, for single-node runs; or a Spack build with the site's compilers and
MPI as externals, submitted as a batch job (slow; for sites that need their own MPI). Install shows what
will run, where, and how it is checked, and nothing runs until you confirm. Each install lands as
`DIR.partial` and is renamed only once it works, with an `install.toml` saying what it is; an existing
install is never overwritten. (A Kubernetes remote's install is the image it names.)

Every install is then verified by `mallard install verify --format json` as a short job (`srun` on the
remote's viewer or job partition): the environment check and a quick template run per dimension. The
verdict is recorded with the install in `remotes.toml`; Verify reruns it. A Mallard older than 0.12 has no
`install verify`, and gets the environment check alone.

Versions live side by side and the first recorded install is the remote's default; nothing on the remote
points at "current". Upgrade installs the version you name beside the default, verifies it, and makes it
the default only if it passes; the old install stays, so runs that used it can continue on it. Removing an
install the GUI made deletes its directory, after a confirmation; Forget only drops a recorded install from
the list. `$MALLARD_GUI_RELEASES` points the GUI at a mirror of the releases (or a local directory, as
the tests do).

## Data

The Data page lists a run's files with their state at every location: `home` (where it ran), `present`,
`prefix` (a shorter copy of a growing stream), `partial` (an interrupted transfer, resumable) or `absent`.
Files are known by their content: objects (snapshots, restarts, inputs) by SHA-256, from the run log's
output records or hashed where they are; streams (the run log, probe, force and integral CSV files) by
their length and the SHA-256 of that prefix. Selections are by what you think in: a snapshot, a time
range, probes and forces, the newest restart, the inputs, the whole run.

A transfer shows what moves, what is skipped because the destination holds it, and an estimate from the
throughput measured on that route. Objects land as `NAME.partial`, resume from its length (from any
location holding the same object) and are renamed only once their SHA-256 matches at the destination;
streams are appended after their prefix is checked. Routes: this machine and a remote over SSH (or
`kubectl exec`), remote to remote through this machine's memory (never its disk), directly between two SSH
remotes with the forwarded agent (the source pushes, or the destination pulls), or a copy in place when
both see the same storage. The first time two SSH remotes meet, a background probe tries `ssh` each way
and looks for a marker file of one in the other's storage; the result is kept in `remotes.toml` and redone
after a direct transfer fails.

Objects recorded with a chunk tree (`sha256_tree` with `chunk_bytes` and `chunks` in the run log) move in
chunks, eight at a time, each verified where it lands and retried once; a resumed copy moves only the chunks
that differ. The run log is read incrementally (new records only), a snapshot's rank pieces are one entry,
and histories of long runs are held as min/max/last bins at a few resolutions, so memory stays bounded and
a zoom past what is held rereads only its byte range.

Move or continue, beside the transfer: Check restart brings the run's newest restart to the destination
and has the Mallard there read it (`mallard info --format json`): format version, dimension, precision,
cells, fields, species and time must match what the run's home reads. Continue starts a new run of the
case there from that restart, its input files staged first. Move copies the whole run (the restart first,
checked), verifies every file and then changes the run's home, the only committed change; the old home's
files stay until you remove them from the listed paths.

Where copies are is per-machine knowledge, in `~/.cache/mallard-gui/index.sqlite` (with the transfers in
progress and the measured throughput); it is never committed, and is rebuilt from the locations on
Refresh. Copies of a run go to `runs/<run>` here and to `<run_root>/<run>` on a remote (`run_root` from the
project's `[remotes.<name>]`, else the remote's storage).

## Results

The Results page opens any snapshot of a run where the run is, without copying it here. A viewer process
reads the data and sends back rendered frames: for a local run, a process on this machine reading the run
directory in place; for a remote run, a viewer job next to the data (one per remote, reused for every run),
reached through the SSH master connection, its frames carried over the channel in memory. Series come from
the run log's output records (each writer's snapshots, else the `.pvd` collections); a `write_data` whose
`region` names a zone is a zone surface. The page has field, component, colormap (cividis, batlow, vik) and
range (per snapshot or fixed in time), time scrubbing, the case view's section tool (clip and slice planes,
x-ray), zone surfaces, and probes and lines over time picked on the view.

For a local run the browser draws the surfaces (geometry mode) when they fit the geometry budget; Images
and Geometry force one or the other. Saved views (camera, field, probe) go to the project file; a probe
over time shows its spectrum; a surface extract becomes a derived series. As images, the wheel zooms toward the cursor and the image
itself scales (and, for a pan, moves) at once, while the frame that shows the move is on its way; drags send one
move per displayed frame.

Large meshes stay within budgets: a snapshot's cell count is read from its header before its data, and a
volume over the cell budget (5 M) shows its zone surfaces instead; surfaces over the triangle budget are
decimated, a coarser level drawn while the view moves and full quality on release; snapshots read are kept in
a bounded cache, and probes over time sample at most 400 snapshots.

The viewer job runs `python -m mallard_gui.viewer --stdio` under `srun` on the remote and exits when the GUI
closes its input or after its idle timeout. Its settings are a remote's `[remotes.<name>.viewer]` in
`remotes.toml`: `python` (default `viewer/bin/python` beside the recorded install, a uv environment with
mallard-gui), `partition`, `account`, `time` (`01:00:00`), `cpus` (2), `idle` seconds (900) and `launch`
(`srun`, or `login` to run on the login node).

On a Kubernetes remote the viewer is a pod on the volume (`python -m mallard_gui.viewer --listen 8765`,
bound to 127.0.0.1 in the pod), reached through `kubectl port-forward` to a localhost port. The GUI uploads
its own package and a VTK wheel matching the pod's platform (resolved here with uv) to the volume once;
`[remotes.<name>.viewer]` sets `image` (default: the remote's image), `python` (`python3`), `libs` (a
`.tgz` of shared libraries added to `LD_LIBRARY_PATH`, e.g. OSMesa and LLVM where the image has no
offscreen GL), `opengl` (`osmesa` or `egl`), `cpus`, `memory` and `idle`.

## Development

```bash
uv sync
uv run ruff check && uv run ruff format --check
uv run mypy
uv run pytest                                  # tests marked `mallard` skip without a build
MALLARD=/path/to/mallard uv run pytest -m mallard
```

SSH is tested without a server: `tests/fixtures/fake-ssh` stands in for OpenSSH and runs the remote command
with this machine's shell (`$MALLARD_GUI_SSH` points the GUI at another client, the same way). Transfers,
interruptions and resumption run through it for real. Tests marked `ui` drive every page through Playwright
in Google Chrome, in WebKit (the desktop app's engine) and in WebKit styled as the desktop app, in both
themes, with a layout check (`tests/ui/alignment.js`): icons and labels share one vertical center, button
content is centered, one icon size, one button height per toolbar, no text cut off without an ellipsis, no
content spilling out of a control, and WCAG AA contrast; and check that keyboard focus is always visible and
controls answer hover. `python -m tests.ui.screenshots DIR` shoots every page, panel, dialog and state at
2880×1800 in both themes and engines, with crops of the chrome.

Design tokens (`src/mallard_gui/assets/tokens.json`) are a copy of the website's `design/tokens.json`;
`tokens.py` records the commit they came from. The UI font is Inter (OFL, bundled). The duck mark
(`assets/logo@2x.png`, `assets/favicon.png`) comes from the website's `content/assets/icon.png` and
`favicon.png` at MatthewBonanni/mallard-website@f4d9879a; the website has no vector source of it yet.

## Compatibility

The GUI reads the check report version 1 and run log versions 2-3, and says so when a build writes a newer
one.

### Mallard's session

The GUI keeps one `mallard session` per case (started with the case's input, so the front end picks its
dimension's solver; Kokkos at the GUI's thread count) and asks it what only Mallard knows. What a build
answers is read from `initialize`'s `methods`, never from its version; a build without `mallard session`, or
without a request, gets the GUI's older way:

| Request | What the GUI takes from it | A build without it |
|---------|----------------------------|--------------------|
| `initialize` | the keys a running run takes (with type, units and help, shown in the Control tab), the control files its runs poll (Stop and Checkpoint are enabled by them), the socket's commands, and the devices (in the Run dialog) | `check --help` and `ctl --help` |
| `units.catalog` | every unit, prefix and exact factor, and the units each dimension's selector offers | the bundled table in `units.py` |
| `case.boundary_values` | inflow arrows at every face as Mallard evaluates them (profiles, tables), mass-flow and stagnation inflow marked as estimates; asked after each check of a case with an inflow boundary, with the editor's unsaved text | the entry's own values; `u` expressions of x, y, z, t sampled here; estimates from the initial state |
| `case.view` frames | where each boundary's u starts and which way it grows, marked on the selected boundary (from the check's `view`, the same result) | the start points parsed from the check's text |
| check `key`, `line`, `column` (not a session request: the check report's diagnostics, detected by the fields' presence) | each diagnostic's key, line and column, setup warnings among the diagnostics | keys and lines parsed from the message text; setup warnings read from stderr |
| check `unassigned`, `periodic` | rows of the report's boundaries; zones without a condition as case-tree nodes to assign one, also when the setup stops on them; periodic node offsets, or "not measured" on a mesh read in parallel | no such rows |
| `file.info` | Continue is enabled by a restart's `continuable`, and the run page shows its `incompatibilities` | Continue for any stopped run with a restart |
| `case.files` | the case page's Files tab: the input and every file its setup read, missing ones flagged | the tab says the build does not list them |
| key-catalog `models` | the (i) button after a choice of models from the literature, opening the theory section of the one chosen on the website, and each model's source in the dropdown | no button |
| key-catalog `form` | arrays of tables, named entries and tables in the case tree and the key editor (the general catalog's form for tables the input does not set) | arrays guessed from types and `name`/`region` keys |
 Where Mallard does not yet provide something, the GUI works around it and a `TODO(#issue)` marks the
spot:

| Issue | Workaround until it lands |
|-------|---------------------------|
| #1311 | a partial input fails `list keys`: the last good catalog is kept |
| #1313 | case-view block names parsed for their input path; 3D cell zones not drawn |
| #1314 (PR #1331) | version parsed from `--version` |
| #1317 | manifests built from the run log, files listed and hashed by a POSIX shell script on the location |
| #1316 (PR #1334) | the run's input copy sets `run.control_file_interval` |
| #1256 | catalog defaults unquoted; section order and file roles built into the GUI; the files an input names, for staging |
| session: capabilities | GPU names `check --memory` takes and `check --initial` read from `check --help` (`initialize` does not list them) |
| #1318 | restart compatibility judged by comparing `mallard info` facts at home and at the destination |
| #1320 (PR #1339) | a Mallard without `install verify` gets the environment check alone (`check --format json`) |
| #1322, #1323 (PR #1345) | a release without `release-manifest.json`: archives named by convention, each checked against its `.sha256` |
| #1360, #1361 | a wall is extracted through a copy of the run's input with a boundary `[[probes]]` entry added (`extract/<name>.toml`) |
| #1362 | spectra, autocorrelation, window statistics and coefficients computed in the GUI (`signals.py`, Mallard's methods) |
| #1363 | a nondimensional run's CSV units lines say SI; the GUI shows them as written |
| #1321 | `uv tool install --with mallard-python` adds Mallard's executables; no `[solver]` extra until the wheels are published |
