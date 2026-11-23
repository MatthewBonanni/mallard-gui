// The native menu bar. Document and view items are the GUI page's commands (window.mallardMenu, document.js),
// which the page also offers in a browser through its command palette and keyboard shortcuts; the rest are the
// system's own (Hide, Cut, Copy, Paste, Minimize…) or the shell's (Open Recent, the documentation).
use std::path::{Path, PathBuf};

use tauri::menu::{
    CheckMenuItem, Menu, MenuItem, MenuItemKind, PredefinedMenuItem, Submenu, HELP_SUBMENU_ID,
    WINDOW_SUBMENU_ID,
};
use tauri::{AppHandle, Runtime};

pub const RECENT_PREFIX: &str = "recent:";
pub const CLEAR_RECENT: &str = "clear_recent";
pub const DOCS: &str = "docs";
pub const DOCS_URL: &str = "https://github.com/MatthewBonanni/mallard/tree/main/docs";

/// The menu's own items: id, label, accelerator. Each id is a command of the GUI page (document_panel.py).
pub const FILE: &[(&str, &str, Option<&str>)] = &[
    ("new_project", "New Project…", Some("CmdOrCtrl+N")),
    ("open", "Open…", Some("CmdOrCtrl+O")),
];
pub const FILE_AFTER_RECENT: &[(&str, &str, Option<&str>)] = &[
    ("close", "Close Project", Some("CmdOrCtrl+W")),
    ("save", "Save", Some("CmdOrCtrl+S")),
    ("save_as", "Save As…", Some("CmdOrCtrl+Shift+S")),
    ("revert", "Revert to Saved", None),
    ("import_mesh", "Import Mesh…", Some("CmdOrCtrl+Shift+I")),
    ("paraview", "Open in ParaView", Some("CmdOrCtrl+Shift+P")),
    ("reveal", REVEAL, None),
];
/// The platform's name for showing a file where it lives.
#[cfg(target_os = "macos")]
pub const REVEAL: &str = "Reveal in Finder";
#[cfg(target_os = "windows")]
pub const REVEAL: &str = "Show in Explorer";
#[cfg(not(any(target_os = "macos", target_os = "windows")))]
pub const REVEAL: &str = "Show in File Manager";
pub const EDIT: &[(&str, &str, Option<&str>)] = &[
    ("undo", "Undo", Some("CmdOrCtrl+Z")),
    ("redo", "Redo", Some("CmdOrCtrl+Shift+Z")),
];
pub const VIEW: &[(&str, &str, Option<&str>)] = &[
    ("fit", "Zoom to Fit", Some("CmdOrCtrl+0")),
    ("theme", "Toggle Light or Dark Theme", None),
];
/// The rail's pages, in its order (mallard_gui/ui/document_panel.py's PAGES).
pub const PAGES: &[(&str, &str, Option<&str>)] = &[
    ("page_case", "Case Setup", Some("CmdOrCtrl+1")),
    ("page_runs", "Runs", Some("CmdOrCtrl+2")),
    ("page_data", "Data", Some("CmdOrCtrl+3")),
    ("page_results", "Results", Some("CmdOrCtrl+4")),
    ("page_analysis", "Analysis", Some("CmdOrCtrl+5")),
    ("page_compare", "Compare Runs", Some("CmdOrCtrl+6")),
    ("page_remotes", "Remotes", Some("CmdOrCtrl+7")),
];
pub const SHOW_MESH: (&str, &str, Option<&str>) =
    ("show_mesh", "Show Mesh", Some("CmdOrCtrl+Shift+M"));
pub const QUIT: (&str, &str, Option<&str>) = ("quit", "Quit Mallard", Some("CmdOrCtrl+Q"));
pub const SETTINGS: (&str, &str, Option<&str>) = ("settings", "Settings…", Some("CmdOrCtrl+,"));
/// The page's About box: the GUI's version and, apart from it, the solver's, which only the server knows.
pub const ABOUT: (&str, &str, Option<&str>) = ("about", "About Mallard GUI", None);

