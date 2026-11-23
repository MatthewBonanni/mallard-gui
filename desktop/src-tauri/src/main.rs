// Mallard's desktop app: a native window on the same mallard-gui server that `uv tool install` gives.
// The shell starts the server on a project, waits for the URL it writes to a private file (no output
// parsing), points its webview at that URL and stops the server when the app quits or opens another
// project. Until then the window shows the bundled splash (the GUI's assets/loading.html): the server's
// startup steps, which it appends to another private file, or why it failed, with Retry and Show Log. The page gets one bridge, window.mallardShell (shell.js), from the GUI's origin alone: native
// notifications, file dialogs, the window's document state, and opening, closing and quitting, which the
// page asks for once unsaved edits are saved or let go. The native menu (menu.rs) sends its commands to
// the page.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod menu;

use std::fs;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::Mutex;
use std::thread;
use std::time::{Duration, Instant};

use serde::Serialize;
use tauri::ipc::CapabilityBuilder;
use tauri::webview::PageLoadEvent;
use tauri::{
    AppHandle, DragDropEvent, Manager, RunEvent, Url, WebviewUrl, WebviewWindow,
    WebviewWindowBuilder, WindowEvent, Wry,
};
use tauri_plugin_dialog::DialogExt;
use tauri_plugin_notification::NotificationExt;

const STARTUP: Duration = Duration::from_secs(120);
// The bridge's commands; each is allowed for the GUI's origin only (allow_gui).
const COMMANDS: &[&str] = &[
    "notify",
    "set_document",
    "choose_path",
    "open_project",
    "close_project",
    "quit",
    "reveal",
    "open_url",
];

struct Server {
    child: Mutex<Option<Child>>,
    dir: PathBuf,
    splash: Mutex<Option<Url>>,
    screen: Mutex<Splash>, // what the splash shows, sent again whenever it loads
    project: Mutex<Option<PathBuf>>, // the last start's, for Retry
    generation: AtomicU64, // each start's number: a start that another replaced stops watching
    page: AtomicBool,      // a GUI page is open and has told the shell its document
    quitting: AtomicBool, // the page agreed to quit: windows close and the app exits without asking again
    recent: Mutex<Vec<PathBuf>>,
    handles: Mutex<Option<menu::Handles<Wry>>>,
}

#[derive(Debug, PartialEq)]
struct Notice {
    title: String,
    body: String,
    sound: bool,
}

/// What a notify call posts: warnings and errors make a sound; an empty title becomes the app's name.
fn notice(title: &str, body: &str, level: &str) -> Result<Notice, String> {
    let sound = match level {
        "info" => false,
        "warning" | "error" => true,
        other => return Err(format!("level {other:?}: info, warning or error")),
    };
    let title = if title.trim().is_empty() {
        "Mallard"
    } else {
        title
    };
    Ok(Notice {
        title: title.chars().take(200).collect(),
        body: body.chars().take(1000).collect(),
        sound,
    })
}

/// $MALLARD_DESKTOP_NOTIFICATIONS names a file that receives each notification as a JSON line instead of
/// the system: for tests, which must never post real notifications.
#[tauri::command]
fn notify(app: AppHandle, title: String, body: String, level: String) -> Result<(), String> {
    let n = notice(&title, &body, &level)?;
    if let Some(path) = std::env::var_os("MALLARD_DESKTOP_NOTIFICATIONS") {
        use std::io::Write;
        let line =
            serde_json::json!({"title": n.title, "body": n.body, "level": level, "sound": n.sound});
        let mut f = fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(path)
            .map_err(|e| e.to_string())?;
        return writeln!(f, "{line}").map_err(|e| e.to_string());
    }
    let mut builder = app.notification().builder().title(n.title).body(n.body);
    if n.sound {
        builder = builder.sound("default");
    }
    builder.show().map_err(|e| e.to_string())
}

/// The bridge's commands, for pages of the GUI's origin (its port changes with every start) and no other. The
/// dialog plugin's own commands are granted to no page: its panels open through choose_path alone.
fn gui_capability(url: &Url) -> CapabilityBuilder {
    let origin = url.origin().ascii_serialization();
    let mut capability = CapabilityBuilder::new("mallard-gui")
        .remote(format!("{origin}/*"))
        .local(false)
        .window("main");
    for command in COMMANDS {
        capability = capability.permission(format!("allow-{}", command.replace('_', "-")));
    }
    capability
}

/// A new start replaces the capability, so the previous server's origin loses the bridge.
fn allow_gui(app: &AppHandle, url: &Url) -> Result<(), String> {
    app.add_capability(gui_capability(url))
        .map_err(|e| e.to_string())
}

// The document: the window's title, macOS's edited dot and proxy icon, the menu's Undo, Redo and Show Mesh

#[cfg(target_os = "macos")]
fn mark_document(window: &WebviewWindow, edited: bool, path: &str) {
    let Ok(ns) = window.ns_window() else { return };
    let ns = ns as usize;
    let path = path.to_owned();
    let _ = window.run_on_main_thread(move || {
        // SAFETY: the window's NSWindow, used on the main thread while the window exists.
        let ns = unsafe { &*(ns as *const objc2_app_kit::NSWindow) };
        ns.setDocumentEdited(edited);
        ns.setRepresentedFilename(&objc2_foundation::NSString::from_str(&path));
    });
}

#[cfg(not(target_os = "macos"))]
fn mark_document(_window: &WebviewWindow, _edited: bool, _path: &str) {}

/// Called by the page whenever its document changes (and once it is open).
#[allow(clippy::too_many_arguments)]
#[tauri::command]
fn set_document(
    app: AppHandle,
    window: WebviewWindow,
    title: String,
    path: String,
    edited: bool,
    can_undo: bool,
    can_redo: bool,
    mesh: bool,
) {
    let server = app.state::<Server>();
    let first = !server.page.swap(true, Ordering::SeqCst);
    let _ = window.set_title(&title);
    mark_document(&window, edited, &path);
    refresh_menu(&app, first);
    if let Some(h) = server.handles.lock().unwrap().as_ref() {
        h.set_document(can_undo, can_redo, mesh);
    };
}

// Open Recent: the `recent` of mallard-gui's settings.toml (settings.py), which the shell points the server at
// ($MALLARD_GUI_SETTINGS); the shell's older recent-projects file until the server has moved it in.

fn legacy_recent_file(app: &AppHandle) -> Option<PathBuf> {
    data_file(app, "recent-projects")
}

