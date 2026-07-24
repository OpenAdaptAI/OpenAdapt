//! First-use provisioning for the separately licensed FFmpeg runtime.
//!
//! FFmpeg is deliberately not linked into the MIT application and is not
//! bundled in the Tauri installer or frozen Python sidecar. A release-reviewed
//! manifest pins one archive and every extracted file for each supported
//! target. The archive is downloaded on first use, verified before extraction,
//! probed as a separate process, and atomically promoted into app-local data.
//! The Python sidecar receives only the final absolute executable path.

use std::collections::HashSet;
use std::fs::{self, File};
use std::io::{Read, Write};
use std::path::{Component, Path, PathBuf};
use std::process::Stdio;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use tauri::{AppHandle, Emitter, Manager, State};
use tokio::io::AsyncWriteExt;
use tokio::process::Command;
use url::Url;
use zip::ZipArchive;

const EMBEDDED_MANIFEST: &str = include_str!("../ffmpeg-runtime-manifest.json");
const MANIFEST_SCHEMA_VERSION: u32 = 1;
const PROVISION_TIMEOUT: Duration = Duration::from_secs(10 * 60);
const PROBE_TIMEOUT: Duration = Duration::from_secs(15);
const COMPLETE_MARKER: &str = ".complete.json";
const EVENT_NAME: &str = "runtime://ffmpeg-status";

#[derive(Debug, Clone, Deserialize)]
struct RuntimeManifest {
    schema_version: u32,
    runtime: String,
    runtime_version: String,
    artifacts: Vec<RuntimeArtifact>,
}

#[derive(Debug, Clone, Deserialize)]
struct RuntimeArtifact {
    target: String,
    build_id: String,
    url: String,
    archive_sha256: String,
    archive_max_bytes: u64,
    files: Vec<RuntimeFile>,
    probe: RuntimeProbe,
    source: SourceProvenance,
    license: LicenseProvenance,
}

#[derive(Debug, Clone, Deserialize)]
struct RuntimeFile {
    member: String,
    destination: String,
    sha256: String,
    max_bytes: u64,
    #[serde(default)]
    role: Option<RuntimeRole>,
}

#[derive(Debug, Clone, Copy, Deserialize, PartialEq, Eq, Hash)]
#[serde(rename_all = "snake_case")]
enum RuntimeRole {
    Ffmpeg,
    Ffprobe,
}

#[derive(Debug, Clone, Deserialize)]
struct RuntimeProbe {
    version_contains: String,
    ffprobe_version_contains: String,
    required_build_flags: Vec<String>,
    forbidden_build_flags: Vec<String>,
    required_encoders: Vec<String>,
    required_muxers: Vec<String>,
}

#[derive(Debug, Clone, Deserialize)]
struct SourceProvenance {
    url: String,
    sha256: String,
    signature_url: String,
    signing_key_fingerprint: String,
    build_workflow: String,
}

#[derive(Debug, Clone, Deserialize)]
struct LicenseProvenance {
    expression: String,
    license_destination: String,
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum ProvisionPhase {
    Checking,
    Downloading,
    Verifying,
    Ready,
    Error,
    Unavailable,
}

#[derive(Debug, Clone, Serialize)]
pub struct FfmpegStatus {
    pub phase: ProvisionPhase,
    pub source: String,
    pub runtime_version: String,
    pub target: String,
    pub path: Option<String>,
    pub ffprobe_path: Option<String>,
    pub detail: Option<String>,
}

#[derive(Debug, Serialize, Deserialize)]
struct CompleteMarker {
    schema_version: u32,
    target: String,
    build_id: String,
    archive_sha256: String,
    ffmpeg_sha256: String,
    ffprobe_sha256: String,
    source_sha256: String,
    installed_at_unix: u64,
}

#[derive(Debug)]
struct PreparedInstall {
    staging_dir: PathBuf,
    paths: RuntimePaths,
}

#[derive(Debug, Clone)]
pub struct RuntimePaths {
    pub ffmpeg: PathBuf,
    pub ffprobe: PathBuf,
}

pub struct FfmpegManager {
    tools_root: PathBuf,
    artifact: Option<RuntimeArtifact>,
    override_paths: Option<RuntimePaths>,
    effective_paths: Option<RuntimePaths>,
    status: Mutex<FfmpegStatus>,
    in_flight: AtomicBool,
}

pub struct FfmpegHandle(pub Arc<FfmpegManager>);

impl FfmpegManager {
    pub fn from_app(app: &AppHandle) -> Result<Self, String> {
        let tools_root = app
            .path()
            .app_local_data_dir()
            .map_err(|error| format!("cannot resolve app-local data directory: {error}"))?
            .join("tools")
            .join("ffmpeg");
        let ffmpeg_override = std::env::var_os("OPENADAPT_FFMPEG_PATH").map(PathBuf::from);
        let ffprobe_override = std::env::var_os("OPENADAPT_FFPROBE_PATH").map(PathBuf::from);
        Self::new(tools_root, ffmpeg_override, ffprobe_override)
    }