/// The page commands the menu sends; a page without one of these ignores it.
pub fn page_commands() -> Vec<&'static str> {
    FILE.iter()
        .chain(FILE_AFTER_RECENT)
        .chain(EDIT)
        .chain(VIEW)
        .chain(PAGES)
        .map(|(id, _, _)| *id)
        .chain([SHOW_MESH.0, QUIT.0, SETTINGS.0, ABOUT.0])
        .collect()
}

/// What a menu item asks of the page: its command and argument, or None for the shell's own items.
pub fn page_call(id: &str, recent: &[PathBuf]) -> Option<(String, Option<String>)> {
    if let Some(index) = id.strip_prefix(RECENT_PREFIX) {
        let path = recent.get(index.parse::<usize>().ok()?)?;
        return Some((
            "open_recent".into(),
            Some(path.to_string_lossy().into_owned()),
        ));
    }
    page_commands()
        .contains(&id)
        .then(|| (id.to_string(), None))
}

/// The mesh files the page takes (mallard_gui/ui/paths.py's MESH_EXTENSIONS).
pub const MESH_EXTENSIONS: &[&str] = &["msh", "cas", "cgns", "h5", "hdf5"];

/// What files dropped on the window ask of the page: a project (a directory or an input)
/// opens in place of this one, a mesh becomes the case's mesh. The first file that is one of these decides.
pub fn dropped_call(paths: &[PathBuf]) -> Option<(String, String)> {
    paths.iter().find_map(|p| {
        let ext = p
            .extension()
            .map(|e| e.to_string_lossy().to_lowercase())
            .unwrap_or_default();
        let path = p.to_string_lossy().into_owned();
        if p.is_dir() || (ext == "toml" && p.is_file()) {
            Some(("open_recent".into(), path))
        } else if MESH_EXTENSIONS.contains(&ext.as_str()) && p.is_file() {
            Some(("use_mesh".into(), path))
        } else {
            None
        }
    })
}

/// The JavaScript that hands a command to the page.
pub fn page_script(command: &str, arg: Option<&str>) -> String {
    let quote = |s: &str| serde_json::to_string(s).unwrap_or_default();
    match arg {
        Some(a) => format!(
            "window.mallardMenu && window.mallardMenu({}, {})",
            quote(command),
            quote(a)
        ),
        None => format!(
            "window.mallardMenu && window.mallardMenu({})",
            quote(command)
        ),
    }
}

/// An Open Recent entry: the input's directory and name, as the Finder would tell two `input.toml`s apart.
pub fn recent_label(path: &Path) -> String {
    let name = path
        .file_name()
        .map(|n| n.to_string_lossy().into_owned())
        .unwrap_or_default();
    match path.parent().and_then(|p| p.file_name()) {
        Some(dir) => format!("{} — {name}", dir.to_string_lossy()),
        None => name,
    }
}

/// The recent inputs of settings.toml (mallard_gui/settings.py: `recent`, newest first), or None when it has
/// none yet; missing files and repeats are left out.
pub fn recent_in_settings(text: &str) -> Option<Vec<PathBuf>> {
    let table = text.parse::<toml::Table>().ok()?;
    let entries = table.get("recent")?.as_array()?;
    Some(existing(entries.iter().filter_map(|v| v.as_str())))
}

/// The recent inputs file of GUIs before settings.toml: one path per line, newest first.
pub fn read_recent(text: &str) -> Vec<PathBuf> {
    existing(text.lines().map(str::trim).filter(|l| !l.is_empty()))
}

fn existing<'a>(paths: impl Iterator<Item = &'a str>) -> Vec<PathBuf> {
    let mut out: Vec<PathBuf> = Vec::new();
    for p in paths.map(PathBuf::from) {
        if p.is_file() && !out.contains(&p) {
            out.push(p);
        }
    }
    out
}

