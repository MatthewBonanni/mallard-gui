# Changelog

## 0.1.0 - 2026-11-23

Design system:

- A redesigned look, from one set of tokens (`tokens.py`: a type scale, a 4 px spacing grid, radii, elevation and
  a ladder of surfaces for each theme). The title bar, rail and status bar are one chrome framing panes that read
  as docked cards; the command search sits centered in the title bar; panel heads, document tabs (the open tab
  joined to its content), flat toolbars with ruled groups, and sentence-case headers replace the all-caps bands.
- The inspector's values are boxed fields, so what can be edited reads at a glance; a unit is a box attached to
  its number. Tables have sticky head bands and hairline rules; numbers use tabular figures.
- The Run monitor's figures are tiles and its plots cards on the canvas; plots draw left and bottom axes with
  sans tick labels; status pills carry a dot (finished is green). Dialogs have a title band, a footer action bar
  and a blurred scrim; view overlays (legend, colorbar, triad, inflow) are floating cards; the 3D views' background
  is the canvas color.
- One icon set: Lucide (ISC), credited in the About box.

Packaging:

- `desktop/package.py` (`uv run --group bundle desktop/package.py`) builds the self-contained desktop app on
  each system, with `SHA256SUMS`: macOS `Mallard-<version>-macos-<arch>.zip` and `.dmg` (ad-hoc signed), Linux
  `.AppImage` and `.deb`, Windows an NSIS `-setup.exe`. Each carries the server frozen with PyInstaller, so it runs
  without uv, Python or mallard-gui. There is no Developer ID signature, notarization or Windows code signing:
  the README says how to open it past macOS's and Windows' warnings, or how to build it yourself.
- The desktop shell finds `mallard-gui` and the frozen server on Windows too (`.exe`, `;`-separated `PATH`,
  `%USERPROFILE%`, settings under `%LOCALAPPDATA%`) and starts the server without a console window.
- `.github/workflows/desktop-release.yml` builds the bundles on macOS, Linux and Windows runners and attaches them
  to the release of a pushed `v*` tag (or of a tag given by hand); it never runs for branch pushes or PRs.
- `install.sh` is gone: install the desktop app from Releases, or the browser-mode server with
  `uv tool install git+https://github.com/MatthewBonanni/mallard-gui` (`--with mallard-python` for what
  `--solver` added), and Mallard by its own installation docs.

Layout:

- Every split between panes has a splitter: each page's left and right panes, the case page's Problems, the
  Run monitor's and Results' plots, and the lists in side panes. Drag it or focus it and use the arrow keys
  (Shift: larger steps, Home/End: the limits); double-click for the default size; side panes, Problems and
  plots hide to a strip (Enter, or the splitter's toggle) and come back from it. Sizes stay within limits,
  the views redraw at their new size with their cameras, a narrow window shrinks the side panes alike, and
  sizes are kept per page in settings.toml's `[layout]` (Settings > Appearance resets them).
- The rail's buttons name their page and its shortcut, and Cmd/Ctrl+1 to 7 go to the pages in the rail's order
  (also in the View menu); Cmd/Ctrl+, opens Settings.
- The case tree's notes end at one edge whatever a row ends with (a list's +, a problem badge, now beside the
  name); a list with no entries says "none" ("off" is a switched-off feature); the strip of feature chips above
  the tree is gone, the features being rows of it.
- The view toolbars' Fit (arrows to the corners, F) and Frame (the selection framed, Shift+F) have icons of their
  own and say their keys; F and Shift+F work in Results too.
- Inflow arrows are one toolbar switch, Inflow arrows: on, every inflow boundary at automatic density, one color
  when the speeds are equal, else colored by speed with a small |u| scale. The Inflow box, its density and its
  selected-or-all choice are gone.

Inspector:

- A choice of models from the literature (drag laws, SGS models, wall models, Riemann solvers, time integrators,
  transport, chemistry, radiation, thickened-flame efficiencies, ...) has an (i) button after its dropdown: its
  tooltip names the model chosen (else the default) and its source, and it opens the theory manual's section with
  the model's formulation on the Mallard website in the system browser. The dropdown lists each model's source
  beside it. Both come from the key catalog's `models`, which builds before it do not give.