    fn new(
        tools_root: PathBuf,
        ffmpeg_override: Option<PathBuf>,
        ffprobe_override: Option<PathBuf>,
    ) -> Result<Self, String> {
        let manifest: RuntimeManifest = serde_json::from_str(EMBEDDED_MANIFEST)
            .map_err(|error| format!("invalid embedded FFmpeg manifest: {error}"))?;
        validate_manifest(&manifest)?;

        let target = current_target().to_owned();
        let artifact = manifest
            .artifacts
            .iter()
            .find(|artifact| artifact.target == target)
            .cloned();

        let (override_paths, override_error) =
            validate_override_paths(ffmpeg_override, ffprobe_override);

        let effective_paths = if let Some(paths) = override_paths.as_ref() {
            Some(paths.clone())
        } else {
            artifact.as_ref().and_then(|artifact| {
                let ffmpeg = runtime_file(artifact, RuntimeRole::Ffmpeg).ok()?;
                let ffprobe = runtime_file(artifact, RuntimeRole::Ffprobe).ok()?;
                let root = tools_root.join(&artifact.build_id);
                Some(RuntimePaths {
                    ffmpeg: root.join(&ffmpeg.destination),
                    ffprobe: root.join(&ffprobe.destination),
                })
            })
        };

        let status = if let Some(error) = override_error {
            FfmpegStatus {
                phase: ProvisionPhase::Error,
                source: "override".into(),
                runtime_version: manifest.runtime_version.clone(),
                target: target.clone(),
                path: None,
                ffprobe_path: None,
                detail: Some(error),
            }
        } else if override_paths.is_some() {
            FfmpegStatus {
                phase: ProvisionPhase::Checking,
                source: "override".into(),
                runtime_version: manifest.runtime_version.clone(),
                target: target.clone(),
                path: effective_paths
                    .as_ref()
                    .map(|paths| path_display(&paths.ffmpeg)),
                ffprobe_path: effective_paths
                    .as_ref()
                    .map(|paths| path_display(&paths.ffprobe)),
                detail: None,
            }
        } else if artifact.is_some() {
            FfmpegStatus {
                phase: ProvisionPhase::Checking,
                source: "managed".into(),
                runtime_version: manifest.runtime_version.clone(),
                target: target.clone(),
                path: effective_paths
                    .as_ref()
                    .map(|paths| path_display(&paths.ffmpeg)),
                ffprobe_path: effective_paths
                    .as_ref()
                    .map(|paths| path_display(&paths.ffprobe)),
                detail: None,
            }
        } else {
            FfmpegStatus {
                phase: ProvisionPhase::Unavailable,
                source: "managed".into(),
                runtime_version: manifest.runtime_version.clone(),
                target: target.clone(),
                path: None,
                ffprobe_path: None,
                detail: Some(
                    "No release-reviewed FFmpeg runtime is published for this target. \
                     Set OPENADAPT_FFMPEG_PATH and OPENADAPT_FFPROBE_PATH to \
                     absolute local executables."
                        .into(),
                ),
            }
        };

        Ok(Self {
            tools_root,
            artifact,
            override_paths,
            effective_paths,
            status: Mutex::new(status),
            in_flight: AtomicBool::new(false),
        })
    }

    pub fn effective_paths(&self) -> Option<RuntimePaths> {
        self.effective_paths.clone()
    }

    pub fn status(&self) -> FfmpegStatus {
        self.status.lock().unwrap().clone()
    }

    fn set_status(&self, app: &AppHandle, phase: ProvisionPhase, detail: Option<String>) {
        let mut status = self.status.lock().unwrap();
        status.phase = phase;
        status.detail = detail;
        let payload = status.clone();
        drop(status);
        let _ = app.emit(EVENT_NAME, payload);
    }

