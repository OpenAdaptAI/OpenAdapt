//! Strict operating-system deep-link boundary for Cloud pairing and authoring.
//!
//! The protocol handler never opens a URL or constructs a process command. It
//! accepts two fixed schemes, validates every field, and forwards the original
//! URI as one JSON string to one sidecar action:
//!
//! - `openadapt://connect` → `connect_uri`
//! - `openadapt://runner` → `claim_runner_uri`
//!
//! Connect is not widened to accept runner fields, and runner is not widened
//! to accept connect fields.

use std::collections::{HashMap, HashSet};
use std::error::Error;
use std::hash::{DefaultHasher, Hash, Hasher};
use std::sync::{Arc, Mutex};

use serde_json::json;
use tauri::{App, AppHandle, Emitter};
use tauri_plugin_deep_link::DeepLinkExt;
use url::Url;

use crate::sidecar::SidecarInner;

const MANAGED_HOST: &str = "app.openadapt.ai";
const AUTHORING_ORIGIN: &str = "https://openadapt.ai";
const MAX_URI_BYTES: usize = 2048;
const MAX_RECENT_LINKS: usize = 64;

#[derive(Default)]
pub struct PairingLinkState {
    handled: Mutex<HashSet<u64>>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
struct PairingAction {
    command: &'static str,
    uri: String,
}

pub fn setup(
    app: &mut App,
    engine: Arc<SidecarInner>,
    state: Arc<PairingLinkState>,
) -> Result<(), Box<dyn Error>> {
    // Static installer registration covers deb/MSI/NSIS. AppImages need the
    // current executable registered at runtime because their absolute path can
    // change. Debug Windows uses the same helper for local verification.
    #[cfg(any(target_os = "linux", all(debug_assertions, windows)))]
    if let Err(error) = app.deep_link().register_all() {
        // deb/MSI/NSIS registration remains intact if runtime AppImage/dev
        // registration is unavailable; do not prevent the app from starting.
        eprintln!("[pairing] protocol registration unavailable: {error}");
    }

    if let Some(urls) = app.deep_link().get_current()? {
        route_urls(app.handle().clone(), engine.clone(), state.clone(), &urls);
    }

    let app_handle = app.handle().clone();
    app.deep_link().on_open_url(move |event| {
        route_urls(
            app_handle.clone(),
            engine.clone(),
            state.clone(),
            &event.urls(),
        );
    });
    Ok(())
}

fn route_urls(
    app: AppHandle,
    engine: Arc<SidecarInner>,
    state: Arc<PairingLinkState>,
    urls: &[Url],
) {
    let action = match single_action(urls) {
        Ok(action) => action,
        Err(error) => {
            let event = if urls.iter().any(|url| url.host_str() == Some("runner")) {
                "engine://authoring_state"
            } else {
                "engine://pairing_state"
            };
            emit_state(&app, event, "error", Some(error));
            return;
        }
    };

    // Keep only a non-secret in-memory fingerprint. Initial delivery and the
    // single-instance event can otherwise race and consume the one-use code
    // twice.
    let fingerprint = fingerprint(&action.uri);
    {
        let mut handled = state.handled.lock().unwrap();
        if handled.contains(&fingerprint) {
            return;
        }
        if handled.len() >= MAX_RECENT_LINKS {
            handled.clear();
        }
        handled.insert(fingerprint);
    }

    let event = status_event(action.command);
    let connecting = if action.command == "claim_runner_uri" {
        "claiming"
    } else {
        "connecting"
    };
    emit_state(&app, event, connecting, None);
    tauri::async_runtime::spawn(async move {
        let result = engine
            .send_command(action.command, json!({ "uri": action.uri }))
            .await;
        match result {
            Ok(data) => {
                let connected = if action.command == "claim_runner_uri" {
                    "bound"
                } else {
                    "connected"
                };
                let _ = app.emit(event, json!({ "status": connected, "data": data }));
            }
            Err(error) => {
                eprintln!("[pairing] connection failed: {error}");
                emit_state(&app, event, "error", Some(&error));
            }
        }
    });
}

fn status_event(command: &str) -> &'static str {
    if command == "claim_runner_uri" {
        "engine://authoring_state"
    } else {
        "engine://pairing_state"
    }
}