- Every key with a unit has a unit selector: the keys and their dimensions come from the Units table of Mallard's
  input reference (`tools/unit_keys.py` regenerates `unit_keys.py`) where the key catalog says none, so 140 keys
  have one instead of 77. The input holds SI numbers, the solver's units: `2.5 min` or 2.5 with min selected
  writes 150; the field shows the unit last typed or chosen (or the Settings' preferred one), and choosing another
  converts what shows, not the input. A vector or a list of points has one selector for all its components.
  Angles (degrees), `chemistry.C_reg` (kmol/m^3) and `physics.sutherland_S` (a difference, K) show their fixed
  unit; a key whose unit depends on another (`statistics.phase.level`, a `[[sources]]` rate with `total`) says
  "unit?" with why, where the case's catalog does not resolve it.
- Lists of entries (sponges, sources, probes, triggers, periodic pairs) have no on/off switch: the list shows its
  entries, or "None yet" with Add. Single blocks keep theirs (chemistry, LES, radiation, statistics, and the
  uniform volume source, Body forces, beside the Sources list). A table's head counts "2 of 7 options set", which
  cannot read as a limit of entries.
- A list of points (`[particles] points`, a patch's `corners`) is a row of x/y(/z) boxes per point, a blank row
  to add one and a button to remove each; nothing is written while a box is not a number.
- Per-direction counts (`[particles.lattice] n`) are one box per direction that takes integers; the particles'
  `velocity` is u/v(/w) of numbers; `lattice.lo`, `hi`, `gravity`, a winding's `point` and `axis`, `periodic.axis`,
  `mesh.transform.rotate.axis` and `statistics.phase.probe` are vectors.
- Vectors (points, directions, velocities, gravity) are one box per component, x/y(/z) or u/v(/w), two in a 2D
  case and three in 3D, instead of a TOML array to type. Components take expressions where the key does
  (velocities, momentum sources, inlet direction); a component that is not valid is marked at its box, with what
  is wrong under the row, and nothing is written until all are. A default shows in each box as its placeholder.
- Optional features (chemistry, LES, radiation, particles, statistics, body forces, sponges, sources, triggers,
  probes, periodicity, wall model, synthetic turbulence) are off unless the input has their table, whatever their
  keys' defaults: a switch on the section's head turns each on or off, an off feature shows what it does and a
  Turn on button in place of its keys (chemistry asks for the mechanism first), and turning off a feature with
  settings asks first and can be undone. `[chemistry] enabled` is driven by the switch rather than listed. The
  case tree marks each feature on or off, with a strip of all of them above it.
- Each section has its own icon: a flame for chemistry, swirls for LES, a sun for radiation, and others for
  sponges, sources, triggers, parallel, particles and periodicity.
- Switching presets names what each choice does: "Use les, keep my 3 changes" (the default) or "Use les, replace
  my 3 changes", listing your changes with your value and the preset's; Undo reverts the switch.
- A field list (`[[write_data]] fields`) is a picker, not a TOML text box: its entries are chips (× removes one)
  and Choose opens the case's own fields from `mallard list fields`, in Mallard's groups (conservative,
  primitive, species, derived, statistics, mesh, and the surface fields a region of faces writes), each with its
  description and unit, filtered as you type, with a box per group that selects all it shows. An entry the case
  does not write is red, with why (`MU_T needs [les]`, `FOO is not a field Mallard writes`). Apply writes the
  array exactly as picked; shorthands (`U`, `PHASE_*`) stay as written until a name they cover is cleared.
- Keys Mallard takes one of (`dt` or `cfl`, `interval` or `time_interval`, a probe's geometry or `region`, ...) are
  a group each under a selector: the one in use open, the others collapsed as not used. Switching away from one
  that sets keys asks before clearing them; setting a key of an unused one offers to clear the other. The list is
  the GUI's until Mallard's catalog says which keys exclude each other.
- Every error message copies in full with one action: its copy button, its context menu or Cmd+C (Problems, the
  check report, the preset bar, toasts, banners, run alerts and failed jobs). Problems copies code, key, line,
  message, fix and docs link, several at once when selected with Shift or Cmd. Double-click or Enter on a problem
  opens it highlighted in the check report, whose line opens the text there.
- A typed table (a probe, boundary, region shape, mesh, initial state, ...) shows only the keys its type reads,
  as Mallard reports them for the input: a line probe no longer offers `radius`. Where the input has several kinds
  at one path, each kind's keys come from a scratch `list keys` per kind. A key the type does not read but the
  input sets stays, flagged, with Reset.
- An unset key with no default value reads what leaving it out means, from the catalog's `unset` (Mallard
  #1400): "not set: no wall-clock limit" for `run.t_wall_stop`, in its field and its key help. Catalogs before
  #1400 read "not set: automatic" where the description says Mallard picks the value, and "not set: disabled"
  for the -1 sentinel.
- Every section lists every key its catalog accepts, set or not: type, units, allowed values and the default (per
  dimension, "3D only" tagged) as the unset key's placeholder, with the catalog entry shown on hover. Editing an
  unset key writes it into its table (creating the table, keeping comments); Reset removes it. A section with no
  catalog says why.
- Nested tables and arrays of tables show in place, each entry with Duplicate and Delete (Undo); every repeatable
  entry type gets Add, in the case tree and the inspector, with a type menu where the entry has a `type`, writing
  a minimal entry, selecting it and revealing it in input.toml.
- Presets (`numerics.preset` and any other catalog `preset` key) show the preset in effect, what each one
  chooses, the keys it fills ("preset") and the keys set over it ("override", one-click Revert), all from
  `mallard input resolve` and `mallard check`; switching lists what changes, keeping or dropping the overrides.
- A key is starred as required only where the case must set it: the case's own catalog decides for the tables
  it reads (the general catalog's `required` holds only where a key is read at all, so `initialize.file` is no
  longer starred in a case that does not restart), and the general catalog only for an entry just added.
- An entry's own page (a `[[triggers]]` entry just added, a named region) has Duplicate and Delete with Undo, as
  its link in the section does.
- When the input does not set up, the preset bar shows the error Mallard stopped on, named for the input
  (`E-INPUT: input.toml: line 156: triggers[0].when: missing; ...`), not "could not set this case up"; so does
  the preview of a switch.
- One searchable dropdown for every choice among values in the inspector (allowed values, presets, units, a
  zone's condition, setup actions): opening it focuses its search field, so typing filters at once; arrow keys
  move, Enter picks, Escape closes.
- The preset bar is the selector, its status, the per-key tags with Revert and the switch preview; what each
  preset chooses shows beside it in the dropdown, not as a list under it.
- Every boolean key is a checkbox; unset, it shows its default (or the preset's value) dimmed, and Reset returns
  a set one to it.
- A key naming a file (the mesh, a restart, a flame table, a profile, a probe file) has a button for the file
  dialog (the desktop's native panel, else the server's picker listing every file), starting where the file is;
  a choice inside the case's directory is stored relative to it.
- A key naming a zone, a region or a species is a dropdown of the case's zones (typing one the check does not know
  yet still works), its regions, or its mechanism's species (`mallard list species`).
- A table's X and Y are one composition editor: a Mole (X) / Mass (Y) toggle (Mallard takes exactly one), each
  species from a searchable list of the mechanism's species with its fraction, Add species, the sum and
  Normalize; species the mechanism lacks and X with Y are flagged. Switching X and Y converts with the species'
  molar masses where Mallard gives them, and otherwise asks before clearing the values.
- A key the case's dimension does not read (`write_data.boundary` in 2D: the catalog's dims, unless the case's own
  catalog reads it) is not offered: it is listed read-only in a folded "not read in 2D" group, and set, it is
  flagged with Reset. Add does not offer such entries.
- One disclosure control for every expand/collapse in the app (the case tree's 16px chevron), and the
  inspector never clips labels, units or tags down to its narrowest width.

Mallard's session:

- The GUI asks a resident `mallard session` per case what only Mallard knows, detecting each request from
  `initialize`'s methods; builds without one keep the GUI's older way.
- Inflow arrows come from `case.boundary_values`: Mallard's own values at every face (profiles and tables
  included), mass-flow and stagnation inflow marked as estimates, only where flow enters; the selected
  boundary marks where its u starts and which way it grows, from the case view's frames.
- Units, prefixes, exact factors and each dimension's unit choices come from `units.catalog` (angles too).
- Run controls follow `initialize`: Stop and Checkpoint by the control files the build's runs poll; the
  Control tab's keys with their units and help; the Run dialog shows the devices Mallard sees.
- Continue follows the restart's `file.info` `continuable`, and the run page shows why Mallard refuses it.
- A Files tab on the case page lists the input and every file its setup read (`case.files`), missing files
  flagged.
- The case tree and key editor take arrays of tables, named entries and tables from the catalog's `form`.

Check report:

- An edit sets the case up (reads and partitions its mesh) once, for its check, not three times: the key catalog
  comes from the check's own setup (`check --catalog`), the preset row is checked again only when the keys the
  input sets change, and a check the next edit makes stale is stopped. On a 110k-cell 3D case the check's
  answer after an edit came in 13 s instead of 25-32 s, the presets' in 12 s instead of 23-31 s. A check with no
  answer within the time limit says so instead of spinning.
- The Check report tab shows everything `mallard check` reports: every diagnostic in full with a docs link,
  boundaries (unassigned zones, periodic pairs), regions, the worst cells of each failing quality metric, memory
  only at the setup's peak, the cells near the time-step limit, solvers, dependencies, GPU-aware MPI and NCCL.
- Diagnostics link to the structured `key` the check gives (full indexed paths such as
  `write_data[1].time_interval`, mapped onto the case tree and the key editor), and their `line` and `column`
  open the text tab with the caret at that spot; builds before them keep the keys their text names (from the
  catalog) and the setup warnings read from stderr. Report rows link to their key or case-tree node; a location
  shows on the case view, framed, with Back to report.
- Every error of an input reported at once and the setup's warnings show in the problems panel together.
- `unassigned` zones are case-tree nodes with a condition to pick, also when the setup stops on them; periodic
  pairs read Mallard's `periodic` as written (rotation or translation, null members), and say when the node
  offsets were not measured (a mesh read in parallel) instead of reporting 0.
- The tree's collapse caret, the zone list's eye and a suggested key's fix act again (their `click.stop`
  handlers were never registered).

Analysis:

- Results' field is drawn as cell values (each cell flat in the value Mallard wrote) or interpolated:
  the Rendering choice on the Field panel that the view, slices, clips, Compare, geometry mode and saved views follow.
  Probes, lines and statistics always use the cell values. Built for a third choice, Mallard's high-order
  reconstruction, to come.
- Large snapshots open in seconds: a surface over the triangle budgets is coarsened by vertex clustering (a
  4M-cell 2D snapshot did not open in 10 minutes under quadric decimation, which logged GBs of warnings on flat
  surfaces); a cached snapshot keeps its surfaces, so going back to it redraws at once, and another field's
  statistics reuse the cells' measures.
- Particles in Results: the run's particle file (`[particles] file`) drawn over the flow as dots with a dark rim,
  colored by a per-particle quantity (its first one by default; the speed of solid particles, `T_p`, `id`) over
  its own range, with its own colorbar, or one color; a Particles list toggles them and says when the file is of
  another step than the snapshot shown. In 3D, X-ray or Clip shows those inside the volume.
- Results has the shown field's statistics for the snapshot: mean, standard deviation, RMS, min and max (each
  with where it is: a click probes over time there), the integral, and a histogram over the shown range in the
  colormap's colors. Each cell counts by its volume (area in 2D), so a refined region does not outweigh the
  rest; while a slice is on, the same over the slice (by area).
- Compare (results toolbar) splits the view in two: the second, beside it, has the same camera, clip, slice and
  snapshot, colored by another field (and component) over its own range, with its own colorbar. Both are images
  from the viewer while split, so they move together.
- Compare runs groups the keys that differ under a head per table (MESH, NUMERICS, BOUNDARIES ...), each with
  how many of its keys differ; a row names its key within the table, the full key on hover.
- ParaView's colormaps, from ParaView 6.2.1's own presets (control points and color space): Fast, ParaView's
  default since 6.0 and now the GUI's, interpolated in Lab; Cool to Warm (diverging space), Viridis, Inferno,
  Plasma, Cividis, Turbo, Jet, Rainbow Uniform, Grayscale, Black-Body Radiation, Blue Orange (divergent) and
  Rainbow Desaturated. The picker is a dropdown with a swatch for each. The view, saved images and animations, the
  ParaView state and the parallel viewer draw the same points in the same space (VTK's interpolation, as
  ParaView's), whatever presets the ParaView at hand has. batlow and vik, which ParaView does not have, are gone: a
  saved view naming one opens with Fast or Cool to Warm.
- Initial state in the case tree shows the initial field on the case view at once: the Analyze panel on its
  Initial state tab, the field (the first of U, P, T and RHO that varies), a colormap and a range to choose,
  the seconds counted while Mallard sets a large case up; held until the input changes; gone when another node
  is chosen. Mallard writes it without running the case: `check --initial` where the build has it, else a run
  of one step whose only output is the snapshot at step 0 (the 0.10 builds, whose Initial state tab was empty).
  Field colors are drawn unlit: the browser lit a flat field to a pale wash.
- A signal clicked right after a tab is that tab's: before the page had drawn the Signal tab, the click was
  still the Histories tab's (it overlaid the signal there, and the spectrum never came).
- An Analysis page: histories overlaid with zoom, pan, log axes, a crosshair readout and a running mean, read
  by byte range so a zoom or a CSV export reaches every sample; a probe's Welch spectrum with its Strouhal
  number, autocorrelation, and mean and RMS with confidence intervals and a convergence verdict; force
  coefficients against reference quantities in units; line and wall distributions extracted by
  `mallard post extract`, with unit-aware axes.
- The run monitor's four plots are interactive and live: they follow the latest samples, a zoom or pan
  pauses following and reads its window by byte range once, a fit resumes it; wheel and box zoom, pan, log
  axes and a crosshair readout as on the Analysis page. Log axes under two decades get 2 and 5 ticks.
- Open in ParaView carries the view: time, field and component, colormap range, camera and section plane.

Polish:

- The Regions panel lists every region of the case tree, those of probes and statistics included, not "None yet".
- A nondimensional run's Results plots label time, length and frequency without SI units.
- Mallard's uppercase output names (particle and probe columns, forces, monitors, case-view arrays) are read, and
  the lowercase names of older Mallard still are.
- The case view's state (the 2D lock, a preset) reaches the page before the camera or scene it goes with: on a
  2D case the view no longer turns before the lock applies.
- The inspector's Cancel (switching a composition's basis, one alternative for another, a preset) is answered by
  the server, so asking again at once always asks.

- The run dialog: hovering a sizing plot reads a whole rank count, one of those checked, never "1.444 ranks";
  the first plot is "wall time and total compute hours", its hover note saying it is an ideal-scaling bound, and
  halo per rank is secondary. Measure with a GPU target this machine lacks says "No NVIDIA A100 80 GB available:
  set up a remote with one to measure" and times nothing here.
- The run dialog's wall time and compute hours follow Mallard's parallel-efficiency model where the check gives
  it (`cost.scaling`, Mallard #1440): the efficiency at each rank count, the wall time's uncertainty as a band and
  in the headline (±), and the model's basis under the plot's title. A Mallard without it keeps the ideal-scaling
  bound, which the subtitle says.
- The run dialog's throughput estimate, before any run of the project, is the nearest of a table of measured
  runs (A100 and Apple M CPUs; 2D and 3D; MUSCL and TENO-E 3, 5, 6; Euler, Navier-Stokes, LES, reacting), its
  basis in one line; `tools/throughput_table.py` regenerates the table and records each entry's source.
  Its A100 entries come from one campaign over every physics (Euler, NS, LES, reacting), dimension and
  reconstruction; the entries measured on a shared workstation say they are indicative.
- A disabled primary button is neutral gray with muted text, not a faded accent that reads as a dim working
  button; Launch says what it waits for ("Measuring the throughput: warming up...", with a spinner, or what
  blocks it).
- The GUI's scripts and styles are served with `Cache-Control: no-cache`: the desktop app no longer draws a
  reinstalled GUI with the old ones it had cached.
- About Mallard GUI (the app menu's About, which said "Mallard" with the GUI's version) names the GUI and its
  version, and apart from it the solver: "Solver: mallard 0.10.x at <path>", or "not found".
- Choosing a quantity on a finished run's plot redraws that plot: it redrew the whole run, which put every plot
  back to its default, so the choice seemed to do nothing.
- The Runs page in a small window: the run's figures wrap to more columns instead of running into each other,
  each plot is titled by its own selector (no "Pr..." repeat) with both CSV and SVG in view, the run head's
  buttons turn to icons instead of falling off, and the plots shrink rather than draw over the status bar.
- The desktop app's About box says what Mallard is, its license and its site; a disabled Stop (a finished run) is
  grey instead of a faded red that read as live.
- A check of unsaved edits names the input (`input.toml: line 11: ...`) in Problems and the check report, not the
  hidden recovery file `mallard check` read them from (`.input.unsaved.toml`).
- Every select draws the same chevron as the inspector's (Settings, the monitor's plot pickers, Health, Compare,
  Export, the Inflow box) instead of WebKit's native stepper glyph beside the app's own.
- A small window keeps tab labels on one line (the tab bar scrolls instead), and the overlays at a view's bottom
  corners stay apart: the interaction hint wraps beside the case view's Inflow box and the results' colorbar.
  The results view no longer prints render statistics over the field: they are the Viewer section's "last frame"
  row, and the viewer's "where" reads "this machine, reading the run in place" without a process id.
- Empty pages say what belongs there and how to get it, centered, with the action that does (Go to Case setup):
  Runs, Results, Data, Analysis and Compare without runs or without a selected run, and their side panes, instead
  of empty forms, tables, disabled buttons and tabs with nothing in them. The run's Log, Control and Outputs tabs
  say why they are empty. The Runs page's "3D view" placeholder tab is gone (the Results page draws fields), a
  finished run shows no ETA, and Compare's breadcrumb no longer reads "Compare vs" with no runs picked.
- The status bar reads as words, in segments: `Mallard 0.10.0 · 2D · double · CPU (Threads)`, the cells, the
  check's verdict with a colored dot and when it ran (`1 warning  checked 10:15:27`, `Check passed`, `Checking…`)
  and `input.toml · saved` or `unsaved changes`.
- The Run dialog loses nothing at any window size: the settings and the check's rows they rest on are one column,
  the estimate's verdict, figures and plots over ranks the other, each scrolling on its own with a shade where
  more is beyond the edge. At a laptop's size and a small window's the verdict, every figure and the three plots
  are in view whole; before, the plots and the verdict were cut off under the dialog's buttons.
- A page opened after another one watched a run (a reload, a second browser window, the desktop app's window on a
  server a browser also shows) drew the case view and the results view blank: the run's events and the views'
  interactor events shared one state key, so a view mounted later subscribed to the run's events and failed.
- Open in ParaView is a labeled button on the results view's toolbar (and still in the Export rows, the File
  menu and the palette). ParaView is looked for while the page is shown and when the pointer comes to the
  button: with none found the button is off and says how to get one found, and it follows an install removed
  or replaced while the GUI runs. A toolbar wider than its view goes on to a second row instead of scrolling
  its last buttons out of sight.