    async fn ensure_ready(&self, app: &AppHandle) -> Result<(), String> {
        if let Some(paths) = self.override_paths.as_ref() {
            self.set_status(app, ProvisionPhase::Verifying, None);
            probe_executables(paths, &override_probe()).await?;
            self.set_status(app, ProvisionPhase::Ready, None);
            return Ok(());
        }

        let artifact = self
            .artifact
            .as_ref()
            .ok_or_else(|| "no managed FFmpeg runtime is available for this target".to_owned())?;
        let final_dir = self.tools_root.join(&artifact.build_id);
        if cache_is_valid(&final_dir, artifact)? {
            let paths = runtime_paths(&final_dir, artifact)?;
            self.set_status(app, ProvisionPhase::Verifying, None);
            probe_executables(&paths, &artifact.probe).await?;
            self.set_status(app, ProvisionPhase::Ready, None);
            return Ok(());
        }

        self.set_status(app, ProvisionPhase::Downloading, None);
        fs::create_dir_all(&self.tools_root)
            .map_err(|error| format!("cannot create FFmpeg tools directory: {error}"))?;
        let archive_path = unique_sibling(&self.tools_root, "download", "zip");
        if let Err(error) = download_archive(artifact, &archive_path).await {
            let _ = tokio::fs::remove_file(&archive_path).await;
            return Err(error);
        }

        self.set_status(app, ProvisionPhase::Verifying, None);
        let tools_root = self.tools_root.clone();
        let archive_for_extract = archive_path.clone();
        let artifact_for_extract = artifact.clone();
        let prepared = tokio::task::spawn_blocking(move || {
            prepare_install(&tools_root, &archive_for_extract, &artifact_for_extract)
        })
        .await
        .map_err(|error| format!("FFmpeg extraction worker failed: {error}"))??;
        let _ = tokio::fs::remove_file(&archive_path).await;

        if let Err(error) = probe_executables(&prepared.paths, &artifact.probe).await {
            let _ = fs::remove_dir_all(&prepared.staging_dir);
            return Err(error);
        }

        write_complete_marker(&prepared.staging_dir, artifact)?;
        promote_install(
            &self.tools_root,
            &prepared.staging_dir,
            &final_dir,
            &artifact.build_id,
        )?;
        self.set_status(app, ProvisionPhase::Ready, None);
        Ok(())
    }
}

pub fn start_provisioning(app: AppHandle, manager: Arc<FfmpegManager>) {
    if manager.in_flight.swap(true, Ordering::SeqCst) {
        return;
    }
    let manager_for_task = manager.clone();
    tauri::async_runtime::spawn(async move {
        let result =
            tokio::time::timeout(PROVISION_TIMEOUT, manager_for_task.ensure_ready(&app)).await;
        let error = match result {
            Ok(Ok(())) => None,
            Ok(Err(error)) => Some(error),
            Err(_) => Some("FFmpeg provisioning timed out".to_owned()),
        };
        if let Some(error) = error {
            manager_for_task.set_status(&app, ProvisionPhase::Error, Some(error));
        }
        manager_for_task.in_flight.store(false, Ordering::SeqCst);
    });
}

#[tauri::command]
pub fn ffmpeg_status(state: State<'_, FfmpegHandle>) -> FfmpegStatus {
    state.0.status()
}

#[tauri::command]
pub fn retry_ffmpeg_provisioning(app: AppHandle, state: State<'_, FfmpegHandle>) -> FfmpegStatus {
    start_provisioning(app, state.0.clone());
    state.0.status()
}

fn validate_manifest(manifest: &RuntimeManifest) -> Result<(), String> {
    if manifest.schema_version != MANIFEST_SCHEMA_VERSION
        || manifest.runtime != "ffmpeg"
        || manifest.runtime_version.trim().is_empty()
    {
        return Err("unsupported FFmpeg runtime manifest header".into());
    }

    let mut targets = HashSet::new();
    for artifact in &manifest.artifacts {
        if !targets.insert(artifact.target.as_str()) {
            return Err(format!(
                "duplicate FFmpeg artifact target: {}",
                artifact.target
            ));
        }
        validate_segment(&artifact.build_id, "build_id")?;
        validate_sha256(&artifact.archive_sha256)?;
        if artifact.archive_max_bytes == 0 {
            return Err(format!(
                "{} has an empty archive size limit",
                artifact.target
            ));
        }
        let url = Url::parse(&artifact.url)
            .map_err(|error| format!("invalid FFmpeg artifact URL: {error}"))?;
        if url.scheme() != "https" || url.username() != "" || url.password().is_some() {
            return Err("FFmpeg artifact URL must be credential-free HTTPS".into());
        }
        if artifact.files.is_empty() {
            return Err(format!("{} has no pinned files", artifact.target));
        }
        let mut destinations = HashSet::new();
        let mut roles = HashSet::new();
        for file in &artifact.files {
            validate_relative_path(&file.member)?;
            validate_relative_path(&file.destination)?;
            validate_sha256(&file.sha256)?;
            if file.max_bytes == 0 {
                return Err(format!("{} has an empty file size limit", file.destination));
            }
            if !destinations.insert(file.destination.as_str()) {
                return Err(format!(
                    "duplicate FFmpeg destination: {}",
                    file.destination
                ));
            }
            if let Some(role) = file.role {
                if !roles.insert(role) {
                    return Err(format!("duplicate FFmpeg runtime role: {role:?}"));
                }
            }
        }
        if roles != HashSet::from([RuntimeRole::Ffmpeg, RuntimeRole::Ffprobe]) {
            return Err(format!(
                "{} must identify exactly one ffmpeg and one ffprobe executable",
                artifact.target
            ));
        }

        if artifact.probe.version_contains.trim().is_empty()
            || artifact.probe.ffprobe_version_contains.trim().is_empty()
            || artifact.probe.required_encoders.is_empty()
            || artifact.probe.required_muxers.is_empty()
        {
            return Err(format!(
                "{} has an incomplete runtime probe",
                artifact.target
            ));
        }
        let source_url = Url::parse(&artifact.source.url)
            .map_err(|error| format!("invalid FFmpeg source URL: {error}"))?;
        let signature_url = Url::parse(&artifact.source.signature_url)
            .map_err(|error| format!("invalid FFmpeg signature URL: {error}"))?;
        if source_url.scheme() != "https" || signature_url.scheme() != "https" {
            return Err("FFmpeg source and signature URLs must use HTTPS".into());
        }
        validate_sha256(&artifact.source.sha256)?;
        if artifact.source.signing_key_fingerprint.len() != 40
            || !artifact
                .source
                .signing_key_fingerprint
                .bytes()
                .all(|byte| byte.is_ascii_hexdigit())
            || artifact.source.build_workflow.trim().is_empty()
        {
            return Err(format!(
                "{} has incomplete source provenance",
                artifact.target
            ));
        }
        if artifact.license.expression.trim().is_empty()
            || !artifact
                .files
                .iter()
                .any(|file| file.destination == artifact.license.license_destination)
        {
            return Err(format!(
                "{} has incomplete license provenance",
                artifact.target
            ));
        }
    }
    Ok(())
}

fn runtime_file(artifact: &RuntimeArtifact, role: RuntimeRole) -> Result<&RuntimeFile, String> {
    artifact
        .files
        .iter()
        .find(|file| file.role == Some(role))
        .ok_or_else(|| format!("{} does not identify {role:?}", artifact.target))
}

fn runtime_paths(root: &Path, artifact: &RuntimeArtifact) -> Result<RuntimePaths, String> {
    Ok(RuntimePaths {
        ffmpeg: root.join(&runtime_file(artifact, RuntimeRole::Ffmpeg)?.destination),
        ffprobe: root.join(&runtime_file(artifact, RuntimeRole::Ffprobe)?.destination),
    })
}

fn validate_override_paths(
    ffmpeg: Option<PathBuf>,
    ffprobe: Option<PathBuf>,
) -> (Option<RuntimePaths>, Option<String>) {
    let Some(ffmpeg) = ffmpeg else {
        return if ffprobe.is_some() {
            (
                None,
                Some("OPENADAPT_FFPROBE_PATH requires OPENADAPT_FFMPEG_PATH".into()),
            )
        } else {
            (None, None)
        };
    };
    let ffprobe = ffprobe.unwrap_or_else(|| {
        let filename = if cfg!(windows) {
            "ffprobe.exe"
        } else {
            "ffprobe"
        };
        ffmpeg
            .parent()
            .unwrap_or_else(|| Path::new(""))
            .join(filename)
    });
    match (
        validate_override_path(&ffmpeg, "OPENADAPT_FFMPEG_PATH"),
        validate_override_path(&ffprobe, "OPENADAPT_FFPROBE_PATH"),
    ) {
        (Ok(ffmpeg), Ok(ffprobe)) => (Some(RuntimePaths { ffmpeg, ffprobe }), None),
        (Err(error), _) | (_, Err(error)) => (None, Some(error)),
    }
}

fn validate_override_path(path: &Path, variable: &str) -> Result<PathBuf, String> {
    if !path.is_absolute() {
        return Err(format!("{variable} must be absolute"));
    }
    let canonical =
        fs::canonicalize(path).map_err(|error| format!("{variable} is unavailable: {error}"))?;
    let metadata =
        fs::metadata(&canonical).map_err(|error| format!("cannot inspect {variable}: {error}"))?;
    if !metadata.is_file() {
        return Err(format!("{variable} must name a regular file"));
    }
    Ok(canonical)
}

fn current_target() -> &'static str {
    match (std::env::consts::OS, std::env::consts::ARCH) {
        ("macos", "aarch64") => "aarch64-apple-darwin",
        ("macos", "x86_64") => "x86_64-apple-darwin",
        ("windows", "x86_64") => "x86_64-pc-windows-msvc",
        ("linux", "x86_64") => "x86_64-unknown-linux-gnu",
        _ => "unsupported",
    }
}

