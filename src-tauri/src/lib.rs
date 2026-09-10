use tauri::Manager;
use tauri_plugin_updater::UpdaterExt;

/// Check for a newer version and install it in the background.
///
/// Clients should never be asked to download an installer again, so this runs
/// on its own at startup rather than behind a menu item nobody opens. The
/// update is verified against the public key compiled into this binary before
/// anything is installed — that signature, not a code-signing certificate, is
/// what stops someone who controls the download URL pushing arbitrary code to
/// every client machine.
///
/// Any failure here is deliberately silent. A client whose network blocks
/// GitHub, or who is offline, should still get a working dashboard; they
/// simply stay on the version they have.
async fn update_in_background(app: tauri::AppHandle) {
    let updater = match app.updater() {
        Ok(updater) => updater,
        Err(error) => {
            log::warn!("updater unavailable: {error}");
            return;
        }
    };

    match updater.check().await {
        Ok(Some(update)) => {
            let version = update.version.clone();
            log::info!("installing update {version}");
            // Downloaded by the app itself, so the file carries no
            // Mark-of-the-Web and SmartScreen does not gate it the way it
            // gates an installer fetched through a browser.
            if let Err(error) = update.download_and_install(|_, _| {}, || {}).await {
                log::warn!("update {version} failed to install: {error}");
            }
        }
        Ok(None) => log::info!("already on the latest version"),
        Err(error) => log::warn!("update check failed: {error}"),
    }
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_updater::Builder::new().build())
        .plugin(tauri_plugin_process::init())
        .setup(|app| {
            // Logging in release builds too, not just debug. Every message
            // this file writes is about the updater, and the updater fails
            // silently by design — so without this a client whose app has
            // quietly stopped updating leaves no evidence of why, which is
            // exactly the situation the log is for.
            app.handle().plugin(
                tauri_plugin_log::Builder::default()
                    .level(log::LevelFilter::Info)
                    .target(tauri_plugin_log::Target::new(
                        tauri_plugin_log::TargetKind::LogDir { file_name: None },
                    ))
                    .build(),
            )?;

            // Spawned rather than awaited: the window opens immediately and
            // the check happens behind it.
            let handle = app.handle().clone();
            tauri::async_runtime::spawn(async move {
                update_in_background(handle).await;
            });

            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
