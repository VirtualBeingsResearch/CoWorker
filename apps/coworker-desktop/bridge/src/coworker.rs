use futures_util::StreamExt;
use reqwest::header::HeaderMap;
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use tokio::{
    sync::{mpsc, oneshot},
    time::{Duration, timeout},
};
use tracing::{info, warn};

use crate::{
    config::BridgeCoworker,
    desktop_protocol::{
        ActorId, DESKTOP_PROTOCOL_VERSION, DESKTOP_REGISTRATION_KIND, DeliveryAck,
        DesktopEnvelopeV1, REQUIRED_COWORKER_SKILL, desktop_client_id,
    },
    error::{BridgeError, Result},
    relay_transport,
};

const DUPLICATE_SSE_REJECTION_HEADER: &str = "duplicate-participant";
const SSE_CONNECT_RESPONSE_TIMEOUT: Duration = Duration::from_secs(30);

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CoworkerRegistration {
    pub registration_id: String,
    pub participant_id: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CoworkerMessageAttachment {
    pub filename: String,
    pub media_type: String,
    pub data: String,
}

#[derive(Clone)]
pub struct CoworkerHttpClient {
    client: reqwest::Client,
}

impl CoworkerHttpClient {
    pub fn new() -> Result<Self> {
        let client = reqwest::Client::builder()
            // Do not set a global request timeout: SSE responses are intentionally
            // long-lived and should stay open across heartbeat frames.
            .connect_timeout(std::time::Duration::from_secs(10))
            .build()?;
        Ok(Self { client })
    }

    pub async fn register_desktop_participant(
        &self,
        coworker: &BridgeCoworker,
        desktop_id: &str,
        actor_id: ActorId,
        display_name: &str,
        bearer_token: Option<&str>,
        development_mode: bool,
    ) -> Result<CoworkerRegistration> {
        validate_desktop_transport(&coworker.base_url, bearer_token, development_mode)?;
        if relay_transport::relay_endpoint(&coworker.base_url).is_some() {
            let token = required_relay_token(bearer_token)?;
            let body = json!({
                "kind": DESKTOP_REGISTRATION_KIND,
                "client_id": desktop_client_id(desktop_id, actor_id, &coworker.coworker_id),
                "display_name": display_name,
                "metadata": {
                    "desktop_id": desktop_id,
                    "actor_id": actor_id,
                    "coworker_id": coworker.coworker_id,
                    "protocol_versions": [DESKTOP_PROTOCOL_VERSION],
                    "capabilities": ["conversations", "modes", "approvals", "attachments", "reliable_delivery", "desktop_update_push"],
                    "desktop_version": env!("CARGO_PKG_VERSION"),
                    "skill_version": "1.0.0",
                    "required_skill": REQUIRED_COWORKER_SKILL,
                    "available": true,
                },
            });
            let response = relay_transport::request(
                &coworker.base_url,
                token,
                "POST",
                "/api/communicate/register",
                vec![("content-type".into(), "application/json".into())],
                serde_json::to_vec(&body)?,
            )
            .await?;
            let response = relay_response_for_status(response)?;
            let response: Value = serde_json::from_slice(&response.body)?;
            return registration_from_response(&response);
        }
        let url = format!("{}/api/communicate/register", coworker.base_url);
        let request = self
            .client
            .post(url)
            .timeout(std::time::Duration::from_secs(30));
        let response = with_bearer(request, bearer_token)
            .json(&json!({
                "kind": DESKTOP_REGISTRATION_KIND,
                "client_id": desktop_client_id(desktop_id, actor_id, &coworker.coworker_id),
                "display_name": display_name,
                "metadata": {
                    "desktop_id": desktop_id,
                    "actor_id": actor_id,
                    "coworker_id": coworker.coworker_id,
                    "protocol_versions": [DESKTOP_PROTOCOL_VERSION],
                    "capabilities": ["conversations", "modes", "approvals", "attachments", "reliable_delivery", "desktop_update_push"],
                    "desktop_version": env!("CARGO_PKG_VERSION"),
                    "skill_version": "1.0.0",
                    "required_skill": REQUIRED_COWORKER_SKILL,
                    "available": true,
                },
            }))
            .send()
            .await?;
        let response: Value = response_for_status(response).await?.json().await?;
        registration_from_response(&response)
    }

    pub async fn delete_desktop_registration(
        &self,
        coworker: &BridgeCoworker,
        registration_id: &str,
        bearer_token: Option<&str>,
        development_mode: bool,
    ) -> Result<()> {
        validate_desktop_transport(&coworker.base_url, bearer_token, development_mode)?;
        if relay_transport::relay_endpoint(&coworker.base_url).is_some() {
            let response = relay_transport::request(
                &coworker.base_url,
                required_relay_token(bearer_token)?,
                "DELETE",
                &format!("/api/communicate/register/{registration_id}"),
                vec![],
                vec![],
            )
            .await?;
            relay_response_for_status(response)?;
            return Ok(());
        }
        let url = format!(
            "{}/api/communicate/register/{registration_id}",
            coworker.base_url
        );
        let response = with_bearer(
            self.client
                .delete(url)
                .timeout(std::time::Duration::from_secs(30)),
            bearer_token,
        )
        .send()
        .await?;
        response_for_status(response).await?.bytes().await?;
        Ok(())
    }

    pub async fn post_desktop_envelope(
        &self,
        coworker: &BridgeCoworker,
        participant_id: &str,
        envelope: &DesktopEnvelopeV1,
        bearer_token: Option<&str>,
        development_mode: bool,
    ) -> Result<DeliveryAck> {
        envelope.validate()?;
        validate_desktop_transport(&coworker.base_url, bearer_token, development_mode)?;
        if relay_transport::relay_endpoint(&coworker.base_url).is_some() {
            let body = desktop_message_body(participant_id, envelope)?;
            let response = relay_transport::request(
                &coworker.base_url,
                required_relay_token(bearer_token)?,
                "POST",
                "/messages",
                vec![("content-type".into(), "application/json".into())],
                serde_json::to_vec(&body)?,
            )
            .await?;
            let response = relay_response_for_status(response)?;
            return Ok(serde_json::from_slice(&response.body)?);
        }
        let url = format!("{}/messages", coworker.base_url);
        let body = desktop_message_body(participant_id, envelope)?;
        let response = with_bearer(
            self.client
                .post(url)
                .timeout(std::time::Duration::from_secs(30)),
            bearer_token,
        )
        .json(&body)
        .send()
        .await?;
        let response: DeliveryAck = response_for_status(response).await?.json().await?;
        Ok(response)
    }

    pub async fn list_desktop_registrations(
        &self,
        coworker: &BridgeCoworker,
        bearer_token: Option<&str>,
        development_mode: bool,
    ) -> Result<Vec<CoworkerRegistration>> {
        validate_desktop_transport(&coworker.base_url, bearer_token, development_mode)?;
        if relay_transport::relay_endpoint(&coworker.base_url).is_some() {
            let response = relay_transport::request(
                &coworker.base_url,
                required_relay_token(bearer_token)?,
                "GET",
                "/api/communicate/register",
                vec![("accept".into(), "application/json".into())],
                vec![],
            )
            .await?;
            let response = relay_response_for_status(response)?;
            let response: Value = serde_json::from_slice(&response.body)?;
            return registrations_from_response(&response);
        }
        self.list_registrations_with_bearer(coworker, bearer_token)
            .await
    }

    async fn list_registrations_with_bearer(
        &self,
        coworker: &BridgeCoworker,
        bearer_token: Option<&str>,
    ) -> Result<Vec<CoworkerRegistration>> {
        let url = format!("{}/api/communicate/register", coworker.base_url);
        let response = with_bearer(
            self.client
                .get(url)
                .timeout(std::time::Duration::from_secs(30)),
            bearer_token,
        )
        .send()
        .await?;
        let response: Value = response_for_status(response).await?.json().await?;
        registrations_from_response(&response)
    }

    pub async fn consume_desktop_sse_once(
        &self,
        coworker: BridgeCoworker,
        participant_id: String,
        bearer_token: Option<&str>,
        development_mode: bool,
        messages: mpsc::Sender<String>,
        connected: oneshot::Sender<()>,
    ) -> Result<()> {
        validate_desktop_transport(&coworker.base_url, bearer_token, development_mode)?;
        if relay_transport::relay_endpoint(&coworker.base_url).is_some() {
            return relay_transport::consume_sse(
                &coworker.base_url,
                required_relay_token(bearer_token)?,
                &format!("/sse/{participant_id}"),
                messages,
                connected,
            )
            .await;
        }
        let url = format!("{}/sse/{}", coworker.base_url, participant_id);
        let response = timeout(
            SSE_CONNECT_RESPONSE_TIMEOUT,
            with_bearer(self.client.get(&url), bearer_token).send(),
        )
        .await
        .map_err(|_| {
            BridgeError::message(format!(
                "Coworker SSE connect timed out after {}s waiting for response",
                SSE_CONNECT_RESPONSE_TIMEOUT.as_secs()
            ))
        })??;
        check_duplicate(response.headers(), &coworker, &participant_id)?;
        let response = response_for_status(response).await?;
        let _ = connected.send(());
        info!(coworker_id = %coworker.coworker_id, actor_participant = %participant_id, "Desktop actor SSE connected");
        let mut stream = response.bytes_stream();
        let mut buffer = String::new();
        let mut current = Vec::<String>::new();
        while let Some(chunk) = stream.next().await {
            let chunk = chunk?;
            buffer.push_str(&String::from_utf8_lossy(&chunk));
            while let Some(index) = buffer.find('\n') {
                let mut line = buffer[..index].to_owned();
                if line.ends_with('\r') {
                    line.pop();
                }
                buffer.drain(..=index);
                if line.is_empty() {
                    if !current.is_empty() {
                        let message = current.join("\n");
                        current.clear();
                        if messages.send(message).await.is_err() {
                            return Ok(());
                        }
                    }
                } else if let Some(value) = line.strip_prefix("data:") {
                    current.push(value.strip_prefix(' ').unwrap_or(value).to_owned());
                }
            }
        }
        Ok(())
    }
}

async fn response_for_status(response: reqwest::Response) -> Result<reqwest::Response> {
    let status = response.status();
    if status.is_success() {
        return Ok(response);
    }
    let body = response.text().await?;
    Err(BridgeError::http_status(
        status,
        response_error_detail(&body),
    ))
}

fn response_error_detail(body: &str) -> String {
    const MAX_ERROR_CHARS: usize = 1_000;
    if let Ok(value) = serde_json::from_str::<Value>(body) {
        for key in ["detail", "message", "error"] {
            if let Some(detail) = value.get(key) {
                if let Some(detail) = detail.as_str().filter(|value| !value.trim().is_empty()) {
                    return detail.trim().chars().take(MAX_ERROR_CHARS).collect();
                }
                if !detail.is_null() {
                    return detail.to_string().chars().take(MAX_ERROR_CHARS).collect();
                }
            }
        }
    }
    let detail: String = body.trim().chars().take(MAX_ERROR_CHARS).collect();
    if detail.is_empty() {
        "response body was empty".to_owned()
    } else {
        detail
    }
}

fn desktop_message_body(participant_id: &str, envelope: &DesktopEnvelopeV1) -> Result<Value> {
    let mut body = serde_json::to_value(envelope)?;
    let fields = body
        .as_object_mut()
        .ok_or_else(|| BridgeError::message("Desktop envelope must serialize as an object"))?;
    fields.insert(
        "sender_id".to_owned(),
        Value::String(participant_id.to_owned()),
    );
    Ok(body)
}

fn registration_from_response(response: &Value) -> Result<CoworkerRegistration> {
    let registration_id = response
        .get("registration_id")
        .and_then(Value::as_str)
        .filter(|value| !value.is_empty())
        .ok_or_else(|| BridgeError::message("communicate register missing registration_id"))?
        .to_owned();
    let participant_id = response
        .get("participant_id")
        .and_then(Value::as_str)
        .filter(|value| !value.is_empty())
        .ok_or_else(|| BridgeError::message("communicate register missing participant_id"))?
        .to_owned();
    Ok(CoworkerRegistration {
        registration_id,
        participant_id,
    })
}

fn registrations_from_response(response: &Value) -> Result<Vec<CoworkerRegistration>> {
    response
        .get("registrations")
        .and_then(Value::as_array)
        .ok_or_else(|| BridgeError::message("communicate register list missing registrations"))?
        .iter()
        .map(|item| serde_json::from_value(item.clone()).map_err(BridgeError::from))
        .collect::<Result<Vec<_>>>()
}

fn required_relay_token(token: Option<&str>) -> Result<&str> {
    token
        .filter(|value| !value.trim().is_empty())
        .ok_or_else(|| {
            BridgeError::Config("Relay connection requires a communication token".into())
        })
}

fn relay_response_for_status(
    response: relay_transport::RelayResponse,
) -> Result<relay_transport::RelayResponse> {
    if (200..300).contains(&response.status) {
        return Ok(response);
    }
    let body = String::from_utf8_lossy(&response.body);
    Err(BridgeError::HttpStatus {
        status: response.status,
        reason: "Relay response".into(),
        detail: response_error_detail(&body),
    })
}

fn with_bearer(
    request: reqwest::RequestBuilder,
    bearer_token: Option<&str>,
) -> reqwest::RequestBuilder {
    match bearer_token.filter(|value| !value.trim().is_empty()) {
        Some(token) => request.bearer_auth(token),
        None => request,
    }
}

pub fn validate_desktop_transport(
    base_url: &str,
    bearer_token: Option<&str>,
    development_mode: bool,
) -> Result<()> {
    if development_mode {
        return Ok(());
    }
    if relay_transport::relay_endpoint(base_url).is_some() {
        required_relay_token(bearer_token)?;
        return Ok(());
    }
    if !base_url.trim().to_ascii_lowercase().starts_with("https://") {
        return Err(BridgeError::Config(format!(
            "production Coworker URL must use HTTPS: {base_url}"
        )));
    }
    if bearer_token.is_none_or(|value| value.trim().is_empty()) {
        return Err(BridgeError::Config(format!(
            "production Coworker connection requires a bearer token: {base_url}"
        )));
    }
    Ok(())
}

fn check_duplicate(
    headers: &HeaderMap,
    coworker: &BridgeCoworker,
    participant_id: &str,
) -> Result<()> {
    if headers
        .get("x-connection-rejected")
        .and_then(|value| value.to_str().ok())
        == Some(DUPLICATE_SSE_REJECTION_HEADER)
    {
        warn!(
            coworker_id = %coworker.coworker_id,
            %participant_id,
            "Coworker SSE rejected duplicate participant"
        );
        return Err(BridgeError::DuplicateSseParticipant);
    }
    Ok(())
}

#[cfg(test)]
fn consume_sse_line(line: &str, current: &mut Vec<String>) -> Option<String> {
    if line.is_empty() {
        if current.is_empty() {
            return None;
        }
        return Some(std::mem::take(current).join("\n"));
    }
    if line.starts_with(':') {
        return None;
    }
    if let Some(data) = line.strip_prefix("data:") {
        current.push(data.strip_prefix(' ').unwrap_or(data).to_owned());
    }
    None
}

#[cfg(test)]
mod tests {
    use reqwest::header::{HeaderMap, HeaderValue};
    use serde_json::json;

    use super::{
        check_duplicate, consume_sse_line, desktop_message_body, response_error_detail,
        validate_desktop_transport,
    };
    use crate::config::BridgeCoworker;
    use crate::desktop_protocol::{DesktopEnvelopeV1, DesktopEventType};

    #[test]
    fn parses_multiline_sse_data() {
        let mut current = Vec::new();
        assert!(consume_sse_line("data: one", &mut current).is_none());
        assert!(consume_sse_line("data: two", &mut current).is_none());
        assert_eq!(consume_sse_line("", &mut current), Some("one\ntwo".into()));
    }

    #[test]
    fn extracts_fastapi_error_detail_for_desktop_diagnostics() {
        assert_eq!(
            response_error_detail(r#"{"detail":"通信令牌未配置"}"#),
            "通信令牌未配置"
        );
        assert_eq!(
            response_error_detail(r#"{"message":"fallback"}"#),
            "fallback"
        );
        assert_eq!(response_error_detail("plain failure"), "plain failure");
    }

    #[test]
    fn duplicate_sse_header_is_rejected() {
        let coworker = BridgeCoworker {
            coworker_id: "cw_default".into(),
            display_name: "搭档".into(),
            base_url: "http://localhost:8000".into(),
        };
        let mut headers = HeaderMap::new();
        headers.insert(
            "x-connection-rejected",
            HeaderValue::from_static("duplicate-participant"),
        );

        assert!(check_duplicate(&headers, &coworker, "bridge").is_err());
    }

    #[test]
    fn production_transport_requires_https_and_token() {
        assert!(
            validate_desktop_transport("http://localhost:8000", Some("secret"), false).is_err()
        );
        assert!(validate_desktop_transport("https://example.test", None, false).is_err());
        assert!(validate_desktop_transport("https://example.test", Some("secret"), false).is_ok());
        assert!(
            validate_desktop_transport(
                "http://relay.test:8443/i/cw_abcdefgh",
                Some("cwct_v1_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"),
                false
            )
            .is_ok()
        );
        assert!(validate_desktop_transport("http://localhost:8000", None, true).is_ok());
    }

    #[test]
    fn desktop_message_body_flattens_the_envelope() {
        let mut envelope = DesktopEnvelopeV1::new(
            DesktopEventType::ThreadEvent,
            json!({"actor_id": "claude", "message": "hello"}),
        );
        envelope.conversation_id = Some("session-1".into());

        let body = desktop_message_body("coworker-desktop:desk:claude:cw:p", &envelope)
            .expect("valid body");

        assert_eq!(body["sender_id"], "coworker-desktop:desk:claude:cw:p");
        assert_eq!(body["message_id"], envelope.message_id);
        assert_eq!(body["type"], "desktop.thread.event");
        assert_eq!(body["payload"]["message"], "hello");
        assert!(body.get("content").is_none());
    }
}