/// mallard-gui's settings.toml: in $MALLARD_DESKTOP_DATA when tests set it, else where mallard-gui keeps it
/// (config.py's config_dir): $MALLARD_GUI_SETTINGS, $MALLARD_GUI_CONFIG_DIR, $XDG_CONFIG_HOME/mallard-gui, or
/// ~/.config/mallard-gui.
fn settings_path(var: impl Fn(&str) -> Option<String>, home: Option<PathBuf>) -> Option<PathBuf> {
    let set = |k: &str| var(k).filter(|v| !v.is_empty());
    if let Some(d) = set("MALLARD_DESKTOP_DATA") {
        return Some(PathBuf::from(d).join("settings.toml"));
    }
    if let Some(f) = set("MALLARD_GUI_SETTINGS") {
        return Some(PathBuf::from(f));
    }
    let dir = set("MALLARD_GUI_CONFIG_DIR")
        .map(PathBuf::from)
        .or_else(|| set("XDG_CONFIG_HOME").map(|x| PathBuf::from(x).join("mallard-gui")))
        .or_else(|| home.map(|h| h.join(".config").join("mallard-gui")))?;
    Some(dir.join("settings.toml"))
}

fn settings_file() -> Option<PathBuf> {
    settings_path(
        |k| std::env::var(k).ok(),
        std::env::var_os("HOME").map(PathBuf::from),
    )
}

fn load_recent(app: &AppHandle) -> Vec<PathBuf> {
    let from_settings = settings_file()
        .and_then(|f| fs::read_to_string(f).ok())
        .and_then(|t| menu::recent_in_settings(&t));
    from_settings.unwrap_or_else(|| {
        legacy_recent_file(app)
            .and_then(|f| fs::read_to_string(f).ok())
            .map(|t| menu::read_recent(&t))
            .unwrap_or_default()
    })
}

fn clear_recent(app: &AppHandle) {
    if let Some(f) = settings_file() {
        if let Some(text) = fs::read_to_string(&f)
            .ok()
            .and_then(|t| menu::settings_without_recent(&t))
        {
            let tmp = f.with_extension("toml.tmp");
            if fs::write(&tmp, text).is_ok() {
                let _ = fs::rename(&tmp, &f);
            }
        }
    }
    if let Some(f) = legacy_recent_file(app) {
        let _ = fs::remove_file(f);
    }
}

/// The menu again when the recent inputs changed, or `force`; document items follow whether a page is open.
fn refresh_menu(app: &AppHandle, force: bool) {
    let server = app.state::<Server>();
    let recent = load_recent(app);
    let changed = *server.recent.lock().unwrap() != recent;
    if changed || force {
        match menu::build(app, &recent) {
            Ok((m, handles)) => {
                let _ = app.set_menu(m);
                *server.handles.lock().unwrap() = Some(handles);
                *server.recent.lock().unwrap() = recent;
            }
            Err(e) => eprintln!("mallard: the menu: {e}"),
        }
    }
    if let Some(h) = server.handles.lock().unwrap().as_ref() {
        h.set_page(server.page.load(Ordering::SeqCst));
    };
}

fn server_alive(server: &Server) -> bool {
    matches!(
        server.child.lock().unwrap().as_mut().map(|c| c.try_wait()),
        Some(Ok(None))
    )
}

/// Whether the page decides (it may have unsaved edits): a GUI page is open and its server answers.
fn page_decides(server: &Server) -> bool {
    server.page.load(Ordering::SeqCst)
        && !server.quitting.load(Ordering::SeqCst)
        && server_alive(server)
}

fn send_to_page(app: &AppHandle, command: &str, arg: Option<&str>) {
    if let Some(w) = app.get_webview_window("main") {
        let _ = w.eval(menu::page_script(command, arg));
    }
}

/// Files dropped on the window: the page takes them (asking first about unsaved edits); on the splash, a project
/// opens.
fn on_drop(app: &AppHandle, paths: &[PathBuf]) {
    let Some((command, path)) = menu::dropped_call(paths) else {
        return;
    };
    if page_decides(&app.state::<Server>()) {
        send_to_page(app, &command, Some(&path));
    } else if command == "open_recent" {
        let app = app.clone();
        thread::spawn(move || switch_project(&app, Some(PathBuf::from(path)), false));
    }
}

fn on_menu(app: &AppHandle, id: &str) {
    let server = app.state::<Server>();
    match id {
        menu::DOCS => open_external(menu::DOCS_URL),
        menu::CLEAR_RECENT => {
            clear_recent(app);
            refresh_menu(app, true);
        }
        _ if page_decides(&server) => {
            let recent = server.recent.lock().unwrap().clone();
            if let Some((command, arg)) = menu::page_call(id, &recent) {
                send_to_page(app, &command, arg.as_deref());
            }
        }
        // No page to ask: the splash, or a server that stopped.
        "quit" => app.exit(0),
        "about" => {
            app.dialog()
                .message(
                    "Case setup, runs and analysis for the Mallard flow solver.\n\n\
                     Solver: shown here once a project is open.\n\n\
                     Matthew Bonanni · Apache License 2.0 · Icons: Lucide (ISC License)\n\
                     github.com/MatthewBonanni/mallard",
                )
                .title(format!("Mallard GUI {}", app.package_info().version))
                .show(|_| {});
        }
        "open" | "new_project" => {
            let (app, new) = (app.clone(), id == "new_project");
            thread::spawn(move || {
                let choice = if new {
                    Choice::new(Panel::Save, "New Project: a new directory for the case")
                } else {
                    Choice::new(Panel::Open, "Open a Mallard input")
                        .filter("Mallard input", &["toml"])
                };
                if let Some(path) = choice.show(&app) {
                    switch_project(&app, Some(path), new);
                }
            });
        }
        _ => {
            let recent = server.recent.lock().unwrap().clone();
            if let Some(("open_recent", Some(path))) = menu::page_call(id, &recent)
                .as_ref()
                .map(|(c, a)| (c.as_str(), a.clone()))
            {
                let app = app.clone();
                thread::spawn(move || switch_project(&app, Some(PathBuf::from(path)), false));
            }
        }
    }
}

// The system's open and save panels (NSOpenPanel and NSSavePanel on macOS)

#[derive(Debug, Clone, Copy, PartialEq)]
enum Panel {
    Open,
    Save,
}

impl Panel {
    fn parse(mode: &str) -> Result<Panel, String> {
        match mode {
            "open" => Ok(Panel::Open),
            "save" => Ok(Panel::Save),
            other => Err(format!("mode {other:?}: open or save")),
        }
    }
}

#[derive(Debug, Clone, PartialEq, serde::Deserialize)]
struct Filter {
    name: String,
    extensions: Vec<String>,
}