fn emit_state(app: &AppHandle, event: &str, status: &str, error: Option<&str>) {
    let payload = match error {
        Some(error) => json!({ "status": status, "error": error }),
        None => json!({ "status": status }),
    };
    let _ = app.emit(event, payload);
}

fn fingerprint(uri: &str) -> u64 {
    let mut hasher = DefaultHasher::new();
    uri.hash(&mut hasher);
    hasher.finish()
}

fn single_action(urls: &[Url]) -> Result<PairingAction, &'static str> {
    if urls.len() != 1 {
        return Err("OpenAdapt received an ambiguous connect request");
    }
    action_for_url(&urls[0])
}

fn action_for_url(url: &Url) -> Result<PairingAction, &'static str> {
    let uri = url.as_str();
    let runner = url.host_str() == Some("runner");
    let invalid = if runner {
        "Invalid OpenAdapt runner link"
    } else {
        "Invalid OpenAdapt connect link"
    };
    if uri.len() > MAX_URI_BYTES
        || url.scheme() != "openadapt"
        || !url.username().is_empty()
        || url.password().is_some()
        || url.port().is_some()
        || !matches!(url.path(), "" | "/")
        || url.fragment().is_some()
    {
        return Err(invalid);
    }

    match url.host_str() {
        Some("connect") => connect_action(url, uri),
        Some("runner") => runner_action(url, uri),
        _ => Err("Invalid OpenAdapt connect link"),
    }
}

fn connect_action(url: &Url, uri: &str) -> Result<PairingAction, &'static str> {
    let mut fields: HashMap<String, String> = HashMap::new();
    for (key, value) in url.query_pairs() {
        if !matches!(key.as_ref(), "pairing" | "host" | "destination_kind")
            || fields
                .insert(key.into_owned(), value.into_owned())
                .is_some()
        {
            return Err("Connect link contains unknown or duplicate fields");
        }
    }

    let pairing = fields
        .get("pairing")
        .ok_or("Connect link is missing pairing or host")?;
    let host = fields
        .get("host")
        .ok_or("Connect link is missing pairing or host")?;
    if !valid_pairing_secret(pairing) {
        return Err("Pairing code is malformed");
    }
    validate_destination(host, fields.get("destination_kind").map(String::as_str))?;

    Ok(PairingAction {
        command: "connect_uri",
        uri: uri.to_owned(),
    })
}

fn runner_action(url: &Url, uri: &str) -> Result<PairingAction, &'static str> {
    let mut fields: HashMap<String, String> = HashMap::new();
    for (key, value) in url.query_pairs() {
        if !matches!(key.as_ref(), "pack" | "bind" | "origin")
            || fields
                .insert(key.into_owned(), value.into_owned())
                .is_some()
        {
            return Err("Runner link contains unknown or duplicate fields");
        }
    }

    let pack = fields
        .get("pack")
        .ok_or("Runner link is missing pack, bind, or origin")?;
    let bind = fields
        .get("bind")
        .ok_or("Runner link is missing pack, bind, or origin")?;
    let origin = fields
        .get("origin")
        .ok_or("Runner link is missing pack, bind, or origin")?;
    if !valid_pack_id(pack) {
        return Err("Pack id is malformed");
    }
    if !valid_bind_token(bind) {
        return Err("Bind token is malformed");
    }
    if origin != AUTHORING_ORIGIN {
        return Err("Runner link does not name the OpenAdapt authoring origin");
    }

    Ok(PairingAction {
        command: "claim_runner_uri",
        uri: uri.to_owned(),
    })
}

fn valid_pairing_secret(value: &str) -> bool {
    value.len() == 47 && value.starts_with("oap_") && unreserved_body(&value[4..])
}