fn validate_segment(value: &str, label: &str) -> Result<(), String> {
    if value.is_empty()
        || value == "."
        || value == ".."
        || value.contains('/')
        || value.contains('\\')
    {
        return Err(format!("invalid FFmpeg {label}: {value}"));
    }
    Ok(())
}

fn validate_relative_path(value: &str) -> Result<(), String> {
    if value.is_empty() || value.contains('\\') {
        return Err(format!("invalid FFmpeg archive path: {value}"));
    }
    let path = Path::new(value);
    if path.is_absolute()
        || path
            .components()
            .any(|component| !matches!(component, Component::Normal(_)))
    {
        return Err(format!("unsafe FFmpeg archive path: {value}"));
    }
    Ok(())
}

fn validate_sha256(value: &str) -> Result<(), String> {
    if value.len() != 64 || !value.bytes().all(|byte| byte.is_ascii_hexdigit()) {
        return Err(format!("invalid SHA-256 value: {value}"));
    }
    Ok(())
}

async fn download_archive(artifact: &RuntimeArtifact, destination: &Path) -> Result<(), String> {
    let client = download_client()?;
    let mut response = client
        .get(&artifact.url)
        .send()
        .await
        .map_err(|error| format!("FFmpeg download failed: {error}"))?
        .error_for_status()
        .map_err(|error| format!("FFmpeg download failed: {error}"))?;
    if response
        .content_length()
        .is_some_and(|length| length > artifact.archive_max_bytes)
    {
        return Err("FFmpeg archive exceeds its pinned size limit".into());
    }

    let mut file = tokio::fs::File::create(destination)
        .await
        .map_err(|error| format!("cannot create FFmpeg download: {error}"))?;
    let mut hasher = Sha256::new();
    let mut written = 0_u64;
    while let Some(chunk) = response
        .chunk()
        .await
        .map_err(|error| format!("FFmpeg download stream failed: {error}"))?
    {
        written = written
            .checked_add(chunk.len() as u64)
            .ok_or_else(|| "FFmpeg archive size overflow".to_owned())?;
        if written > artifact.archive_max_bytes {
            return Err("FFmpeg archive exceeds its pinned size limit".into());
        }
        hasher.update(&chunk);
        file.write_all(&chunk)
            .await
            .map_err(|error| format!("cannot write FFmpeg download: {error}"))?;
    }
    file.sync_all()
        .await
        .map_err(|error| format!("cannot sync FFmpeg download: {error}"))?;
    let digest = hex::encode(hasher.finalize());
    if digest != artifact.archive_sha256.to_ascii_lowercase() {
        return Err(format!(
            "FFmpeg archive hash mismatch: expected {}, got {digest}",
            artifact.archive_sha256
        ));
    }
    Ok(())
}

fn download_client() -> Result<reqwest::Client, String> {
    if rustls::crypto::CryptoProvider::get_default().is_none() {
        rustls::crypto::ring::default_provider()
            .install_default()
            .map_err(|_| "cannot initialize FFmpeg downloader cryptography".to_owned())?;
    }
    reqwest::Client::builder()
        .https_only(true)
        .connect_timeout(Duration::from_secs(20))
        .timeout(PROVISION_TIMEOUT)
        .user_agent("OpenAdapt-Desktop/ffmpeg-provisioner")
        .build()
        .map_err(|error| format!("cannot initialize FFmpeg downloader: {error}"))
}