#[derive(Debug, PartialEq)]
struct Choice {
    panel: Panel,
    title: String,
    directory: String,
    name: String,
    filters: Vec<Filter>,
}

impl Choice {
    fn new(panel: Panel, title: &str) -> Choice {
        Choice {
            panel,
            title: title.into(),
            directory: String::new(),
            name: String::new(),
            filters: Vec::new(),
        }
    }

    fn filter(mut self, name: &str, extensions: &[&str]) -> Choice {
        self.filters.push(Filter {
            name: name.into(),
            extensions: extensions.iter().map(|e| e.to_string()).collect(),
        });
        self
    }

    /// What the page asked for: bare extensions only (`toml`, not `*.toml` or `../x`); a filter left with
    /// none is dropped, so it never hides every file.
    fn from_page(
        mode: &str,
        title: String,
        directory: String,
        name: String,
        filters: Vec<Filter>,
    ) -> Result<Choice, String> {
        let filters = filters
            .into_iter()
            .map(|f| Filter {
                name: f.name,
                extensions: f
                    .extensions
                    .into_iter()
                    .filter(|e| !e.is_empty() && e.chars().all(|c| c.is_ascii_alphanumeric()))
                    .collect(),
            })
            .filter(|f| !f.extensions.is_empty())
            .collect();
        Ok(Choice {
            panel: Panel::parse(mode)?,
            title,
            directory,
            name,
            filters,
        })
    }

    /// The panel, blocking until it closes: never on the main thread.
    fn show(self, app: &AppHandle) -> Option<PathBuf> {
        let mut dialog = app
            .dialog()
            .file()
            .set_title(self.title)
            .set_can_create_directories(true);
        if !self.directory.is_empty() {
            dialog = dialog.set_directory(self.directory);
        }
        if !self.name.is_empty() {
            dialog = dialog.set_file_name(self.name);
        }
        for f in &self.filters {
            let extensions: Vec<&str> = f.extensions.iter().map(String::as_str).collect();
            dialog = dialog.add_filter(&f.name, &extensions);
        }
        match self.panel {
            Panel::Open => dialog.blocking_pick_file(),
            Panel::Save => dialog.blocking_save_file(),
        }
        .and_then(|p| p.into_path().ok())
    }
}

/// The bridge's one dialog command: async, so the blocking panel runs off the main thread. Resolves to the
/// chosen path, or null when cancelled.
#[tauri::command]
async fn choose_path(
    app: AppHandle,
    mode: String,
    title: String,
    directory: String,
    name: String,
    filters: Vec<Filter>,
) -> Result<Option<String>, String> {
    let choice = Choice::from_page(&mode, title, directory, name, filters)?;
    Ok(choice.show(&app).map(|p| p.to_string_lossy().into_owned()))
}

// Opening, closing, quitting: the page asks once its edits are saved or let go

/// Stop the server and show the splash with `splash`; the window forgets its document.
fn show_splash(app: &AppHandle, splash: Splash) {
    let server = app.state::<Server>();
    server.page.store(false, Ordering::SeqCst);
    stop(&server);
    refresh_menu(app, false);
    let Some(window) = app.get_webview_window("main") else {
        return;
    };
    let _ = window.set_title("Mallard");
    mark_document(&window, false, "");
    *server.screen.lock().unwrap() = splash;
    if let Some(url) = server.splash.lock().unwrap().clone() {
        let _ = window.navigate(url);
    };
}

/// Another project in this window: `path` is a project directory or an input in one; `new` makes it first.
fn switch_project(app: &AppHandle, path: Option<PathBuf>, new: bool) {
    let log = app.state::<Server>().dir.join("server.log");
    let splash = path
        .as_ref()
        .map(|p| Splash::loading(p, &log))
        .unwrap_or_else(|| Splash::idle(""));
    show_splash(app, splash);
    if let Some(window) = app.get_webview_window("main") {
        start(app.clone(), window, path, new);
    }
}

#[tauri::command]
fn open_project(app: AppHandle, path: String) {
    thread::spawn(move || switch_project(&app, Some(PathBuf::from(path)), false));
}

#[tauri::command]
fn close_project(app: AppHandle) {
    thread::spawn(move || {
        show_splash(&app, Splash::idle("File > Open… or Open Recent opens one."))
    });
}

#[tauri::command]
fn quit(app: AppHandle) {
    app.state::<Server>().quitting.store(true, Ordering::SeqCst);
    app.exit(0);
}

fn open_external(target: &str) {
    #[cfg(target_os = "macos")]
    let program = "open";
    #[cfg(target_os = "windows")]
    let program = "explorer";
    #[cfg(not(any(target_os = "macos", target_os = "windows")))]
    let program = "xdg-open";
    let _ = Command::new(program)
        .arg(target)
        .stdin(Stdio::null())
        .spawn();
}

#[tauri::command]
fn reveal(path: String) -> Result<(), String> {
    let path = PathBuf::from(path);
    if !path.exists() {
        return Err(format!("{}: no such file", path.display()));
    }
    #[cfg(target_os = "macos")]
    let spawned = Command::new("open").arg("-R").arg(&path).spawn();
    #[cfg(target_os = "windows")]
    let spawned = Command::new("explorer")
        .arg(format!("/select,{}", path.display()))
        .spawn();
    #[cfg(not(any(target_os = "macos", target_os = "windows")))]
    let spawned = Command::new("xdg-open")
        .arg(path.parent().unwrap_or(&path))
        .spawn();
    spawned.map(|_| ()).map_err(|e| e.to_string())
}

/// Web pages only: the bridge never opens files or other schemes this way.
fn web_url(url: &str) -> Result<Url, String> {
    let parsed = Url::parse(url).map_err(|e| e.to_string())?;
    if parsed.scheme() != "https" {
        return Err(format!("{url}: only https links open"));
    }
    Ok(parsed)
}

#[tauri::command]
fn open_url(url: String) -> Result<(), String> {
    open_external(web_url(&url)?.as_str());
    Ok(())
}

// The splash: startup stages, a failure with Retry and Show Log, or no project

const STAGES: [&str; 4] = ["server", "case", "interface", "render"];

/// What the splash shows; its window.mallardSplash.update takes this as JSON.
#[derive(Clone, Debug, Default, PartialEq, Serialize)]
struct Splash {
    mode: &'static str, // "loading", "failed" or "idle"
    project: String,
    path: String,
    stage: &'static str,
    detail: String,
    times: Vec<u64>, // milliseconds of each finished stage
    message: String,
    reason: String,
    log: Option<String>,
    tail: String,
}