fn valid_bind_token(value: &str) -> bool {
    if value.starts_with("oar_") || value.starts_with("oap_") {
        return false;
    }
    // Cloud runner bodies are 64 hex. That encoding is never a bind token.
    if value.len() == 68 && value.starts_with("oab_") && hex_body(&value[4..]) {
        return false;
    }
    value.len() == 47 && value.starts_with("oab_") && unreserved_body(&value[4..])
}

fn hex_body(value: &str) -> bool {
    value
        .bytes()
        .all(|byte| matches!(byte, b'0'..=b'9' | b'a'..=b'f'))
}

fn valid_pack_id(value: &str) -> bool {
    if let Some(body) = value.strip_prefix("p.") {
        return body.len() == 12 && unreserved_body(body);
    }
    if let Some(body) = value.strip_prefix("v1.") {
        return (32..=2000).contains(&body.len()) && unreserved_body(body);
    }
    false
}

fn unreserved_body(value: &str) -> bool {
    value
        .bytes()
        .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'_' | b'-'))
}

fn validate_destination(host: &str, destination_kind: Option<&str>) -> Result<(), &'static str> {
    let parsed = Url::parse(host).map_err(|_| "Connect link contains an invalid Cloud origin")?;
    if !matches!(parsed.scheme(), "http" | "https")
        || !parsed.username().is_empty()
        || parsed.password().is_some()
        || !matches!(parsed.path(), "" | "/")
        || parsed.query().is_some()
        || parsed.fragment().is_some()
    {
        return Err("Connect link contains an invalid Cloud origin");
    }

    let hostname = parsed
        .host_str()
        .ok_or("Connect link contains an invalid Cloud origin")?;
    match destination_kind {
        None | Some("openadapt-managed")
            if parsed.scheme() == "https"
                && hostname == MANAGED_HOST
                && parsed.port_or_known_default() == Some(443) =>
        {
            Ok(())
        }
        Some("local")
            if matches!(hostname, "localhost" | "127.0.0.1" | "::1")
                && matches!(parsed.scheme(), "http" | "https") =>
        {
            Ok(())
        }
        Some("openadapt-managed") => {
            Err("Connect link does not name the managed OpenAdapt service")
        }
        Some("local") => Err("A local connect link must use this computer"),
        _ => Err("Connect link has an unsupported destination kind"),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const SECRET: &str = "oap_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA";

    fn parse(raw: &str) -> Url {
        Url::parse(raw).unwrap()
    }

    const BIND: &str = "oab_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA";
    const PACK: &str = "p.abcdefghijkl";

    fn runner_uri() -> String {
        format!("openadapt://runner?pack={PACK}&bind={BIND}&origin=https%3A%2F%2Fopenadapt.ai")
    }

    #[test]
    fn accepts_only_fixed_connect_action() {
        let url = parse(&format!(
            "openadapt://connect?pairing={SECRET}&host=https%3A%2F%2Fapp.openadapt.ai"
        ));
        let action = action_for_url(&url).unwrap();
        assert_eq!(action.command, "connect_uri");
        assert_eq!(action.uri, url.as_str());

        for raw in [
            format!("openadapt://run?pairing={SECRET}&host=https://app.openadapt.ai"),
            format!("https://connect?pairing={SECRET}&host=https://app.openadapt.ai"),
            format!("openadapt://connect/run?pairing={SECRET}&host=https://app.openadapt.ai"),
            format!("openadapt://connect?pairing={SECRET}&host=https://app.openadapt.ai#x"),
            runner_uri(),
            format!("openadapt://connect?pack={PACK}&bind={BIND}&origin=https://openadapt.ai"),
        ] {
            assert!(action_for_url(&parse(&raw)).is_err(), "{raw}");
        }
    }

    #[test]
    fn accepts_only_fixed_runner_action() {
        let url = parse(&runner_uri());
        let action = action_for_url(&url).unwrap();
        assert_eq!(action.command, "claim_runner_uri");
        assert_eq!(action.uri, url.as_str());

        for raw in [
            format!("openadapt://run?pack={PACK}&bind={BIND}&origin=https://openadapt.ai"),
            format!(
                "openadapt://connect/runner?pack={PACK}&bind={BIND}&origin=https://openadapt.ai"
            ),
            format!("{}#x", runner_uri()),
            format!("openadapt://connect?pairing={SECRET}&host=https://app.openadapt.ai"),
            format!("openadapt://runner?pairing={SECRET}&host=https://app.openadapt.ai"),
        ] {
            assert!(action_for_url(&parse(&raw)).is_err(), "{raw}");
        }
    }

    #[test]
    fn rejects_malformed_duplicate_and_unknown_fields() {
        for raw in [
            "openadapt://connect?pairing=short&host=https://app.openadapt.ai".to_string(),
            format!("openadapt://connect?pairing={SECRET}"),
            format!(
                "openadapt://connect?pairing={SECRET}&pairing={SECRET}&host=https://app.openadapt.ai"
            ),
            format!(
                "openadapt://connect?pairing={SECRET}&host=https://app.openadapt.ai&command=run"
            ),
        ] {
            assert!(action_for_url(&parse(&raw)).is_err(), "{raw}");
        }
    }

    #[test]
    fn runner_rejects_malformed_duplicate_unknown_and_foreign_tokens() {
        for raw in [
            format!("openadapt://runner?pack=short&bind={BIND}&origin=https://openadapt.ai"),
            format!("openadapt://runner?pack={PACK}&bind={BIND}"),
            format!(
                "openadapt://runner?pack={PACK}&bind={BIND}&bind={BIND}&origin=https://openadapt.ai"
            ),
            format!(
                "openadapt://runner?pack={PACK}&bind={BIND}&origin=https://openadapt.ai&command=run"
            ),
            format!(
                "openadapt://runner?pack={PACK}&bind=oar_{}&origin=https://openadapt.ai",
                "a".repeat(64)
            ),
            format!("openadapt://runner?pack={PACK}&bind={SECRET}&origin=https://openadapt.ai"),
            format!(
                "openadapt://runner?pack={PACK}&bind=oab_{}&origin=https://openadapt.ai",
                "a".repeat(64)
            ),
            format!(
                "openadapt://runner?pack={PACK}&bind=oals_{}&origin=https://openadapt.ai",
                "A".repeat(43)
            ),
            format!(
                "openadapt://runner?pack={PACK}&bind={BIND}&origin=https://preview.openadapt.ai"
            ),
        ] {
            assert!(action_for_url(&parse(&raw)).is_err(), "{raw}");
        }
    }

    #[test]
    fn argument_shaped_data_never_changes_the_fixed_action() {
        let encoded_argument = "%2D%2Dhost%3Dhttps%3A%2F%2Fevil.example";
        let raw =
            format!("openadapt://connect?pairing={encoded_argument}&host=https://app.openadapt.ai");
        assert_eq!(
            action_for_url(&parse(&raw)),
            Err("Pairing code is malformed")
        );
    }

    #[test]
    fn destinations_are_managed_origin_or_explicit_loopback() {
        assert!(validate_destination("https://app.openadapt.ai", None).is_ok());
        assert!(validate_destination("http://localhost:3000", Some("local")).is_ok());
        assert!(validate_destination("https://app.openadapt.ai.evil.example", None).is_err());
        assert!(validate_destination("https://example.com", Some("local")).is_err());
        assert!(validate_destination("http://app.openadapt.ai", None).is_err());
    }

    #[test]
    fn multiple_urls_are_rejected_as_ambiguous() {
        let url = parse(&format!(
            "openadapt://connect?pairing={SECRET}&host=https://app.openadapt.ai"
        ));
        assert!(single_action(&[]).is_err());
        assert!(single_action(&[url.clone(), url]).is_err());
    }
}