/// settings.toml without its recent inputs (Clear Menu); None when the text does not parse, which stays as it is.
pub fn settings_without_recent(text: &str) -> Option<String> {
    let mut table = text.parse::<toml::Table>().ok()?;
    table.remove("recent");
    toml::to_string(&table).ok()
}

/// One entry of the menu bar.
#[derive(Debug, Clone, PartialEq)]
pub enum Entry {
    Item {
        id: String,
        label: String,
        accel: Option<&'static str>,
        enabled: bool,
    },
    Check(&'static str, &'static str, Option<&'static str>),
    System(System),
    Separator,
    Menu {
        id: &'static str,
        label: &'static str,
        entries: Vec<Entry>,
    },
}

/// The system's own items.
#[derive(Debug, Clone, Copy, PartialEq)]
pub enum System {
    Services,
    Hide,
    HideOthers,
    ShowAll,
    Cut,
    Copy,
    Paste,
    SelectAll,
    Fullscreen,
    Minimize,
    Zoom,
    BringAllToFront,
}

fn item(spec: &(&str, &str, Option<&'static str>), enabled: bool) -> Entry {
    Entry::Item {
        id: spec.0.into(),
        label: spec.1.into(),
        accel: spec.2,
        enabled,
    }
}

/// The menu bar, with `recent` under File > Open Recent.
pub fn spec(recent: &[PathBuf]) -> Vec<Entry> {
    use Entry::{Menu, Separator};
    use System::*;
    let mut open_recent: Vec<Entry> = recent
        .iter()
        .enumerate()
        .map(|(i, p)| Entry::Item {
            id: format!("{RECENT_PREFIX}{i}"),
            label: recent_label(p),
            accel: None,
            enabled: true,
        })
        .collect();
    if !recent.is_empty() {
        open_recent.push(Separator);
    }
    open_recent.push(item(
        &(CLEAR_RECENT, "Clear Menu", None),
        !recent.is_empty(),
    ));
    let doc = |i: usize| item(&FILE_AFTER_RECENT[i], true);
    vec![
        Menu {
            id: "app",
            label: "Mallard",
            entries: vec![
                item(&ABOUT, true),
                Separator,
                item(&SETTINGS, true),
                Separator,
                Entry::System(Services),
                Separator,
                Entry::System(Hide),
                Entry::System(HideOthers),
                Entry::System(ShowAll),
                Separator,
                item(&QUIT, true),
            ],
        },
        Menu {
            id: "file",
            label: "File",
            entries: vec![
                item(&FILE[0], true),
                item(&FILE[1], true),
                Menu {
                    id: "open_recent",
                    label: "Open Recent",
                    entries: open_recent,
                },
                Separator,
                doc(0),
                doc(1),
                doc(2),
                doc(3),
                Separator,
                doc(4),
                doc(5),
                Separator,
                doc(6),
            ],
        },
        Menu {
            id: "edit",
            label: "Edit",
            entries: vec![
                item(&EDIT[0], true),
                item(&EDIT[1], true),
                Separator,
                Entry::System(Cut),
                Entry::System(Copy),
                Entry::System(Paste),
                Entry::System(SelectAll),
            ],
        },
        Menu {
            id: "view",
            label: "View",
            entries: vec![
                Entry::Check(SHOW_MESH.0, SHOW_MESH.1, SHOW_MESH.2),
                item(&VIEW[0], true),
                Separator,
                item(&VIEW[1], true),
                Separator,
            ]
            .into_iter()
            .chain(PAGES.iter().map(|p| item(p, true)))
            .chain([Separator, Entry::System(Fullscreen)])
            .collect(),
        },
        Menu {
            id: WINDOW_SUBMENU_ID,
            label: "Window",
            entries: vec![
                Entry::System(Minimize),
                Entry::System(Zoom),
                Separator,
                Entry::System(BringAllToFront),
            ],
        },
        Menu {
            id: HELP_SUBMENU_ID,
            label: "Help",
            entries: vec![item(&(DOCS, "Mallard Documentation", None), true)],
        },
    ]
}

/// Every item id of a menu bar, submenus included.
#[cfg(test)]
pub fn ids(entries: &[Entry]) -> Vec<String> {
    let mut out = Vec::new();
    for e in entries {
        match e {
            Entry::Item { id, .. } => out.push(id.clone()),
            Entry::Check(id, _, _) => out.push((*id).into()),
            Entry::Menu { id, entries, .. } => {
                out.push((*id).into());
                out.extend(ids(entries));
            }
            _ => {}
        }
    }
    out
}

/// The items the shell changes as the page's document changes.
pub struct Handles<R: Runtime> {
    pub undo: Option<MenuItem<R>>,
    pub redo: Option<MenuItem<R>>,
    pub mesh: Option<CheckMenuItem<R>>,
    pub page_items: Vec<MenuItem<R>>,
}

impl<R: Runtime> Handles<R> {
    /// Document items work while a GUI page is open; Open, New Project and Quit always do.
    pub fn set_page(&self, open: bool) {
        for item in &self.page_items {
            let _ = item.set_enabled(open);
        }
        if let Some(mesh) = &self.mesh {
            let _ = mesh.set_enabled(open);
        }
    }

    pub fn set_document(&self, can_undo: bool, can_redo: bool, mesh: bool) {
        if let Some(i) = &self.undo {
            let _ = i.set_enabled(can_undo);
        }
        if let Some(i) = &self.redo {
            let _ = i.set_enabled(can_redo);
        }
        if let Some(i) = &self.mesh {
            let _ = i.set_checked(mesh);
        }
    }
}

fn kind<R: Runtime>(
    app: &AppHandle<R>,
    entry: &Entry,
    handles: &mut Handles<R>,
) -> tauri::Result<MenuItemKind<R>> {
    Ok(match entry {
        Entry::Item {
            id,
            label,
            accel,
            enabled,
        } => {
            let item = MenuItem::with_id(app, id.as_str(), label, *enabled, *accel)?;
            match id.as_str() {
                "undo" => handles.undo = Some(item.clone()),
                "redo" => handles.redo = Some(item.clone()),
                _ => {}
            }
            if page_commands().contains(&id.as_str())
                && id != QUIT.0
                && !FILE.iter().any(|f| f.0 == id)
            {
                handles.page_items.push(item.clone());
            }
            MenuItemKind::MenuItem(item)
        }
        Entry::Check(id, label, accel) => {
            let item = CheckMenuItem::with_id(app, *id, *label, true, false, *accel)?;
            handles.mesh = Some(item.clone());
            MenuItemKind::Check(item)
        }
        Entry::Separator => MenuItemKind::Predefined(PredefinedMenuItem::separator(app)?),
        Entry::System(s) => MenuItemKind::Predefined(system(app, *s)?),
        Entry::Menu { id, label, entries } => {
            let menu = Submenu::with_id(app, *id, *label, true)?;
            for e in entries {
                menu.append(&kind(app, e, handles)?)?;
            }
            MenuItemKind::Submenu(menu)
        }
    })
}

fn system<R: Runtime>(app: &AppHandle<R>, s: System) -> tauri::Result<PredefinedMenuItem<R>> {
    match s {
        System::Services => PredefinedMenuItem::services(app, None),
        System::Hide => PredefinedMenuItem::hide(app, None),
        System::HideOthers => PredefinedMenuItem::hide_others(app, None),
        System::ShowAll => PredefinedMenuItem::show_all(app, None),
        System::Cut => PredefinedMenuItem::cut(app, None),
        System::Copy => PredefinedMenuItem::copy(app, None),
        System::Paste => PredefinedMenuItem::paste(app, None),
        System::SelectAll => PredefinedMenuItem::select_all(app, None),
        System::Fullscreen => PredefinedMenuItem::fullscreen(app, None),
        System::Minimize => PredefinedMenuItem::minimize(app, None),
        System::Zoom => PredefinedMenuItem::maximize(app, None),
        System::BringAllToFront => PredefinedMenuItem::bring_all_to_front(app, None),
    }
}

/// The menu bar from `spec`, and the items the shell updates.
pub fn build<R: Runtime>(
    app: &AppHandle<R>,
    recent: &[PathBuf],
) -> tauri::Result<(Menu<R>, Handles<R>)> {
    let mut handles = Handles {
        undo: None,
        redo: None,
        mesh: None,
        page_items: Vec::new(),
    };
    let menu = Menu::new(app)?;
    for entry in spec(recent) {
        menu.append(&kind(app, &entry, &mut handles)?)?;
    }
    Ok((menu, handles))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn temp_inputs(test: &str, n: usize) -> (PathBuf, Vec<PathBuf>) {
        let root = std::env::temp_dir().join(format!("mallard-menu-{test}-{}", std::process::id()));
        let files = (0..n)
            .map(|i| {
                let dir = root.join(format!("case{i}"));
                std::fs::create_dir_all(&dir).unwrap();
                let f = dir.join("input.toml");
                std::fs::write(&f, "").unwrap();
                f
            })
            .collect();
        (root, files)
    }

    #[test]
    fn every_menu_command_reaches_the_page_and_open_recent_lists_the_inputs() {
        let (root, files) = temp_inputs("ids", 2);
        let ids = ids(&spec(&files));
        for command in page_commands() {
            assert!(
                ids.iter().any(|i| i == command),
                "{command}: not in the menu"
            );
            assert!(page_call(command, &files).is_some());
        }
        for id in ["recent:0", "recent:1", CLEAR_RECENT, DOCS] {
            assert!(ids.iter().any(|i| i == id), "{id}: not in the menu");
        }
        assert_eq!(
            page_call("recent:1", &files),
            Some((
                "open_recent".into(),
                Some(files[1].to_string_lossy().into_owned())
            ))
        );
        assert_eq!(page_call("recent:2", &files), None);
        assert_eq!(page_call(DOCS, &files), None); // the shell's own
        assert_eq!(
            page_call(SETTINGS.0, &files),
            Some(("settings".into(), None))
        );
        assert_eq!(page_call(ABOUT.0, &files), Some(("about".into(), None)));
        let _ = std::fs::remove_dir_all(root);
    }

    fn find(entries: &[Entry], want: &str) -> Option<Entry> {
        entries.iter().find_map(|e| match e {
            Entry::Item { id, .. } if id == want => Some(e.clone()),
            Entry::Menu { entries, .. } => find(entries, want),
            _ => None,
        })
    }

    fn accels(entries: &[Entry], out: &mut Vec<(String, &'static str)>) {
        for e in entries {
            match e {
                Entry::Item {
                    id, accel: Some(a), ..
                } => out.push((id.clone(), a)),
                Entry::Check(id, _, Some(a)) => out.push(((*id).into(), a)),
                Entry::Menu { entries, .. } => accels(entries, out),
                _ => {}
            }
        }
    }

    #[test]
    fn the_pages_go_by_number_in_the_rails_order_and_no_shortcut_is_taken_twice() {
        let mut all = Vec::new();
        accels(&spec(&[]), &mut all);
        let pages: Vec<_> = all
            .iter()
            .filter(|(id, _)| id.starts_with("page_"))
            .cloned()
            .collect();
        let want: Vec<_> = PAGES
            .iter()
            .enumerate()
            .map(|(i, (id, _, _))| (id.to_string(), format!("CmdOrCtrl+{}", i + 1)))
            .collect();
        assert_eq!(
            pages
                .iter()
                .map(|(id, a)| (id.clone(), a.to_string()))
                .collect::<Vec<_>>(),
            want
        );
        let mut keys: Vec<_> = all.iter().map(|(_, a)| *a).collect();
        keys.sort_unstable();
        let n = keys.len();
        keys.dedup();
        assert_eq!(keys.len(), n, "a shortcut is taken twice: {all:?}");
    }

    #[test]
    fn the_document_shortcuts_are_the_platforms() {
        let menu = spec(&[]);
        let accel = |id: &str| match find(&menu, id) {
            Some(Entry::Item { accel, .. }) => accel,
            _ => panic!("{id}: not in the menu"),
        };
        assert_eq!(accel("save"), Some("CmdOrCtrl+S"));
        assert_eq!(accel("save_as"), Some("CmdOrCtrl+Shift+S"));
        assert_eq!(accel("open"), Some("CmdOrCtrl+O"));
        assert_eq!(accel("close"), Some("CmdOrCtrl+W"));
        assert_eq!(accel("undo"), Some("CmdOrCtrl+Z"));
        assert_eq!(accel("redo"), Some("CmdOrCtrl+Shift+Z"));
        assert_eq!(accel("quit"), Some("CmdOrCtrl+Q"));
        assert_eq!(accel(SETTINGS.0), Some("CmdOrCtrl+,"));
        assert_eq!(accel("import_mesh"), Some("CmdOrCtrl+Shift+I"));
        assert_eq!(accel("paraview"), Some("CmdOrCtrl+Shift+P"));
        // With no recent inputs, Clear Menu is there but off.
        assert!(matches!(
            find(&menu, CLEAR_RECENT),
            Some(Entry::Item { enabled: false, .. })
        ));
    }

    #[test]
    fn dropped_projects_open_and_dropped_meshes_become_the_mesh() {
        let (root, files) = temp_inputs("drop", 1);
        let mesh = root.join("wing.CGNS");
        std::fs::write(&mesh, "").unwrap();
        let other = root.join("notes.txt");
        std::fs::write(&other, "").unwrap();
        let call = |paths: &[PathBuf]| dropped_call(paths).map(|(c, a)| (c, PathBuf::from(a)));
        assert_eq!(
            call(&[other.clone(), mesh.clone()]),
            Some(("use_mesh".into(), mesh.clone()))
        );
        assert_eq!(
            call(&[files[0].clone(), mesh]),
            Some(("open_recent".into(), files[0].clone()))
        );
        let dir = files[0].parent().unwrap().to_path_buf();
        assert_eq!(
            call(std::slice::from_ref(&dir)),
            Some(("open_recent".into(), dir))
        );
        assert_eq!(call(&[other, root.join("missing.msh")]), None);
        let _ = std::fs::remove_dir_all(root);
    }

    #[test]
    fn page_scripts_quote_their_arguments() {
        assert_eq!(
            page_script("save", None),
            "window.mallardMenu && window.mallardMenu(\"save\")"
        );
        let script = page_script("open_recent", Some("/a/\"b\");alert(1)//"));
        assert!(script.ends_with("(\"open_recent\", \"/a/\\\"b\\\");alert(1)//\")"));
    }

    #[test]
    fn recent_inputs_skip_missing_files_and_repeats() {
        let (root, files) = temp_inputs("recent", 2);
        let text = format!(
            "{}\n\n/nonexistent/input.toml\n{}\n{}\n",
            files[1].display(),
            files[0].display(),
            files[1].display()
        );
        assert_eq!(read_recent(&text), vec![files[1].clone(), files[0].clone()]);
        assert_eq!(recent_label(&files[0]), "case0 — input.toml");
        let settings = format!(
            "schema = \"mallard-gui-settings\"\nrecent = [{:?}, \"/nonexistent/input.toml\", {:?}]\n\n[runs]\nthreads = 4\n",
            files[1].display().to_string(),
            files[0].display().to_string()
        );
        assert_eq!(
            recent_in_settings(&settings),
            Some(vec![files[1].clone(), files[0].clone()])
        );
        assert_eq!(recent_in_settings("[runs]\nthreads = 4\n"), None);
        assert_eq!(recent_in_settings("recent = = 1"), None);
        let cleared = settings_without_recent(&settings).unwrap();
        assert!(!cleared.contains("recent") && cleared.contains("threads = 4"));
        assert_eq!(settings_without_recent("recent = = 1"), None);
        let _ = std::fs::remove_dir_all(root);
    }
}