impl Splash {
    fn idle(message: &str) -> Splash {
        Splash {
            mode: "idle",
            message: message.into(),
            ..Splash::default()
        }
    }

    fn loading(project: &Path, log: &Path) -> Splash {
        Splash {
            mode: "loading",
            project: project
                .file_name()
                .map(|n| n.to_string_lossy().into_owned())
                .unwrap_or_default(),
            path: project.display().to_string(),
            stage: STAGES[0],
            log: Some(log.display().to_string()),
            ..Splash::default()
        }
    }

    /// The stage stays where it failed; `log` is None when the server never started.
    fn failed(self, reason: &str, log: Option<&Path>) -> Splash {
        Splash {
            mode: "failed",
            reason: reason.into(),
            tail: log.map(tail).unwrap_or_default(),
            log: log.map(|l| l.display().to_string()),
            detail: String::new(),
            stage: if self.stage.is_empty() {
                STAGES[0]
            } else {
                self.stage
            },
            ..self
        }
    }
}

/// A line of the server's progress file: its stage and what it is doing.
fn step(line: &str) -> Option<(&'static str, &'static str)> {
    match line.trim() {
        "mallard" => Some(("server", "Checking Mallard")),
        "libraries" => Some(("server", "Loading VTK and trame")),
        "case" => Some(("case", "")),
        "interface" => Some(("interface", "")),
        _ => None,
    }
}

/// The splash during a start, timing each stage.
struct Progress {
    splash: Splash,
    since: Instant,
}

impl Progress {
    /// Moves on to `stage` (never back): each stage left behind gets its time, a skipped one zero. Whether the
    /// splash changed.
    fn advance(&mut self, stage: &'static str, detail: &str, now: Instant) -> bool {
        let at = |s: &str| STAGES.iter().position(|x| *x == s);
        let (Some(from), Some(to)) = (at(self.splash.stage), at(stage)) else {
            return false;
        };
        if to < from || (to == from && self.splash.detail == detail) {
            return false;
        }
        for i in from..to {
            let ms = if i == from {
                now.duration_since(self.since).as_millis() as u64
            } else {
                0
            };
            self.splash.times.push(ms);
        }
        if to > from {
            self.since = now;
        }
        self.splash.stage = stage;
        self.splash.detail = detail.into();
        true
    }
}

/// Why the server stopped: its last `mallard-gui:` line, else its last line, else the exit status.
fn failure_reason(tail: &str, status: &str) -> String {
    let lines: Vec<&str> = tail
        .lines()
        .map(str::trim)
        .filter(|l| !l.is_empty())
        .collect();
    lines
        .iter()
        .rev()
        .find(|l| l.starts_with("mallard-gui:"))
        .or(lines.last())
        .map(|l| l.to_string())
        .unwrap_or_else(|| format!("mallard-gui stopped ({status})"))
}

/// A splash button's request (it navigates to /__mallard/<action> on the splash's own origin), else None.
fn splash_action(url: &Url, splash: &Url) -> Option<&'static str> {
    if !same_site(url, splash) {
        return None;
    }
    match url.path().strip_prefix("/__mallard/")? {
        "retry" => Some("retry"),
        "log" => Some("log"),
        _ => None,
    }
}

/// Url::origin is opaque, so never equal, for the bundled pages' tauri:// scheme.
fn same_site(a: &Url, b: &Url) -> bool {
    a.scheme() == b.scheme()
        && a.host_str() == b.host_str()
        && a.port_or_known_default() == b.port_or_known_default()
}

fn is_splash(url: &Url, splash: Option<&Url>) -> bool {
    splash.is_some_and(|s| same_site(s, url) && s.path() == url.path())
}

/// Keep `splash` for the next time the splash loads, and show it now if it is the page.
fn show(app: &AppHandle, splash: Splash) {
    let server = app.state::<Server>();
    *server.screen.lock().unwrap() = splash;
    if let Some(window) = app.get_webview_window("main") {
        send_splash(&window, &server);
    }
}

fn send_splash(window: &WebviewWindow, server: &Server) {
    let json = serde_json::to_string(&*server.screen.lock().unwrap()).unwrap_or_default();
    let _ = window.eval(format!(
        "window.mallardSplash && window.mallardSplash.update({json})"
    ));
}

fn on_splash_action(app: &AppHandle, action: &str) {
    let server = app.state::<Server>();
    match action {
        "log" => {
            let log = server.dir.join("server.log");
            if log.is_file() {
                open_external(&log.to_string_lossy());
            }
        }
        "retry" if !server_alive(&server) && server.screen.lock().unwrap().mode != "loading" => {
            let project = server.project.lock().unwrap().clone();
            if let Some(window) = app.get_webview_window("main") {
                let app = app.clone();
                thread::spawn(move || start(app, window, project, false));
            }
        }
        _ => {}
    }
}

enum Outcome {
    Ready(String),
    Failed(String),
    Replaced,
}

/// Waits for the server's URL, reporting its progress steps; it fails when the server exits or takes
/// longer than `timeout`, and gives up quietly once another start replaces it.
fn watch(
    server: &Server,
    generation: u64,
    files: (&Path, &Path, &Path), // url, progress, log
    timeout: Duration,
    progress: &mut Progress,
    report: &mut dyn FnMut(&Splash),
) -> Outcome {
    let (url_file, progress_file, log_file) = files;
    let end = Instant::now() + timeout;
    let mut seen = 0;
    while Instant::now() < end {
        if server.generation.load(Ordering::SeqCst) != generation {
            return Outcome::Replaced;
        }
        if let Some(url) = fs::read_to_string(url_file).ok().filter(|u| !u.is_empty()) {
            return Outcome::Ready(url.trim().to_string());
        }
        let lines = fs::read_to_string(progress_file).unwrap_or_default();
        if let Some(complete) = lines.rfind('\n').map(|i| i + 1).filter(|&e| e > seen) {
            let now = Instant::now();
            let mut changed = false;
            for (stage, detail) in lines[seen..complete].lines().filter_map(step) {
                changed |= progress.advance(stage, detail, now);
            }
            seen = complete;
            if changed {
                report(&progress.splash);
            }
        }
        let exited = server
            .child
            .lock()
            .unwrap()
            .as_mut()
            .and_then(|c| c.try_wait().ok().flatten());
        if let Some(status) = exited {
            return Outcome::Failed(failure_reason(&tail(log_file), &status.to_string()));
        }
        thread::sleep(Duration::from_millis(25));
    }
    Outcome::Failed(format!(
        "mallard-gui did not start in {} s",
        timeout.as_secs()
    ))
}