fn prepare_install(
    tools_root: &Path,
    archive_path: &Path,
    artifact: &RuntimeArtifact,
) -> Result<PreparedInstall, String> {
    let staging_dir = unique_sibling(tools_root, &artifact.build_id, "staging");
    fs::create_dir_all(&staging_dir)
        .map_err(|error| format!("cannot create FFmpeg staging directory: {error}"))?;

    let result = (|| {
        let archive_file = File::open(archive_path)
            .map_err(|error| format!("cannot open FFmpeg archive: {error}"))?;
        let mut archive = ZipArchive::new(archive_file)
            .map_err(|error| format!("invalid FFmpeg ZIP archive: {error}"))?;
        reject_unexpected_archive_members(&mut archive, artifact)?;

        for pinned in &artifact.files {
            let mut member = archive
                .by_name(&pinned.member)
                .map_err(|error| format!("missing FFmpeg member {}: {error}", pinned.member))?;
            if member.is_dir() || member.size() > pinned.max_bytes {
                return Err(format!(
                    "FFmpeg member {} violates its size/type contract",
                    pinned.member
                ));
            }
            let destination = staging_dir.join(&pinned.destination);
            if let Some(parent) = destination.parent() {
                fs::create_dir_all(parent)
                    .map_err(|error| format!("cannot stage FFmpeg member: {error}"))?;
            }
            let mut output = File::create(&destination)
                .map_err(|error| format!("cannot create staged FFmpeg member: {error}"))?;
            let mut hasher = Sha256::new();
            let mut copied = 0_u64;
            let mut buffer = [0_u8; 64 * 1024];
            loop {
                let count = member
                    .read(&mut buffer)
                    .map_err(|error| format!("cannot read FFmpeg member: {error}"))?;
                if count == 0 {
                    break;
                }
                copied = copied
                    .checked_add(count as u64)
                    .ok_or_else(|| "FFmpeg member size overflow".to_owned())?;
                if copied > pinned.max_bytes {
                    return Err(format!(
                        "FFmpeg member {} exceeds its size limit",
                        pinned.member
                    ));
                }
                hasher.update(&buffer[..count]);
                output
                    .write_all(&buffer[..count])
                    .map_err(|error| format!("cannot write FFmpeg member: {error}"))?;
            }
            output
                .sync_all()
                .map_err(|error| format!("cannot sync FFmpeg member: {error}"))?;
            let digest = hex::encode(hasher.finalize());
            if digest != pinned.sha256.to_ascii_lowercase() {
                return Err(format!(
                    "FFmpeg member hash mismatch for {}: expected {}, got {digest}",
                    pinned.member, pinned.sha256
                ));
            }
        }

        let paths = runtime_paths(&staging_dir, artifact)?;
        make_executable(&paths.ffmpeg)?;
        make_executable(&paths.ffprobe)?;
        Ok(PreparedInstall {
            staging_dir: staging_dir.clone(),
            paths,
        })
    })();

    if result.is_err() {
        let _ = fs::remove_dir_all(&staging_dir);
    }
    result
}

fn reject_unexpected_archive_members(
    archive: &mut ZipArchive<File>,
    artifact: &RuntimeArtifact,
) -> Result<(), String> {
    let expected: HashSet<&str> = artifact
        .files
        .iter()
        .map(|file| file.member.as_str())
        .collect();
    let expected_directories: HashSet<String> = artifact
        .files
        .iter()
        .flat_map(|file| {
            let mut ancestors = Vec::new();
            let mut path = PathBuf::new();
            for component in Path::new(&file.member).components() {
                path.push(component.as_os_str());
                if path != Path::new(&file.member) {
                    ancestors.push(format!("{}/", path.to_string_lossy()));
                }
            }
            ancestors
        })
        .collect();

    for index in 0..archive.len() {
        let member = archive
            .by_index(index)
            .map_err(|error| format!("cannot inspect FFmpeg archive: {error}"))?;
        let name = member.name();
        validate_relative_path(name.trim_end_matches('/'))?;
        if member.is_dir() {
            if !expected_directories.contains(name) {
                return Err(format!("unexpected FFmpeg archive directory: {name}"));
            }
        } else if !expected.contains(name) {
            return Err(format!("unexpected FFmpeg archive member: {name}"));
        }
    }
    Ok(())
}

fn make_executable(path: &Path) -> Result<(), String> {
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let mut permissions = fs::metadata(path)
            .map_err(|error| format!("cannot inspect staged FFmpeg: {error}"))?
            .permissions();
        permissions.set_mode(0o755);
        fs::set_permissions(path, permissions)
            .map_err(|error| format!("cannot mark staged FFmpeg executable: {error}"))?;
    }
    Ok(())
}