- An orientation triad in the top right corner of the case view and of the results view: the x, y and z axes as
  the camera sees them, turning with the view (the browser's own camera where it draws, the camera of the last
  image otherwise), axes pointing away drawn faint, legible on both themes. No label or handle is drawn on it.
- Clip in the case view is drawn by the browser: it handed the view to the server, so every orbit, pan and zoom
  of a clipped case was a round trip. vtk.js clips the surfaces itself (the plane on each mapper), so the view
  moves as an unclipped one does and a moved plane sends no geometry. A 2D case opens with a plane that cuts it
  (normal x; the page's own z, with which Clip did nothing, is not taken), and the plane is marked in the view.
- The section's settings are a bar along the top of the view, under its toolbar, while Clip (or Slice, in the
  results view) is on: X, Y and Z presets, Flip, the plane's position on a slider across the scene with its
  value, whether the plane is drawn, and, under Numbers, its origin and normal; x-ray's opacity when X-ray is
  on. They were rows appended to the end of the right pane, in both views.
- The case and results views' orientation buttons are there for a 2D case too: ±Z face the plane from either
  side and keep the 2D lock; ±X, ±Y and Iso look at it from out of the plane, which lifts the lock (2D lock turns
  it back to the plane). The 2D lock follows the case when it changes between 2D and 3D: the view counted as 2D
  until its first load, so a case whose check gave no view had no buttons, and a case that became 3D kept a 2D
  lock.