/// The user's login PATH: an app opened from the Finder gets launchd's bare PATH, without uv's tool
/// directory, Homebrew or a mallard the shell's profile puts on PATH.
fn login_path() -> String {
    let shell = std::env::var("SHELL").unwrap_or_else(|_| "/bin/zsh".into());
    let from_shell = Command::new(shell)
        .args(["-l", "-c", "printf %s \"$PATH\""])
        .stdin(Stdio::null())
        .stderr(Stdio::null())
        .output()
        .ok()
        .filter(|o| o.status.success())
        .and_then(|o| String::from_utf8(o.stdout).ok())
        .unwrap_or_default();
    let home = std::env::var("HOME").unwrap_or_default();
    let mut dirs: Vec<String> = from_shell
        .split(':')
        .filter(|d| !d.is_empty())
        .map(String::from)
        .collect();
    for d in [
        format!("{home}/.local/bin"),
        "/opt/homebrew/bin".into(),
        "/usr/local/bin".into(),
    ] {
        if !dirs.contains(&d) {
            dirs.push(d);
        }
    }
    for d in std::env::var("PATH").unwrap_or_default().split(':') {
        if !d.is_empty() && !dirs.iter().any(|x| x == d) {
            dirs.push(d.into());
        }
    }
    dirs.join(":")
}

/// $MALLARD_GUI_SERVER, else the frozen server bundled in the app's resources (desktop/package.sh), else
/// mallard-gui on PATH.
fn server_program(path: &str, resources: Option<PathBuf>) -> Option<PathBuf> {
    if let Ok(p) = std::env::var("MALLARD_GUI_SERVER") {
        return Some(PathBuf::from(p));
    }
    if let Some(bundled) = resources
        .map(|d| d.join("mallard-gui-server").join("mallard-gui-server"))
        .filter(|p| p.is_file())
    {
        return Some(bundled);
    }
    path.split(':')
        .map(|d| Path::new(d).join("mallard-gui"))
        .find(|p| p.is_file())
}

/// Where the shell keeps what it remembers: $MALLARD_DESKTOP_DATA, else the app's config directory. Tests
/// set it so they never touch the user's own.
fn data_dir(env: Option<std::ffi::OsString>, default: Option<PathBuf>) -> Option<PathBuf> {
    env.filter(|d| !d.is_empty()).map(PathBuf::from).or(default)
}

fn data_file(app: &AppHandle, name: &str) -> Option<PathBuf> {
    data_dir(
        std::env::var_os("MALLARD_DESKTOP_DATA"),
        app.path().app_config_dir().ok(),
    )
    .map(|d| d.join(name))
}

fn last_project_file(app: &AppHandle) -> Option<PathBuf> {
    data_file(app, "last-project")
}

/// The project: the first argument, else the last one opened, else one the user picks.
fn choose_project(app: &AppHandle) -> Option<PathBuf> {
    if let Some(arg) = std::env::args().skip(1).find(|a| !a.starts_with("-psn")) {
        return Some(PathBuf::from(arg));
    }
    let remembered = last_project_file(app)
        .and_then(|f| fs::read_to_string(f).ok())
        .map(|s| PathBuf::from(s.trim()))
        .filter(|p| p.is_file() || p.is_dir());
    if remembered.is_some() {
        return remembered;
    }
    show(app, Splash::idle(""));
    app.dialog()
        .file()
        .set_title("Open a Mallard project (a directory holding input.toml)")
        .blocking_pick_folder()
        .and_then(|p| p.into_path().ok())
}

fn tail(path: &Path) -> String {
    let text = fs::read_to_string(path).unwrap_or_default();
    let lines: Vec<&str> = text.lines().collect();
    lines[lines.len().saturating_sub(15)..].join("\n")
}

fn start(app: AppHandle, window: WebviewWindow, project: Option<PathBuf>, new: bool) {
    let server = app.state::<Server>();
    let generation = server.generation.fetch_add(1, Ordering::SeqCst) + 1;
    let Some(project) = project.or_else(|| choose_project(&app)) else {
        return show(
            &app,
            Splash::idle("No project chosen. File > Open… or Open Recent opens one."),
        );
    };
    *server.project.lock().unwrap() = Some(project.clone());
    let url_file = server.dir.join("url");
    let progress_file = server.dir.join("progress");
    let log_file = server.dir.join("server.log");
    let mut progress = Progress {
        splash: Splash::loading(&project, &log_file),
        since: Instant::now(),
    };
    let name = progress.splash.project.clone();
    let _ = window.set_title(&format!("Mallard: {name}"));
    show(&app, progress.splash.clone());
    let fail = |splash: Splash, reason: &str, log: Option<&Path>| {
        show(&app, splash.failed(reason, log));
    };
    let path = login_path();
    let Some(program) = server_program(&path, app.path().resource_dir().ok()) else {
        return fail(
            progress.splash,
            "mallard-gui was not found on your login PATH or in ~/.local/bin. Install it with mallard-gui's \
             install.sh (uv tool install), or set MALLARD_GUI_SERVER to its path.",
            None,
        );
    };
    let _ = fs::remove_file(&url_file);
    let _ = fs::remove_file(&progress_file);
    let log = match fs::File::create(&log_file) {
        Ok(f) => f,
        Err(e) => {
            let reason = format!("{}: {e}", log_file.display());
            return fail(progress.splash, &reason, None);
        }
    };
    let mut command = Command::new(&program);
    command
        .arg(&project)
        .args(["--no-browser", "--url-file"])
        .arg(&url_file)
        .env("MALLARD_GUI_PROGRESS_FILE", &progress_file) // an older server ignores it
        .env("PATH", &path);
    if new {
        command.arg("--new");
    }
    if let Some(f) = settings_file() {
        command.env("MALLARD_GUI_SETTINGS", f);
    }
    if let Some(f) = legacy_recent_file(&app) {
        command.env("MALLARD_GUI_RECENT", f); // moved into settings.toml by the server, once
    }
    let child = command
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(log)
        .spawn();
    let child = match child {
        Ok(c) => c,
        Err(e) => {
            let reason = format!("{}: {e}", program.display());
            return fail(progress.splash, &reason, Some(&log_file));
        }
    };
    *server.child.lock().unwrap() = Some(child);
    let files = (
        url_file.as_path(),
        progress_file.as_path(),
        log_file.as_path(),
    );
    let mut report = |splash: &Splash| show(&app, splash.clone());
    let url = match watch(
        &server,
        generation,
        files,
        STARTUP,
        &mut progress,
        &mut report,
    ) {
        Outcome::Ready(url) => url,
        Outcome::Failed(reason) => return fail(progress.splash, &reason, Some(&log_file)),
        Outcome::Replaced => return,
    };
    if let Some(f) = last_project_file(&app) {
        let _ = f.parent().map(fs::create_dir_all);
        let _ = fs::write(f, project.to_string_lossy().as_bytes());
    }
    let mut url = match Url::parse(&url) {
        Ok(u) => u,
        Err(e) => {
            let reason = format!("mallard-gui wrote an unusable URL: {e}");
            return fail(progress.splash, &reason, Some(&log_file));
        }
    };
    if let Err(e) = allow_gui(&app, &url) {
        let reason = format!("the GUI's permissions: {e}");
        return fail(progress.splash, &reason, Some(&log_file));
    }
    // The GUI's page covers itself with the same screen until its first frame, showing these times.
    progress.advance("render", "", Instant::now());
    let times: Vec<String> = progress.splash.times.iter().map(u64::to_string).collect();
    url.set_fragment(Some(&format!("startup={}", times.join(","))));
    *server.screen.lock().unwrap() = progress.splash;
    let _ = window.navigate(url);
}