async fn probe_executables(paths: &RuntimePaths, probe: &RuntimeProbe) -> Result<(), String> {
    let version = checked_output(&paths.ffmpeg, &["-version"]).await?;
    if !version.contains(&probe.version_contains) {
        return Err(format!(
            "FFmpeg version probe did not contain {:?}",
            probe.version_contains
        ));
    }

    let buildconf = checked_output(&paths.ffmpeg, &["-buildconf"]).await?;
    for required in &probe.required_build_flags {
        if !buildconf.contains(required) {
            return Err(format!("FFmpeg is missing required build flag {required}"));
        }
    }
    for forbidden in &probe.forbidden_build_flags {
        if buildconf.contains(forbidden) {
            return Err(format!("FFmpeg contains forbidden build flag {forbidden}"));
        }
    }

    let encoders = checked_output(&paths.ffmpeg, &["-hide_banner", "-encoders"]).await?;
    let encoder_names = capability_names(&encoders);
    for required in &probe.required_encoders {
        if !encoder_names.contains(required.as_str()) {
            return Err(format!("FFmpeg is missing required encoder {required}"));
        }
    }

    let muxers = checked_output(&paths.ffmpeg, &["-hide_banner", "-muxers"]).await?;
    let muxer_names = capability_names(&muxers);
    for required in &probe.required_muxers {
        if !muxer_names.contains(required.as_str()) {
            return Err(format!("FFmpeg is missing required muxer {required}"));
        }
    }
    let probe_version = checked_output(&paths.ffprobe, &["-version"]).await?;
    if !probe_version.contains(&probe.ffprobe_version_contains) {
        return Err(format!(
            "ffprobe version probe did not contain {:?}",
            probe.ffprobe_version_contains
        ));
    }
    let probe_buildconf = checked_output(&paths.ffprobe, &["-buildconf"]).await?;
    for required in &probe.required_build_flags {
        if !probe_buildconf.contains(required) {
            return Err(format!("ffprobe is missing required build flag {required}"));
        }
    }
    for forbidden in &probe.forbidden_build_flags {
        if probe_buildconf.contains(forbidden) {
            return Err(format!("ffprobe contains forbidden build flag {forbidden}"));
        }
    }
    Ok(())
}

