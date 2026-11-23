fn main() {
    // The bridge's commands: granted at run time to the GUI's own origin only (main.rs, allow_gui)
    let app = tauri_build::AppManifest::new().commands(&[
        "notify",
        "set_document",
        "choose_path",
        "open_project",
        "close_project",
        "quit",
        "reveal",
        "open_url",
    ]);
    tauri_build::try_build(tauri_build::Attributes::new().app_manifest(app))
        .expect("tauri-build failed");
}