- The parallel viewer on this machine outlives its ParaView: when its processes have died or their install is
  gone (ParaView replaced by another version while the GUI runs), the next request starts pvserver and the
  driver from the ParaView found now and gives them the run, theme, snapshot, field and camera the old ones
  had. ParaView is looked for each time: $MALLARD_GUI_PARAVIEW (its binary directory, an executable in it or
  the .app), pvserver on PATH, else the newest /Applications/ParaView-*.app by version.
- The camera button of the case view and of the results view saves the view as a PNG through the save dialog:
  the pixels the view is drawn with (its size times the screen's pixel ratio), in the theme shown, from the
  camera as it is; the results image has the time and the colorbar written on it. The button that frames the
  selected zone (which had the camera icon) has a frame icon.
- A view's canvas fills its view while it is resized: styled with a width alone, it kept the shape of the
  pixels it was last drawn with until vtk.js had sized them again, taller than the view it sat in (905 px in a
  640 px view after a splitter moved). A resized case view keeps its camera once it has been drawn.
- The results view's Clip and Slice: the browser-drawn view keeps the same side of the plane as the images
  (it kept the other); a clipped volume shows the field on its cut face instead of a hollow shell; a slice is
  seen, the volume's surface drawn faintly around it (it was hidden inside it), and in 2D it is a line in the
  accent color over the field; a 2D run opens with a plane that cuts it (normal x, not the page's own z, with
  which Clip and Slice did nothing); the plane is marked in the scene, its outline on the data's box and its
  normal toward the side kept; a run opens whole, whatever the last one showed; settings made in a row reach
  the viewer in that order.
- Text typed into a field stays there until it is entered: the page redraws whenever any state changes (a
  frame, a run's progress) and set every field back to its value, so an origin or a range typed meanwhile was
  replaced and never applied.
- A results view drawn in the browser no longer keeps the last scene's surfaces under the new one: trame-vtk
  drops what it has to say of a renderer left with nothing to draw (between two runs), the removals of its
  actors with it. The renderer is told to the browser all the same.
- A region dragged with its cells highlighted live is drawn whole over a slow link: scene updates sent faster
  than their arrays arrived were applied two at a time by the browser, which drew the points of one with the
  cells of another (edges and faces across the highlight). Updates are applied one after another, and the
  server sends the next scene, the latest, once the last is drawn.
- The case view's labels and handles are placed by vtk.js's own transform (the renderer's world-to-display,
  on the canvas's own box) instead of a projection written beside it, and a pin is hidden until the frame that
  places it: none can sit at the view's top left while the view is not built, has no scene, is hidden, or its
  point is out of view.
- 2D lock pans in the results view the browser draws too: there every drag orbited, the lock on or off, and a
  2D run (locked as it opens) turned edge on. Turning the lock on turns a 2D case or run back to its plane.
- A pan or zoom of the results image after the page was reloaded moves the picture once: frames named the
  server's count of moves, which outlives a page, so the image kept its preview's offset over a frame that
  already held the move and went twice as far as the pointer. A move is named by its page.
- Fit frames the case or the results from where the view looks: it keeps the orbit (direction and up) and only
  moves and zooms, in the case view (the button and F) and the results view, images or browser-drawn. It turned
  the view back to the opening one, and right after a zoom did nothing at all (the server had not heard of the
  zoom yet: Fit, the presets and Ortho now start from the camera the page sends with the click). Framing fits
  the box's corners to the view's own shape with a margin and leaves the view angle alone (2D framing narrowed
  it at every fit); Ortho keeps the size drawn; a view not yet moved by hand is framed again when resized.
- The case tree follows the setup of a case under the input reference's headings (Geometry, Physics,
  Conditions, Solver, Output, Execution); sections the input does not set stay in their group, dimmed and marked
  not set. Disclosure chevrons sit in a fixed gutter (a heading row toggles its group), and children start
  one step right of their section. `[check]` is no section: the check's status sits by the tree's title and
  opens the report.
- Check shows its report once the check is done, wherever the user went meanwhile, and says there when the
  check could not run.
- input.toml is colored by a tested TOML tokenizer (keys, headers, strings, numbers, booleans, dates, comments,
  inline tables and arrays) in either theme, under the plain text field: editing, undo, the caret and reloads
  are unchanged. A click in the case tree scrolls the text to that section, element or key and highlights it
  for a moment, also when the text tab is shown later; the caret's line selects its node.
- The Run dialog's memory choice is now "size for: target device" with the estimates: this machine as detected
  or a GPU by model and memory (`mallard check --memory`), with the verdict in words (what a rank needs, the
  fewest ranks it fits on, the ranks that keep the halo under 10 %); the detected devices stay listed.
- The Run dialog's throughput is preloaded, labeled "estimated" with its basis: this project's nearest earlier
  run's stepping throughput, else Mallard's performance guide. Measure now runs the case with no outputs and
  times only steady stepping from Mallard's own per-interval stepping times (warm-up intervals discarded),
  until the median is known to ±3 % or a time cap, cancellable with live progress; it shows "measured: X ± Y
  cells/s/rank (N steps)" and recosts the wall time at once (it was one `check --steps 20` sample, CV 33-49 %).
- The Run dialog has two titled parts: RUN, "used when you launch" (where, ranks, threads per rank, devices,
  and the Launch button, which says what it starts: "Launch on this machine, 4 ranks"), and ESTIMATE, "only
  affects the cost estimate" (size for, throughput and its basis, measure), with the check's results under it;
  ranks says it serves both.
- The estimate leads with large figures: max cells per rank on the target, ranks to fit, wall time at the chosen
  ranks, GPU- or rank-hours, memory per rank against the target's usable memory (a bar) and the halo per rank,
  each with its basis. Three plots over rank counts (wall time and hours, memory per rank with the usable line,
  halo per rank) come from `mallard check --ranks N` at each count, checked in the background; the chosen ranks
  and the fewest that fit are marked, hover reads the values, and a click on a point sets the run's ranks.
- Numbers group their digits with commas everywhere (2,097,152 cells), through one helper
  (`mallard_gui.grouping.grouped`), plot ticks included; values written to the input or typed back stay
  ungrouped.
- View presets turn the results view drawn in the browser too (geometry mode ignored them); a 2D case shows
  none (they were there, disabled).
- The label of a zone picked in the case view sits at the picked point and follows it (it sat at the view's
  top left, over the toolbar), and goes when the pointer leaves the view; the release of a pan or an orbit
  picks nothing; a region picked in the view keeps its handles.
- `page.log` beside settings.toml records what goes wrong in the page (uncaught errors, vtk.js errors, lost
  WebGL contexts, the case view's mode switches): the desktop app has no console.
- The case view keeps following the case after shapes are rebuilt: the browser's scene named VTK objects by
  address, so a new object at a freed one's address broke a scene update and left stale shapes drawn, the
  handles off them.
- Views follow the theme: lines and points (mesh, zones, region outlines, probes, previews, inflow arrows)
  are drawn unlit (the browser lit normal-less lines as if facing the light, so the light theme's mesh vanished
  on white and orange zones turned yellow) and reach 3:1 contrast on either background (the mesh a quieter
  1.6:1), Okabe-Ito yellow darkened on white; the arrows' cividis and its legend too; a theme switch recolors
  the overlays and the legend at once.
- The results view's images keep a zoom, pan or orbit made right after the view was framed: the half-size
  frames drawn while moving resized the window, which framed the view again and undid the move.
- A startup screen in place of trame's spinner and the desktop app's bare splash: the Mallard mark, the
  server's real stages (starting, loading the case, building the interface, drawing) with their times, in the
  system's theme and the design tokens, shown at once by the desktop app and kept by the GUI's page until its
  first themed frame, then faded out. A failed start says why, with the log's path and tail, Retry and Show
  Log. Reduced motion stops its animation.
- Desktop app: a project (directory or input) dropped on the window opens, a mesh file dropped on it becomes
  the case's mesh.
- The Regions section (and any section of named entries) opens when the input has none; the general
  catalog's `regions.<name>` placeholder stopped its properties with an error.
- File menu: Import Mesh… (Shift Cmd I) and Open in ParaView (Shift Cmd P), also in the command palette;
  Reveal in Finder is Show in Explorer or Show in File Manager on Windows and Linux.
- Smoother views: the results view's images zoom toward the cursor (they zoomed to the center) and preview
  each zoom and pan by scaling and moving the image until a frame showing it arrives (frames carry the last
  move they include); drags send a move per animation frame instead of every 30 ms; vtk.js views apply a
  wheel's zoom once per animation frame however many events arrive in it.
- Faster start: the page's layout is built in one pass (trame rendered each block's subtree again as it
  closed) and `install.sh` compiles bytecode at install, so the first launch no longer compiles ~500 modules;
  the desktop shell notices the server's URL sooner. Launch to a checked case: 1.6 s to 1.4 s warm, the
  server's URL 1.05 s to 0.8 s.
- A Settings page (Mallard > Settings…, Cmd ,): theme (following the system live), preferred units for
  empty fields, threads and the default remote, notification preferences, the mallard front end and each
  dimension's own executable, recent projects; kept in `settings.toml`, into which the old
  `recent-projects` file moves.
- A case runs the executable of its dimension (from its mesh through `mallard info`, or `[mesh]` `Nz`/`Lz`)
  when Settings names one.
- External edits to the open input: reloaded when there are no unsaved edits, else Reload / Keep Mine /
  Compare.
- Desktop app: system font, native cursors, unselectable chrome, no web context menu or Reload/Print/Find
  keys, no rubber-band window scrolling.
- Settings: Choose… beside each mallard executable, the system's open panel in the desktop app.
- The Analysis page's reference quantities take a number and a unit box, as the case's fields do; areas offer
  units (m^2, mm^2, cm^2, in^2, ft^2) in the case's fields too.
- Notices (recovered edits, a file error, opening a project, an input changed on disk) take a strip under the
  topbar instead of floating over the page's toolbars and buttons.
- Layout checks in Chrome, WebKit and the desktop app's styling: alignment, truncation, overflow, contrast,
  visible keyboard focus and hover feedback; the launch dialog's fields aligned with its report, long
  install paths cut at the start, "1 rank".

Documents:

- The input is a document: edits stay in memory until Save, which writes atomically (temporary file and
  rename, permissions and symbolic links kept); an edited marker in the title (and macOS's close button);
  Undo and Redo; Save As (another input in the project, or elsewhere with the project's files copied);
  Revert to Saved; Save / Don't Save / Cancel before Open, Open Recent, New Project, Close and Quit, and
  before a run, which always starts from the input on disk.
- Crash-safe: unsaved edits are kept in `.input.unsaved.toml` beside the input (which `mallard check` reads
  meanwhile) and restored by the next session.
- Desktop app: a native menu bar (Mallard, File with Open Recent, Edit, View, Window, Help), the
  system's open and save panels for Open, Save As, New Project, the mesh (Choose… beside `mesh.filename`)
  and reference data, the edited dot and proxy icon; Quit and the close button ask first. The page asks
  for the native panel whenever the shell's bridge is there, so the server's own picker never shows in it.
- Browser: the same commands in the command palette and as Cmd/Ctrl S, Shift Cmd/Ctrl S, Cmd/Ctrl O and
  Cmd/Ctrl Z; a picker of the server machine's files (a browser's file dialog gives no path), labeled so;
  another project opens in a new server the page moves to.
- `mallard-gui DIR --new` makes a new project; `mallard-gui case.toml` opens that input.

Milestone M4 (full setup):

- Every catalog section in the case tree; quantities with unit selectors, exact conversion and inline unit
  errors; Mallard quantity strings stored.
- Case view: region and probe previews with drag handles, boundary `origin`/`toward` picked on the view,
  inflow velocity arrows (uniform, sampled profile, mass-flow and stagnation estimates) with a legend.
- Check report tab, launch dialog with measured cost, the cost on the Run button; command palette.
- Live control of running local runs through `mallard ctl set` or the `SET` file.
- Results: probe and line markers on the view; a running run's new snapshots appear without a refresh.

Milestone M2 (more places):

- Kubernetes backend: runs as Jobs on a volume, a helper pod for files, logs, control and transfers, the
  viewer as a pod reached through a localhost `kubectl port-forward`; connection test with `auth can-i`.
- PBS and LSF backends (submit, state, queue, cancel).
- Remote-to-remote transfers: through this machine, pushed or pulled directly, or copied in place on shared
  storage, chosen from a probe kept in `remotes.toml`.
- Check a restart at another remote, continue a run there from it, or move a run and change its home;
  removing the old home is a separate, confirmed step.

First working slice of milestone M1 of the design (Mallard `docs/design/gui.md`).

- Projects: `mallard-project.toml`, run records in `mallard-runs/<run>.toml` (identity only), remotes in
  `~/.config/mallard-gui/remotes.toml`.
- Case setup from the local build's key catalog (`mallard list keys`), with style-preserving edits of
  `input.toml`: comments, order and layout survive, and each edit changes only its own value.
- `mallard check` after every edit, its diagnostics inline at their keys, in the case tree and in a problems
  panel; Run is blocked by errors only.
- Case view (2D and 3D) from `check --view`: zones, regions and probes; zoom, pan, orbit, view presets;
  section tool with clip plane, isolate, x-ray and picking through the cut, the zone list in sync.
- Local runs: start, follow `mallard.jsonl` (progress, residual and monitor plots with labeled axes,
  events, outputs), stop and checkpoint through control files.
- Backend interface with an SSH/Slurm skeleton (ControlMaster connections, submit, state, queue, log),
  tested against a fake `ssh`.
- Results viewer: snapshots of local and remote runs opened where they are, rendered by a viewer process
  (a job next to the data on a remote) and streamed as frames; field, colormap and range, time scrubbing,
  clip and slice, zone surfaces, probes and lines over time, and cell, triangle and cache budgets for large
  meshes. The section tool is shared with the case view.
- Plots: legend entries wrap to new rows instead of running past the edge.
- Case view: Focus frames the selected zone (a small body in a large domain).
- Installs (M3): one-click installs of Mallard on a remote from the release manifest (the relocatable
  archive with its SHA-256 checked, else Apptainer by digest, else Spack as a batch job), behind a
  confirmation showing what runs where; `mallard install verify` as a short job, its verdict recorded;
  versions side by side, upgrades that become the default only once verified, and removal on request.
- Desktop app (M3): a Tauri shell (`desktop/`) around the same server, which writes its URL to
  `--url-file` once ready; `install.sh` installs uv where it is missing and `mallard-gui` (with
  `--solver`, Mallard too), and the GUI finds a `mallard` in its own environment.
- Run health: alerts from the run log's warnings, divergence report and end record, and GUI checks for NaN or
  Inf, CFL above the limit or spiking, dt collapse, a stalled run log, diverging monitors and ledger residuals,
  and the wall-time limit; toasts, desktop notifications (the desktop app's Tauri plugin, else the browser's
  Web Notifications API) and a per-run auto-stop with a restart through the `STOP` control file.
- Export: plots and probe files as CSV, plots as light/dark SVG pairs in the docs' figure style, the results
  view over time as GIF or MP4, and Open in ParaView through a `.pvsm` state.
- Compare runs: resolved inputs side by side with the keys that differ, probe and force series overlaid with
  reference data from CSV.