/// SIGINT first, so the server's cleanup (viewer jobs, its token file) runs; a kill if it lingers. The
/// private directory stays for the next server, until the app exits.
fn stop(server: &Server) {
    if let Some(mut child) = server.child.lock().unwrap().take() {
        #[cfg(unix)]
        unsafe {
            libc::kill(child.id() as libc::pid_t, libc::SIGINT);
        }
        let end = Instant::now() + Duration::from_secs(5);
        while Instant::now() < end {
            if let Ok(Some(_)) = child.try_wait() {
                break;
            }
            thread::sleep(Duration::from_millis(50));
        }
        let _ = child.kill();
        let _ = child.wait();
    }
}

fn private_dir() -> PathBuf {
    let dir = std::env::temp_dir().join(format!("mallard-desktop-{}", std::process::id()));
    let _ = fs::create_dir_all(&dir);
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let _ = fs::set_permissions(&dir, fs::Permissions::from_mode(0o700));
    }
    dir
}

fn main() {
    let app = tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_notification::init())
        .invoke_handler(tauri::generate_handler![
            notify,
            set_document,
            choose_path,
            open_project,
            close_project,
            quit,
            reveal,
            open_url
        ])
        .manage(Server {
            child: Mutex::new(None),
            dir: private_dir(),
            splash: Mutex::new(None),
            screen: Mutex::new(Splash::default()),
            project: Mutex::new(None),
            generation: AtomicU64::new(0),
            page: AtomicBool::new(false),
            quitting: AtomicBool::new(false),
            recent: Mutex::new(Vec::new()),
            handles: Mutex::new(None),
        })
        .on_menu_event(|app, event| on_menu(app, event.id().as_ref()))
        .setup(|app| {
            refresh_menu(app.handle(), true);
            let (on_load, on_navigate) = (app.handle().clone(), app.handle().clone());
            // Hidden until the splash has drawn, so the window never flashes an empty frame.
            let window =
                WebviewWindowBuilder::new(app, "main", WebviewUrl::App("index.html".into()))
                    .title("Mallard")
                    .initialization_script(include_str!("shell.js"))
                    .inner_size(1440.0, 900.0)
                    .min_inner_size(900.0, 600.0)
                    .visible(false)
                    .on_page_load(move |window, payload| {
                        let server = on_load.state::<Server>();
                        let splash = server.splash.lock().unwrap().clone();
                        if payload.event() == PageLoadEvent::Finished
                            && is_splash(payload.url(), splash.as_ref())
                        {
                            send_splash(&window, &server);
                            let _ = window.show();
                        }
                    })
                    .on_navigation(move |url| {
                        let splash = on_navigate.state::<Server>().splash.lock().unwrap().clone();
                        let Some(action) = splash.and_then(|s| splash_action(url, &s)) else {
                            return true;
                        };
                        on_splash_action(&on_navigate, action);
                        false
                    })
                    .build()?;
            *app.state::<Server>().splash.lock().unwrap() = window.url().ok();
            let shown = window.clone();
            thread::spawn(move || {
                thread::sleep(Duration::from_secs(2)); // should the splash's load never be reported
                let _ = shown.show();
            });
            let handle = app.handle().clone();
            window.on_window_event(move |event| match event {
                // The red button with unsaved edits: the page asks first, as for Quit.
                WindowEvent::CloseRequested { api, .. } => {
                    if page_decides(&handle.state::<Server>()) {
                        api.prevent_close();
                        send_to_page(&handle, "quit", None);
                    }
                }
                WindowEvent::DragDrop(DragDropEvent::Drop { paths, .. }) => on_drop(&handle, paths),
                _ => {}
            });
            let handle = app.handle().clone();
            thread::spawn(move || start(handle, window, None, false));
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("the Tauri app failed to build");
    app.run(|handle, event| match event {
        // Quit from the Dock or at logout: the page asks first too.
        RunEvent::ExitRequested {
            code: None, api, ..
        } if page_decides(&handle.state::<Server>()) => {
            api.prevent_exit();
            send_to_page(handle, "quit", None);
        }
        RunEvent::Exit => {
            let server = handle.state::<Server>();
            stop(&server);
            let _ = fs::remove_dir_all(&server.dir);
        }
        _ => {}
    });
}

#[cfg(test)]
mod tests {
    use super::*;

    fn test_server(dir: &Path) -> Server {
        Server {
            child: Mutex::new(None),
            dir: dir.to_path_buf(),
            splash: Mutex::new(None),
            screen: Mutex::new(Splash::default()),
            project: Mutex::new(None),
            generation: AtomicU64::new(1),
            page: AtomicBool::new(false),
            quitting: AtomicBool::new(false),
            recent: Mutex::new(Vec::new()),
            handles: Mutex::new(None),
        }
    }

    /// A stand-in server: a shell script with stderr into the log, as start runs mallard-gui.
    fn spawn_script(server: &Server, script: &str) {
        let log = fs::File::create(server.dir.join("server.log")).unwrap();
        let child = Command::new("sh")
            .args(["-c", script])
            .current_dir(&server.dir)
            .stdin(Stdio::null())
            .stderr(log)
            .spawn()
            .unwrap();
        *server.child.lock().unwrap() = Some(child);
    }

    fn watch_script(script: &str, timeout: Duration) -> (Outcome, Vec<Splash>, Progress, PathBuf) {
        let dir = std::env::temp_dir().join(format!(
            "mallard-desktop-test-{}-{:?}",
            std::process::id(),
            thread::current().id()
        ));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        let server = test_server(&dir);
        spawn_script(&server, script);
        let log = dir.join("server.log");
        let mut progress = Progress {
            splash: Splash::loading(Path::new("/cases/cyl"), &log),
            since: Instant::now(),
        };
        let mut seen = Vec::new();
        let outcome = watch(
            &server,
            1,
            (&dir.join("url"), &dir.join("progress"), &log),
            timeout,
            &mut progress,
            &mut |s| seen.push(s.clone()),
        );
        stop(&server);
        (outcome, seen, progress, dir)
    }

    #[test]
    fn the_server_frozen_into_the_apps_resources_comes_before_path() {
        if std::env::var_os("MALLARD_GUI_SERVER").is_some() {
            return;
        }
        let dir =
            std::env::temp_dir().join(format!("mallard-desktop-resources-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        let (resources, bin) = (dir.join("Resources"), dir.join("bin"));
        fs::create_dir_all(resources.join("mallard-gui-server")).unwrap();
        fs::create_dir_all(&bin).unwrap();
        fs::write(bin.join("mallard-gui"), "").unwrap();
        let path = bin.to_str().unwrap();
        assert_eq!(
            server_program(path, Some(resources.clone())),
            Some(bin.join("mallard-gui"))
        );
        let frozen = resources
            .join("mallard-gui-server")
            .join("mallard-gui-server");
        fs::write(&frozen, "").unwrap();
        assert_eq!(server_program(path, Some(resources)), Some(frozen));
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn the_servers_steps_reach_the_splash_and_its_url_ends_the_wait() {
        let (outcome, seen, progress, dir) = watch_script(
            "echo mallard >> progress; sleep 0.1; echo libraries >> progress; sleep 0.1; echo case >> progress; \
             sleep 0.1; echo interface >> progress; sleep 0.1; printf http://127.0.0.1:1/ > url; sleep 1",
            Duration::from_secs(20),
        );
        assert!(matches!(outcome, Outcome::Ready(ref u) if u == "http://127.0.0.1:1/"));
        let shown: Vec<(&str, &str)> = seen.iter().map(|s| (s.stage, s.detail.as_str())).collect();
        assert_eq!(
            shown,
            [
                ("server", "Checking Mallard"),
                ("server", "Loading VTK and trame"),
                ("case", ""),
                ("interface", "")
            ]
        );
        assert_eq!(progress.splash.times.len(), 2);
        assert!(
            progress.splash.times[0] >= 150,
            "{:?}",
            progress.splash.times
        );
        let _ = fs::remove_dir_all(dir);
    }

    #[test]
    fn a_server_that_exits_fails_with_its_reason_and_its_log() {
        let (outcome, seen, progress, dir) = watch_script(
            "echo mallard >> progress; echo case >> progress; sleep 0.1; echo 'Traceback: ...' >&2; \
             echo 'mallard-gui: input.toml: line 12: expected =' >&2; echo 'shutting down' >&2; exit 3",
            Duration::from_secs(20),
        );
        let Outcome::Failed(reason) = outcome else {
            panic!("a failure expected")
        };
        assert_eq!(reason, "mallard-gui: input.toml: line 12: expected =");
        assert_eq!(seen.last().unwrap().stage, "case");
        let log = dir.join("server.log");
        let failed = progress.splash.failed(&reason, Some(&log));
        let json = serde_json::to_value(&failed).unwrap();
        assert_eq!(json["mode"], "failed");
        assert_eq!(json["stage"], "case");
        assert_eq!(json["project"], "cyl");
        assert_eq!(json["log"], log.display().to_string());
        assert!(json["tail"].as_str().unwrap().contains("Traceback"));
        let _ = fs::remove_dir_all(dir);
    }

    #[test]
    fn a_server_that_never_answers_fails_after_the_timeout() {
        let (outcome, _, progress, dir) =
            watch_script("echo mallard >> progress; sleep 30", Duration::from_secs(1));
        assert!(
            matches!(outcome, Outcome::Failed(ref r) if r == "mallard-gui did not start in 1 s")
        );
        assert_eq!(progress.splash.stage, "server");
        let _ = fs::remove_dir_all(dir);
    }

    #[test]
    fn stages_move_forward_only_and_time_each_one() {
        let t0 = Instant::now();
        let mut p = Progress {
            splash: Splash::loading(Path::new("/c/x"), Path::new("/l")),
            since: t0,
        };
        assert!(p.advance("server", "Checking Mallard", t0));
        assert!(!p.advance("server", "Checking Mallard", t0));
        assert!(p.advance("interface", "", t0 + Duration::from_millis(1500)));
        assert_eq!(p.splash.times, [1500, 0]); // the case's step never came: zero
        assert!(!p.advance("case", "", t0 + Duration::from_secs(2)));
        assert!(p.advance("render", "", t0 + Duration::from_millis(1800)));
        assert_eq!(p.splash.times, [1500, 0, 300]);
        assert_eq!(step("libraries"), Some(("server", "Loading VTK and trame")));
        assert_eq!(step("bogus"), None);
    }

    #[test]
    fn a_failure_names_the_servers_own_message_else_its_last_line() {
        assert_eq!(
            failure_reason(
                "x\nmallard-gui: no mallard\n  \nShutting down\n",
                "exit status: 1"
            ),
            "mallard-gui: no mallard"
        );
        assert_eq!(
            failure_reason("Traceback\nValueError: bad\n", "exit status: 1"),
            "ValueError: bad"
        );
        assert_eq!(
            failure_reason("", "exit status: 2"),
            "mallard-gui stopped (exit status: 2)"
        );
        // A failure before the server ran has no log, and keeps the stage it reached.
        let f = Splash::loading(Path::new("/c/x"), Path::new("/l")).failed("not found", None);
        assert_eq!(
            (f.mode, f.stage, f.log, f.tail.as_str()),
            ("failed", "server", None, "")
        );
    }

    #[test]
    fn only_the_splashs_own_buttons_ask_the_shell() {
        let splash = Url::parse("tauri://localhost/index.html").unwrap();
        let at = |u: &str| splash_action(&Url::parse(u).unwrap(), &splash);
        assert_eq!(at("tauri://localhost/__mallard/retry"), Some("retry"));
        assert_eq!(at("tauri://localhost/__mallard/log"), Some("log"));
        assert_eq!(at("tauri://localhost/__mallard/quit"), None);
        assert_eq!(at("http://127.0.0.1:8080/__mallard/retry"), None);
        assert_eq!(at("tauri://localhost/index.html"), None);
        assert!(is_splash(
            &Url::parse("tauri://localhost/index.html?x").unwrap(),
            Some(&splash)
        ));
        assert!(!is_splash(
            &Url::parse("http://127.0.0.1:1/index.html").unwrap(),
            Some(&splash)
        ));
    }

    #[test]
    fn levels_choose_the_sound_and_others_are_refused() {
        assert!(!notice("Run finished", "cyl-re100", "info").unwrap().sound);
        assert!(notice("Run stalled", "", "warning").unwrap().sound);
        assert!(notice("Run failed", "", "error").unwrap().sound);
        assert!(notice("x", "", "critical").is_err());
        assert_eq!(notice(" ", "b", "info").unwrap().title, "Mallard");
    }

    #[test]
    fn settings_live_where_mallard_gui_keeps_them_and_tests_keep_them_apart() {
        let env = |pairs: &'static [(&'static str, &'static str)]| {
            move |k: &str| {
                pairs
                    .iter()
                    .find(|(n, _)| *n == k)
                    .map(|(_, v)| v.to_string())
            }
        };
        let home = Some(PathBuf::from("/home/me"));
        assert_eq!(
            settings_path(env(&[]), home.clone()),
            Some(PathBuf::from("/home/me/.config/mallard-gui/settings.toml"))
        );
        assert_eq!(
            settings_path(env(&[("XDG_CONFIG_HOME", "/x")]), home.clone()),
            Some(PathBuf::from("/x/mallard-gui/settings.toml"))
        );
        assert_eq!(
            settings_path(
                env(&[("MALLARD_GUI_CONFIG_DIR", "/c"), ("XDG_CONFIG_HOME", "/x")]),
                home.clone()
            ),
            Some(PathBuf::from("/c/settings.toml"))
        );
        assert_eq!(
            settings_path(
                env(&[
                    ("MALLARD_GUI_SETTINGS", "/s.toml"),
                    ("MALLARD_GUI_CONFIG_DIR", "/c")
                ]),
                None
            ),
            Some(PathBuf::from("/s.toml"))
        );
        assert_eq!(
            settings_path(
                env(&[
                    ("MALLARD_DESKTOP_DATA", "/t"),
                    ("MALLARD_GUI_SETTINGS", "/s.toml")
                ]),
                home
            ),
            Some(PathBuf::from("/t/settings.toml"))
        );
    }

    #[test]
    fn the_data_directory_override_replaces_the_apps_own() {
        let own = Some(PathBuf::from("/own"));
        assert_eq!(
            data_dir(Some("/tmp/t".into()), own.clone()),
            Some(PathBuf::from("/tmp/t"))
        );
        assert_eq!(data_dir(Some("".into()), own.clone()), own);
        assert_eq!(data_dir(None, own.clone()), own);
    }

    #[test]
    fn the_bridge_opens_web_pages_only() {
        assert!(web_url(menu::DOCS_URL).is_ok());
        assert!(web_url("file:///etc/passwd").is_err());
        assert!(web_url("http://example.com").is_err());
        assert!(web_url("x").is_err());
    }

    fn filter(name: &str, extensions: &[&str]) -> Filter {
        Filter {
            name: name.into(),
            extensions: extensions.iter().map(|e| e.to_string()).collect(),
        }
    }

    #[test]
    fn the_page_asks_for_an_open_or_a_save_panel_with_bare_extensions() {
        let c = Choice::from_page(
            "open",
            "Choose the mesh".into(),
            "/p".into(),
            "".into(),
            vec![
                filter("Mesh", &["msh", "*.cgns", "h5", "../x", ""]),
                filter("Nothing left", &["*"]),
            ],
        )
        .unwrap();
        assert_eq!(c.panel, Panel::Open);
        assert_eq!(c.filters, vec![filter("Mesh", &["msh", "h5"])]);
        let save = Choice::from_page("save", "t".into(), "".into(), "a.toml".into(), vec![]);
        assert_eq!(save.unwrap().panel, Panel::Save);
        for mode in ["folder", "", "OPEN"] {
            assert!(Choice::from_page(mode, "t".into(), "".into(), "".into(), vec![]).is_err());
        }
    }

    fn capability_json(url: &str) -> serde_json::Value {
        use tauri::ipc::RuntimeCapability;
        use tauri::utils::acl::capability::CapabilityFile;
        match gui_capability(&Url::parse(url).unwrap()).build() {
            CapabilityFile::Capability(c) => serde_json::to_value(c).unwrap(),
            _ => panic!("one capability expected"),
        }
    }

    #[test]
    fn the_dialog_is_granted_to_the_gui_origin_alone() {
        let c = capability_json("http://127.0.0.1:8123/?secret=x");
        assert_eq!(
            c["remote"]["urls"],
            serde_json::json!(["http://127.0.0.1:8123/*"])
        );
        assert_eq!(c["local"], false);
        assert_eq!(c["windows"], serde_json::json!(["main"]));
        let granted: Vec<&str> = c["permissions"]
            .as_array()
            .unwrap()
            .iter()
            .map(|p| p.as_str().unwrap())
            .collect();
        let expected: Vec<String> = COMMANDS
            .iter()
            .map(|c| format!("allow-{}", c.replace('_', "-")))
            .collect();
        assert_eq!(granted, expected);
        assert!(granted.contains(&"allow-choose-path"));
        assert!(!granted.iter().any(|p| p.starts_with("dialog:")));
        // The bundled pages (the splash) get no commands at all, the dialog plugin's least of all.
        let default: serde_json::Value =
            serde_json::from_str(include_str!("../capabilities/default.json")).unwrap();
        assert_eq!(default["permissions"], serde_json::json!([]));
        assert!(default.get("remote").is_none());
        let conf: serde_json::Value =
            serde_json::from_str(include_str!("../tauri.conf.json")).unwrap();
        assert!(conf["app"]["security"].get("capabilities").is_none());
    }

    #[test]
    fn every_bridge_command_has_a_permission_for_the_gui() {
        let generated = std::fs::read_dir(concat!(
            env!("CARGO_MANIFEST_DIR"),
            "/permissions/autogenerated"
        ))
        .unwrap()
        .map(|e| e.unwrap().file_name().to_string_lossy().into_owned())
        .collect::<Vec<_>>();
        for command in COMMANDS {
            assert!(
                generated.contains(&format!("{command}.toml")),
                "{command}: no permission (build.rs)"
            );
        }
        let shell = include_str!("shell.js");
        for command in COMMANDS {
            assert!(
                shell.contains(&format!("\"{command}\"")),
                "{command}: not in shell.js"
            );
        }
        let build = include_str!("../build.rs");
        for command in COMMANDS {
            assert!(
                build.contains(&format!("\"{command}\"")),
                "{command}: not in build.rs"
            );
        }
    }
}
