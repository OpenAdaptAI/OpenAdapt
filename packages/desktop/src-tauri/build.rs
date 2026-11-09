use std::env;
use std::fs;
use std::path::PathBuf;

/// The first comment line of the development launcher that
/// `scripts/dev_sidecar.py` writes. Keep it in sync with `LAUNCHER_MARKER`.
const DEV_LAUNCHER_MARKER: &str =
    "# Development engine launcher written by scripts/dev_sidecar.py.";

/// Refuse a release build that would package the development launcher.
///
/// The launcher runs Python from one checkout's `.venv` by absolute path, so
/// an installer built around it fails on every other computer. Debug builds
/// (`npm run tauri dev`) are where the launcher belongs and are not checked.
fn refuse_dev_launcher_in_release() {
    if env::var("PROFILE").as_deref() != Ok("release") {
        return;
    }
    let target = env::var("TARGET").unwrap_or_default();
    let suffix = if target.contains("windows") {
        ".exe"
    } else {
        ""
    };
    let sidecar = PathBuf::from("binaries").join(format!("openadapt-engine-{target}{suffix}"));
    println!("cargo:rerun-if-changed={}", sidecar.display());
    let Ok(bytes) = fs::read(&sidecar) else {
        return;
    };
    let head = String::from_utf8_lossy(&bytes[..bytes.len().min(512)]);
    if head.contains(DEV_LAUNCHER_MARKER) {
        panic!(
            "{} is the development engine launcher from scripts/dev_sidecar.py. \
             A release build needs the frozen engine: run \
             `uv run python scripts/dev_sidecar.py --frozen` first.",
            sidecar.display()
        );
    }
}

fn main() {
    refuse_dev_launcher_in_release();
    tauri_build::build()
}