async fn checked_output(path: &Path, args: &[&str]) -> Result<String, String> {
    let mut command = Command::new(path);
    command.args(args).stdin(Stdio::null()).kill_on_drop(true);
    let output = tokio::time::timeout(PROBE_TIMEOUT, command.output())
        .await
        .map_err(|_| format!("FFmpeg probe timed out: {}", args.join(" ")))?
        .map_err(|error| format!("cannot execute FFmpeg probe: {error}"))?;
    if !output.status.success() {
        return Err(format!(
            "FFmpeg probe failed ({}): {}",
            args.join(" "),
            String::from_utf8_lossy(&output.stderr).trim()
        ));
    }
    Ok(format!(
        "{}\n{}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    ))
}

fn capability_names(output: &str) -> HashSet<&str> {
    output
        .lines()
        .filter_map(|line| {
            let mut fields = line.split_whitespace();
            let flags = fields.next()?;
            let name = fields.next()?;
            if flags
                .bytes()
                .all(|byte| byte.is_ascii_alphabetic() || byte == b'.')
            {
                Some(name)
            } else {
                None
            }
        })
        .collect()
}

fn override_probe() -> RuntimeProbe {
    RuntimeProbe {
        version_contains: "ffmpeg version".into(),
        ffprobe_version_contains: "ffprobe version".into(),
        required_build_flags: Vec::new(),
        forbidden_build_flags: Vec::new(),
        required_encoders: vec!["mpeg4".into()],
        required_muxers: vec!["mp4".into()],
    }
}

fn cache_is_valid(final_dir: &Path, artifact: &RuntimeArtifact) -> Result<bool, String> {
    if !final_dir.is_dir() {
        return Ok(false);
    }
    let marker_path = final_dir.join(COMPLETE_MARKER);
    let marker: CompleteMarker = match fs::read(&marker_path)
        .ok()
        .and_then(|bytes| serde_json::from_slice(&bytes).ok())
    {
        Some(marker) => marker,
        None => return Ok(false),
    };
    let ffmpeg = runtime_file(artifact, RuntimeRole::Ffmpeg)?;
    let ffprobe = runtime_file(artifact, RuntimeRole::Ffprobe)?;
    if marker.schema_version != MANIFEST_SCHEMA_VERSION
        || marker.target != artifact.target
        || marker.build_id != artifact.build_id
        || marker.archive_sha256 != artifact.archive_sha256
        || marker.ffmpeg_sha256 != ffmpeg.sha256
        || marker.ffprobe_sha256 != ffprobe.sha256
        || marker.source_sha256 != artifact.source.sha256
    {
        return Ok(false);
    }
    for pinned in &artifact.files {
        let path = final_dir.join(&pinned.destination);
        if !path.is_file() || sha256_file(&path)? != pinned.sha256.to_ascii_lowercase() {
            return Ok(false);
        }
    }
    Ok(true)
}

fn write_complete_marker(staging_dir: &Path, artifact: &RuntimeArtifact) -> Result<(), String> {
    let ffmpeg = runtime_file(artifact, RuntimeRole::Ffmpeg)?;
    let ffprobe = runtime_file(artifact, RuntimeRole::Ffprobe)?;
    let marker = CompleteMarker {
        schema_version: MANIFEST_SCHEMA_VERSION,
        target: artifact.target.clone(),
        build_id: artifact.build_id.clone(),
        archive_sha256: artifact.archive_sha256.clone(),
        ffmpeg_sha256: ffmpeg.sha256.clone(),
        ffprobe_sha256: ffprobe.sha256.clone(),
        source_sha256: artifact.source.sha256.clone(),
        installed_at_unix: SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_secs(),
    };
    let path = staging_dir.join(COMPLETE_MARKER);
    let mut file = File::create(&path)
        .map_err(|error| format!("cannot create FFmpeg completion marker: {error}"))?;
    serde_json::to_writer_pretty(&mut file, &marker)
        .map_err(|error| format!("cannot write FFmpeg completion marker: {error}"))?;
    file.write_all(b"\n")
        .map_err(|error| format!("cannot finish FFmpeg completion marker: {error}"))?;
    file.sync_all()
        .map_err(|error| format!("cannot sync FFmpeg completion marker: {error}"))
}

fn promote_install(
    tools_root: &Path,
    staging_dir: &Path,
    final_dir: &Path,
    build_id: &str,
) -> Result<(), String> {
    let quarantine = unique_sibling(tools_root, build_id, "replaced");
    let had_previous = final_dir.exists();
    if had_previous {
        fs::rename(final_dir, &quarantine)
            .map_err(|error| format!("cannot quarantine stale FFmpeg runtime: {error}"))?;
    }
    if let Err(error) = fs::rename(staging_dir, final_dir) {
        if had_previous {
            let _ = fs::rename(&quarantine, final_dir);
        }
        return Err(format!("cannot atomically install FFmpeg runtime: {error}"));
    }
    if had_previous {
        let _ = fs::remove_dir_all(quarantine);
    }
    Ok(())
}

fn sha256_file(path: &Path) -> Result<String, String> {
    let mut file =
        File::open(path).map_err(|error| format!("cannot hash {}: {error}", path_display(path)))?;
    let mut hasher = Sha256::new();
    let mut buffer = [0_u8; 64 * 1024];
    loop {
        let count = file
            .read(&mut buffer)
            .map_err(|error| format!("cannot hash {}: {error}", path_display(path)))?;
        if count == 0 {
            break;
        }
        hasher.update(&buffer[..count]);
    }
    Ok(hex::encode(hasher.finalize()))
}

fn unique_sibling(root: &Path, stem: &str, extension: &str) -> PathBuf {
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_nanos();
    root.join(format!(".{stem}-{}-{now}.{extension}", std::process::id()))
}

fn path_display(path: &Path) -> String {
    path.to_string_lossy().into_owned()
}

#[cfg(test)]
mod tests {
    use super::*;
    use zip::write::SimpleFileOptions;
    use zip::ZipWriter;

    fn digest(bytes: &[u8]) -> String {
        hex::encode(Sha256::digest(bytes))
    }

    fn fixture_artifact(ffmpeg: &[u8], ffprobe: &[u8], license: &[u8]) -> RuntimeArtifact {
        RuntimeArtifact {
            target: current_target().into(),
            build_id: "ffmpeg-8.1.2-r1-test".into(),
            url: "https://example.invalid/ffmpeg.zip".into(),
            archive_sha256: "a".repeat(64),
            archive_max_bytes: 10_000,
            files: vec![
                RuntimeFile {
                    member: "bin/ffmpeg".into(),
                    destination: if cfg!(windows) {
                        "bin/ffmpeg.exe".into()
                    } else {
                        "bin/ffmpeg".into()
                    },
                    sha256: digest(ffmpeg),
                    max_bytes: 1_000,
                    role: Some(RuntimeRole::Ffmpeg),
                },
                RuntimeFile {
                    member: "bin/ffprobe".into(),
                    destination: if cfg!(windows) {
                        "bin/ffprobe.exe".into()
                    } else {
                        "bin/ffprobe".into()
                    },
                    sha256: digest(ffprobe),
                    max_bytes: 1_000,
                    role: Some(RuntimeRole::Ffprobe),
                },
                RuntimeFile {
                    member: "LICENSES/FFmpeg.txt".into(),
                    destination: "LICENSES/FFmpeg.txt".into(),
                    sha256: digest(license),
                    max_bytes: 1_000,
                    role: None,
                },
            ],
            probe: RuntimeProbe {
                version_contains: "ffmpeg version 8.1.2".into(),
                ffprobe_version_contains: "ffprobe version 8.1.2".into(),
                required_build_flags: vec!["--disable-gpl".into()],
                forbidden_build_flags: vec!["--enable-nonfree".into()],
                required_encoders: vec!["mpeg4".into()],
                required_muxers: vec!["mp4".into()],
            },
            source: SourceProvenance {
                url: "https://ffmpeg.org/releases/ffmpeg-8.1.2.tar.xz".into(),
                sha256: "b".repeat(64),
                signature_url: "https://ffmpeg.org/releases/ffmpeg-8.1.2.tar.xz.asc".into(),
                signing_key_fingerprint: "FCF986EA15E6E293A5644F10B4322F04D67658D8".into(),
                build_workflow: ".github/workflows/ffmpeg-runtime.yml".into(),
            },
            license: LicenseProvenance {
                expression: "LGPL-2.1-or-later".into(),
                license_destination: "LICENSES/FFmpeg.txt".into(),
            },
        }
    }

    fn write_fixture_zip(
        path: &Path,
        ffmpeg: &[u8],
        ffprobe: &[u8],
        license: &[u8],
        extra: Option<(&str, &[u8])>,
    ) {
        let file = File::create(path).unwrap();
        let mut archive = ZipWriter::new(file);
        let options = SimpleFileOptions::default();
        archive.start_file("bin/ffmpeg", options).unwrap();
        archive.write_all(ffmpeg).unwrap();
        archive.start_file("bin/ffprobe", options).unwrap();
        archive.write_all(ffprobe).unwrap();
        archive.start_file("LICENSES/FFmpeg.txt", options).unwrap();
        archive.write_all(license).unwrap();
        if let Some((name, bytes)) = extra {
            archive.start_file(name, options).unwrap();
            archive.write_all(bytes).unwrap();
        }
        archive.finish().unwrap();
    }

    #[test]
    fn embedded_manifest_contains_complete_reviewed_release() {
        let manifest: RuntimeManifest = serde_json::from_str(EMBEDDED_MANIFEST).unwrap();
        validate_manifest(&manifest).unwrap();
        let targets = manifest
            .artifacts
            .iter()
            .map(|artifact| artifact.target.as_str())
            .collect::<HashSet<_>>();
        assert_eq!(
            targets,
            HashSet::from([
                "aarch64-apple-darwin",
                "x86_64-apple-darwin",
                "x86_64-pc-windows-msvc",
                "x86_64-unknown-linux-gnu",
            ])
        );
    }

    #[test]
    fn manifest_requires_safe_exact_files_and_complete_provenance() {
        let mut manifest = RuntimeManifest {
            schema_version: 1,
            runtime: "ffmpeg".into(),
            runtime_version: "8.1.2-r1".into(),
            artifacts: vec![fixture_artifact(b"ffmpeg", b"ffprobe", b"license")],
        };
        validate_manifest(&manifest).unwrap();

        manifest.artifacts[0].files[0].destination = "../ffmpeg".into();
        assert!(validate_manifest(&manifest)
            .unwrap_err()
            .contains("unsafe FFmpeg archive path"));
        manifest.artifacts[0].files[0].destination = "bin/ffmpeg".into();
        manifest.artifacts[0].source.signing_key_fingerprint = "unknown".into();
        assert!(validate_manifest(&manifest)
            .unwrap_err()
            .contains("incomplete source provenance"));
    }

    #[test]
    fn exact_archive_members_extract_and_hash_validate() {
        let temp = tempfile::tempdir().unwrap();
        let archive = temp.path().join("runtime.zip");
        let ffmpeg = b"not executed in this extraction test";
        let ffprobe = b"not executed either";
        let license = b"LGPL text";
        write_fixture_zip(&archive, ffmpeg, ffprobe, license, None);
        let artifact = fixture_artifact(ffmpeg, ffprobe, license);

        let prepared = prepare_install(temp.path(), &archive, &artifact).unwrap();
        assert_eq!(
            fs::read(prepared.staging_dir.join("bin/ffmpeg")).unwrap(),
            ffmpeg
        );
        assert_eq!(
            fs::read(prepared.staging_dir.join("bin/ffprobe")).unwrap(),
            ffprobe
        );
        assert_eq!(
            fs::read(prepared.staging_dir.join("LICENSES/FFmpeg.txt")).unwrap(),
            license
        );
    }

    #[test]
    fn unexpected_or_hash_drifted_archive_members_fail_closed() {
        let temp = tempfile::tempdir().unwrap();
        let ffmpeg = b"binary";
        let ffprobe = b"probe";
        let license = b"license";
        let artifact = fixture_artifact(ffmpeg, ffprobe, license);

        let extra_archive = temp.path().join("extra.zip");
        write_fixture_zip(
            &extra_archive,
            ffmpeg,
            ffprobe,
            license,
            Some(("../escape", b"bad")),
        );
        assert!(prepare_install(temp.path(), &extra_archive, &artifact)
            .unwrap_err()
            .contains("unsafe FFmpeg archive path"));

        let drift_archive = temp.path().join("drift.zip");
        write_fixture_zip(&drift_archive, b"changed", ffprobe, license, None);
        assert!(prepare_install(temp.path(), &drift_archive, &artifact)
            .unwrap_err()
            .contains("hash mismatch"));
    }

    #[test]
    fn capability_parser_requires_exact_names() {
        let output = " V..... mpeg4 MPEG-4\n V..... msmpeg4v3 Microsoft\n  E mp4 MP4\n";
        let names = capability_names(output);
        assert!(names.contains("mpeg4"));
        assert!(names.contains("mp4"));
        assert!(!names.contains("msmpeg4"));
    }

    #[test]
    fn override_path_must_be_absolute_regular_file() {
        assert!(validate_override_path(Path::new("ffmpeg"), "FFMPEG").is_err());
        let temp = tempfile::tempdir().unwrap();
        assert!(validate_override_path(temp.path(), "FFMPEG").is_err());
        let executable = temp.path().join(if cfg!(windows) {
            "ffmpeg.exe"
        } else {
            "ffmpeg"
        });
        fs::write(&executable, b"fixture").unwrap();
        assert_eq!(
            validate_override_path(&executable, "FFMPEG").unwrap(),
            fs::canonicalize(executable).unwrap()
        );
    }

    #[test]
    fn downloader_initializes_its_tls_provider() {
        download_client().unwrap();
        assert!(rustls::crypto::CryptoProvider::get_default().is_some());
    }

    #[test]
    #[ignore = "set OPENADAPT_FFMPEG_PROOF_ARCHIVE and _MANIFEST_ENTRY"]
    fn locally_built_managed_runtime_extracts_and_probes() {
        let archive = PathBuf::from(
            std::env::var_os("OPENADAPT_FFMPEG_PROOF_ARCHIVE")
                .expect("OPENADAPT_FFMPEG_PROOF_ARCHIVE"),
        );
        let entry_path = PathBuf::from(
            std::env::var_os("OPENADAPT_FFMPEG_PROOF_MANIFEST_ENTRY")
                .expect("OPENADAPT_FFMPEG_PROOF_MANIFEST_ENTRY"),
        );
        let artifact: RuntimeArtifact =
            serde_json::from_slice(&fs::read(entry_path).unwrap()).unwrap();
        let manifest = RuntimeManifest {
            schema_version: 1,
            runtime: "ffmpeg".into(),
            runtime_version: "8.1.2-r1".into(),
            artifacts: vec![artifact.clone()],
        };
        validate_manifest(&manifest).unwrap();

        let temp = tempfile::tempdir().unwrap();
        let prepared = prepare_install(temp.path(), &archive, &artifact).unwrap();
        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .unwrap();
        runtime
            .block_on(probe_executables(&prepared.paths, &artifact.probe))
            .unwrap();
    }
}
