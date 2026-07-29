use std::{
    fmt,
    sync::Arc,
    time::{Duration, SystemTime, UNIX_EPOCH},
};

use base64::{Engine as _, engine::general_purpose::URL_SAFE_NO_PAD};
use ed25519_dalek::{Signer as _, SigningKey};
use futures_util::{SinkExt, StreamExt};
use hkdf::Hkdf;
use rustls::{
    DigitallySignedStruct, SignatureScheme,
    client::danger::{HandshakeSignatureValid, ServerCertVerified, ServerCertVerifier},
    crypto::WebPkiSupportedAlgorithms,
    pki_types::{CertificateDer, ServerName, UnixTime},
};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use tokio::{
    io::{AsyncReadExt, AsyncWriteExt, DuplexStream},
    sync::{mpsc, oneshot},
    task::JoinHandle,
    time::timeout,
};
use tokio_rustls::{TlsConnector, client::TlsStream};
use tokio_tungstenite::{connect_async, tungstenite::Message};
use url::Url;
use x509_parser::parse_x509_certificate;

use crate::error::{BridgeError, Result};

const TOKEN_PREFIX: &str = "cwct_v1_";
const DOMAIN: &[u8] = b"coworker-relay-e2ee-v1";
const PROTOCOL_VERSION: u8 = 1;
const MAX_INNER_FRAME: usize = 1024 * 1024;
const MAX_OUTER_FRAME: usize = 256 * 1024;

const CLIENT_PROOF_CHALLENGE: u8 = 1;
const CLIENT_PROOF: u8 = 2;
const CLIENT_READY: u8 = 3;
const REQUEST_START: u8 = 10;
const REQUEST_BODY: u8 = 11;
const REQUEST_END: u8 = 12;
const REQUEST_CANCEL: u8 = 13;
const RESPONSE_START: u8 = 20;
const RESPONSE_BODY: u8 = 21;
const RESPONSE_END: u8 = 22;
const RESPONSE_ERROR: u8 = 23;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RelayEndpoint {
    pub public_origin: String,
    pub instance_id: String,
    pub websocket_url: String,
}

#[derive(Debug)]
pub struct RelayResponse {
    pub status: u16,
    pub headers: Vec<(String, String)>,
    pub body: Vec<u8>,
}

#[derive(Debug)]
pub enum RelayStreamEvent {
    Start {
        status: u16,
        headers: Vec<(String, String)>,
    },
    Body(Vec<u8>),
    End,
}

struct RelayConnection {
    tls: TlsStream<DuplexStream>,
    tasks: Vec<JoinHandle<()>>,
}

impl Drop for RelayConnection {
    fn drop(&mut self) {
        for task in &self.tasks {
            task.abort();
        }
    }
}

#[derive(Clone, Copy)]
struct InnerFrameHeader {
    kind: u8,
    stream_id: u32,
    length: u32,
}

impl InnerFrameHeader {
    fn encode(self) -> [u8; 10] {
        let mut raw = [0_u8; 10];
        raw[0] = PROTOCOL_VERSION;
        raw[1] = self.kind;
        raw[2..6].copy_from_slice(&self.stream_id.to_be_bytes());
        raw[6..10].copy_from_slice(&self.length.to_be_bytes());
        raw
    }

    fn decode(raw: [u8; 10]) -> Result<Self> {
        if raw[0] != PROTOCOL_VERSION {
            return Err(BridgeError::message(
                "Relay inner protocol version mismatch",
            ));
        }
        let length = u32::from_be_bytes(raw[6..10].try_into().expect("four bytes"));
        if length as usize > MAX_INNER_FRAME {
            return Err(BridgeError::message("Relay inner frame exceeds the limit"));
        }
        Ok(Self {
            kind: raw[1],
            stream_id: u32::from_be_bytes(raw[2..6].try_into().expect("four bytes")),
            length,
        })
    }
}

pub fn relay_endpoint(base_url: &str) -> Option<RelayEndpoint> {
    let parsed = Url::parse(base_url.trim_end_matches('/')).ok()?;
    if !matches!(parsed.scheme(), "http" | "https")
        || parsed.query().is_some()
        || parsed.fragment().is_some()
        || !parsed.username().is_empty()
        || parsed.password().is_some()
    {
        return None;
    }
    let segments = parsed.path_segments()?.collect::<Vec<_>>();
    if segments.len() != 2 || segments[0] != "i" || !valid_instance_id(segments[1]) {
        return None;
    }
    let mut origin = parsed.clone();
    origin.set_path("");
    let public_origin = origin.as_str().trim_end_matches('/').to_owned();
    let websocket_scheme = if parsed.scheme() == "https" {
        "wss"
    } else {
        "ws"
    };
    let mut websocket = parsed.clone();
    websocket.set_scheme(websocket_scheme).ok()?;
    websocket.set_path(&format!("/i/{}/_relay/v1/connect", segments[1]));
    let websocket_url = websocket.to_string();
    Some(RelayEndpoint {
        public_origin,
        instance_id: segments[1].to_owned(),
        websocket_url,
    })
}

pub fn is_relay_update_target(target: &str) -> bool {
    let path = target.split('?').next().unwrap_or(target);
    let segments = path
        .split('/')
        .filter(|value| !value.is_empty())
        .collect::<Vec<_>>();
    match segments.as_slice() {
        ["api", "desktop-updates", "assets", version, filename] => {
            safe_path_component(version) && safe_path_component(filename)
        }
        ["api", "desktop-updates", target, arch, version] => {
            matches!(*target, "darwin" | "linux" | "windows")
                && safe_path_component(arch)
                && safe_path_component(version)
        }
        _ => false,
    }
}

fn safe_path_component(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 200
        && value != "."
        && value != ".."
        && !value.contains('\\')
        && !value.contains('%')
}

fn valid_instance_id(value: &str) -> bool {
    value.strip_prefix("cw_").is_some_and(|suffix| {
        (8..=80).contains(&suffix.len())
            && suffix
                .bytes()
                .all(|value| value.is_ascii_alphanumeric() || matches!(value, b'_' | b'-'))
    })
}

fn relay_token_bytes(token: &str) -> Result<Vec<u8>> {
    let encoded = token.strip_prefix(TOKEN_PREFIX).ok_or_else(|| {
        BridgeError::Config(
            "Relay requires a Coworker-generated high-entropy communication token".into(),
        )
    })?;
    let raw = URL_SAFE_NO_PAD
        .decode(encoded)
        .map_err(|_| BridgeError::Config("Relay communication token is malformed".into()))?;
    if raw.len() != 32 {
        return Err(BridgeError::Config(
            "Relay communication token must contain 32 random bytes".into(),
        ));
    }
    Ok(raw)
}

fn derive_signing_key(token: &str, instance_id: &str, purpose: &str) -> Result<SigningKey> {
    let _ = relay_token_bytes(token)?;
    let mut hasher = Sha256::new();
    hasher.update(DOMAIN);
    hasher.update([0]);
    hasher.update(instance_id.as_bytes());
    let salt = hasher.finalize();
    let hkdf = Hkdf::<Sha256>::new(Some(&salt), token.as_bytes());
    let mut info = Vec::with_capacity(DOMAIN.len() + purpose.len() + 1);
    info.extend_from_slice(DOMAIN);
    info.push(0);
    info.extend_from_slice(purpose.as_bytes());
    let mut seed = [0_u8; 32];
    hkdf.expand(&info, &mut seed)
        .map_err(|_| BridgeError::message("Relay key derivation failed"))?;
    Ok(SigningKey::from_bytes(&seed))
}

fn challenge_payload(
    kind: &str,
    instance_id: &str,
    connection_id: &str,
    nonce: &str,
    epoch: u64,
    expires_at: i64,
) -> String {
    [
        "coworker-relay-v1",
        kind,
        instance_id,
        connection_id,
        nonce,
        &epoch.to_string(),
        &expires_at.to_string(),
    ]
    .join("\n")
}

fn client_proof_payload(instance_id: &str, session_id: &str, nonce: &str) -> String {
    [
        "coworker-relay-v1",
        "inner-client",
        instance_id,
        session_id,
        nonce,
    ]
    .join("\n")
}

pub async fn request(
    base_url: &str,
    token: &str,
    method: &str,
    target: &str,
    mut headers: Vec<(String, String)>,
    body: Vec<u8>,
) -> Result<RelayResponse> {
    let endpoint = relay_endpoint(base_url)
        .ok_or_else(|| BridgeError::Config("invalid Relay Base URL".into()))?;
    if !headers
        .iter()
        .any(|(name, _)| name.eq_ignore_ascii_case("authorization"))
    {
        headers.push(("authorization".into(), format!("Bearer {token}")));
    }
    let mut connection = connect(&endpoint, token).await?;
    write_frame(
        &mut connection.tls,
        REQUEST_START,
        1,
        &serde_json::to_vec(&json!({
            "method": method,
            "path": target.split('?').next().unwrap_or(target),
            "target": target,
            "headers": headers,
        }))?,
    )
    .await?;
    for chunk in body.chunks(64 * 1024) {
        write_frame(&mut connection.tls, REQUEST_BODY, 1, chunk).await?;
    }
    write_frame(&mut connection.tls, REQUEST_END, 1, &[]).await?;
    read_response(&mut connection.tls, 1, None, None).await
}

pub async fn stream_request(
    base_url: &str,
    token: &str,
    method: &str,
    target: &str,
    mut headers: Vec<(String, String)>,
    events: mpsc::Sender<RelayStreamEvent>,
) -> Result<()> {
    let endpoint = relay_endpoint(base_url)
        .ok_or_else(|| BridgeError::Config("invalid Relay Base URL".into()))?;
    if !headers
        .iter()
        .any(|(name, _)| name.eq_ignore_ascii_case("authorization"))
    {
        headers.push(("authorization".into(), format!("Bearer {token}")));
    }
    let mut connection = connect(&endpoint, token).await?;
    write_frame(
        &mut connection.tls,
        REQUEST_START,
        1,
        &serde_json::to_vec(&json!({
            "method": method,
            "path": target.split('?').next().unwrap_or(target),
            "target": target,
            "headers": headers,
        }))?,
    )
    .await?;
    write_frame(&mut connection.tls, REQUEST_END, 1, &[]).await?;
    loop {
        let (kind, stream_id, payload) = read_frame(&mut connection.tls).await?;
        if stream_id != 1 {
            continue;
        }
        let event = match kind {
            RESPONSE_START => {
                let (status, headers) = response_metadata(&payload)?;
                RelayStreamEvent::Start { status, headers }
            }
            RESPONSE_BODY => RelayStreamEvent::Body(payload),
            RESPONSE_END => RelayStreamEvent::End,
            RESPONSE_ERROR => {
                let error: Value = serde_json::from_slice(&payload)?;
                return Err(BridgeError::message(format!(
                    "Coworker Relay request failed: {}",
                    error["error"].as_str().unwrap_or("unknown error")
                )));
            }
            _ => return Err(BridgeError::message("unexpected Relay response frame")),
        };
        let finished = matches!(event, RelayStreamEvent::End);
        if events.send(event).await.is_err() {
            let _ = write_frame(&mut connection.tls, REQUEST_CANCEL, 1, &[]).await;
            return Ok(());
        }
        if finished {
            return Ok(());
        }
    }
}

pub async fn consume_sse(
    base_url: &str,
    token: &str,
    target: &str,
    messages: mpsc::Sender<String>,
    connected: oneshot::Sender<()>,
) -> Result<()> {
    let endpoint = relay_endpoint(base_url)
        .ok_or_else(|| BridgeError::Config("invalid Relay Base URL".into()))?;
    let mut connection = connect(&endpoint, token).await?;
    write_frame(
        &mut connection.tls,
        REQUEST_START,
        1,
        &serde_json::to_vec(&json!({
            "method": "GET",
            "path": target,
            "target": target,
            "headers": [
                ["accept", "text/event-stream"],
                ["authorization", format!("Bearer {token}")],
            ],
        }))?,
    )
    .await?;
    write_frame(&mut connection.tls, REQUEST_END, 1, &[]).await?;
    let result = read_response(&mut connection.tls, 1, Some(messages), Some(connected)).await;
    if result.is_err() {
        let _ = write_frame(&mut connection.tls, REQUEST_CANCEL, 1, &[]).await;
    }
    result.map(|_| ())
}

async fn connect(endpoint: &RelayEndpoint, token: &str) -> Result<RelayConnection> {
    let (mut websocket, _) = timeout(
        Duration::from_secs(15),
        connect_async(&endpoint.websocket_url),
    )
    .await
    .map_err(|_| BridgeError::message("Relay WebSocket connection timed out"))?
    .map_err(|error| BridgeError::message(format!("Relay WebSocket failed: {error}")))?;
    let challenge = websocket
        .next()
        .await
        .ok_or_else(|| BridgeError::message("Relay closed before authentication"))?
        .map_err(|error| BridgeError::message(format!("Relay authentication failed: {error}")))?;
    let challenge: Value = serde_json::from_str(
        challenge
            .to_text()
            .map_err(|_| BridgeError::message("Relay challenge was not text"))?,
    )?;
    if challenge["type"] != "auth_challenge"
        || challenge["protocol_version"] != PROTOCOL_VERSION
        || challenge["instance_id"] != endpoint.instance_id
    {
        return Err(BridgeError::message(
            "invalid Relay authentication challenge",
        ));
    }
    let connection_id = challenge["connection_id"]
        .as_str()
        .ok_or_else(|| BridgeError::message("Relay challenge lacks connection ID"))?;
    let nonce = challenge["nonce"]
        .as_str()
        .ok_or_else(|| BridgeError::message("Relay challenge lacks nonce"))?;
    let epoch = challenge["epoch"].as_u64().unwrap_or(0);
    let expires_at = challenge["expires_at"].as_i64().unwrap_or(0);
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs() as i64;
    if expires_at < now || expires_at > now + 60 {
        return Err(BridgeError::message("Relay challenge is expired"));
    }
    let payload = challenge_payload(
        "desktop",
        &endpoint.instance_id,
        connection_id,
        nonce,
        epoch,
        expires_at,
    );
    let entry_key = derive_signing_key(token, &endpoint.instance_id, "relay-entry-auth")?;
    websocket
        .send(Message::Text(
            serde_json::to_string(&json!({
                "type": "auth_proof",
                "connection_id": connection_id,
                "signature": URL_SAFE_NO_PAD.encode(entry_key.sign(payload.as_bytes()).to_bytes()),
            }))?
            .into(),
        ))
        .await
        .map_err(|error| BridgeError::message(format!("Relay authentication failed: {error}")))?;
    let authenticated = websocket
        .next()
        .await
        .ok_or_else(|| BridgeError::message("Relay closed during authentication"))?
        .map_err(|error| BridgeError::message(format!("Relay authentication failed: {error}")))?;
    let authenticated: Value = serde_json::from_str(
        authenticated
            .to_text()
            .map_err(|_| BridgeError::message("Relay authentication response was not text"))?,
    )?;
    if authenticated["type"] != "auth_ok" {
        return Err(BridgeError::message(format!(
            "Relay authentication rejected: {}",
            authenticated["error"].as_str().unwrap_or("unknown error")
        )));
    }
    let session_id = authenticated["session_id"]
        .as_str()
        .filter(|value| value.len() == 32)
        .ok_or_else(|| BridgeError::message("Relay response lacks a valid session ID"))?
        .to_owned();
    let (sink, stream) = websocket.split();
    let (tls_io, bridge_io) = tokio::io::duplex(512 * 1024);
    let (mut bridge_reader, mut bridge_writer) = tokio::io::split(bridge_io);
    let mut tasks = Vec::with_capacity(2);
    tasks.push(tokio::spawn(async move {
        let mut stream = stream;
        while let Some(message) = stream.next().await {
            let Ok(message) = message else { break };
            match message {
                Message::Binary(body) if body.len() <= MAX_OUTER_FRAME => {
                    if bridge_writer.write_all(&body).await.is_err() {
                        break;
                    }
                }
                Message::Close(_) => break,
                _ => {}
            }
        }
    }));
    tasks.push(tokio::spawn(async move {
        let mut sink = sink;
        let mut buffer = vec![0_u8; MAX_OUTER_FRAME];
        while let Ok(length) = bridge_reader.read(&mut buffer).await {
            if length == 0 {
                break;
            }
            if sink
                .send(Message::Binary(buffer[..length].to_vec().into()))
                .await
                .is_err()
            {
                break;
            }
        }
    }));
    let expected_server_key = derive_signing_key(token, &endpoint.instance_id, "inner-tls-server")?
        .verifying_key()
        .to_bytes();
    let provider = rustls::crypto::ring::default_provider();
    let verifier = Arc::new(PinnedServerVerifier {
        expected_key: expected_server_key,
        algorithms: provider.signature_verification_algorithms,
    });
    let config = rustls::ClientConfig::builder_with_provider(Arc::new(provider))
        .with_protocol_versions(&[&rustls::version::TLS13])
        .map_err(|error| BridgeError::message(format!("Relay TLS configuration failed: {error}")))?
        .dangerous()
        .with_custom_certificate_verifier(verifier)
        .with_no_client_auth();
    let server_name = ServerName::try_from("coworker-relay-inner.invalid")
        .map_err(|error| BridgeError::message(format!("Relay TLS name failed: {error}")))?;
    let mut tls = timeout(
        Duration::from_secs(15),
        TlsConnector::from(Arc::new(config)).connect(server_name, tls_io),
    )
    .await
    .map_err(|_| BridgeError::message("Relay inner TLS handshake timed out"))?
    .map_err(|error| BridgeError::message(format!("Relay inner TLS handshake failed: {error}")))?;
    let (kind, stream_id, challenge_body) = read_frame(&mut tls).await?;
    if kind != CLIENT_PROOF_CHALLENGE || stream_id != 0 {
        return Err(BridgeError::message(
            "Coworker did not request a client proof",
        ));
    }
    let proof_challenge: Value = serde_json::from_slice(&challenge_body)?;
    if proof_challenge["instance_id"] != endpoint.instance_id
        || proof_challenge["session_id"] != session_id
    {
        return Err(BridgeError::message(
            "Coworker client proof binding mismatch",
        ));
    }
    let proof_nonce = proof_challenge["nonce"]
        .as_str()
        .ok_or_else(|| BridgeError::message("Coworker client proof lacks nonce"))?;
    let proof_key = derive_signing_key(token, &endpoint.instance_id, "inner-client-proof")?;
    let proof_payload = client_proof_payload(&endpoint.instance_id, &session_id, proof_nonce);
    write_frame(
        &mut tls,
        CLIENT_PROOF,
        0,
        &serde_json::to_vec(&json!({
            "signature": URL_SAFE_NO_PAD.encode(proof_key.sign(proof_payload.as_bytes()).to_bytes()),
        }))?,
    )
    .await?;
    let (kind, stream_id, _) = read_frame(&mut tls).await?;
    if kind != CLIENT_READY || stream_id != 0 {
        return Err(BridgeError::message(
            "Coworker rejected the inner client proof",
        ));
    }
    Ok(RelayConnection { tls, tasks })
}

async fn write_frame(
    stream: &mut TlsStream<DuplexStream>,
    kind: u8,
    stream_id: u32,
    payload: &[u8],
) -> Result<()> {
    if payload.len() > MAX_INNER_FRAME {
        return Err(BridgeError::message("Relay inner frame exceeds the limit"));
    }
    let header = InnerFrameHeader {
        kind,
        stream_id,
        length: payload.len() as u32,
    }
    .encode();
    stream.write_all(&header).await?;
    stream.write_all(payload).await?;
    stream.flush().await?;
    Ok(())
}

async fn read_frame(stream: &mut TlsStream<DuplexStream>) -> Result<(u8, u32, Vec<u8>)> {
    let mut header = [0_u8; 10];
    stream.read_exact(&mut header).await?;
    let header = InnerFrameHeader::decode(header)?;
    let mut payload = vec![0_u8; header.length as usize];
    stream.read_exact(&mut payload).await?;
    Ok((header.kind, header.stream_id, payload))
}

async fn read_response(
    stream: &mut TlsStream<DuplexStream>,
    expected_stream: u32,
    mut sse_messages: Option<mpsc::Sender<String>>,
    mut connected: Option<oneshot::Sender<()>>,
) -> Result<RelayResponse> {
    let mut status = None;
    let mut headers = Vec::new();
    let mut body = Vec::new();
    let mut sse_buffer = String::new();
    let mut sse_current = Vec::<String>::new();
    loop {
        let (kind, stream_id, payload) = read_frame(stream).await?;
        if stream_id != expected_stream {
            continue;
        }
        match kind {
            RESPONSE_START => {
                let metadata = response_metadata(&payload)?;
                status = Some(metadata.0);
                headers = metadata.1;
                if status.is_some_and(|value| !(200..300).contains(&value)) {
                    continue;
                }
                if let Some(sender) = connected.take() {
                    let _ = sender.send(());
                }
            }
            RESPONSE_BODY => {
                if let Some(sender) = &mut sse_messages {
                    sse_buffer.push_str(&String::from_utf8_lossy(&payload));
                    while let Some(index) = sse_buffer.find('\n') {
                        let mut line = sse_buffer[..index].to_owned();
                        if line.ends_with('\r') {
                            line.pop();
                        }
                        sse_buffer.drain(..=index);
                        if line.is_empty() {
                            if !sse_current.is_empty() {
                                let message = std::mem::take(&mut sse_current).join("\n");
                                if sender.send(message).await.is_err() {
                                    return Ok(RelayResponse {
                                        status: status.unwrap_or(200),
                                        headers,
                                        body: Vec::new(),
                                    });
                                }
                            }
                        } else if let Some(value) = line.strip_prefix("data:") {
                            sse_current.push(value.strip_prefix(' ').unwrap_or(value).to_owned());
                        }
                    }
                } else {
                    body.extend_from_slice(&payload);
                }
            }
            RESPONSE_END => {
                return Ok(RelayResponse {
                    status: status.ok_or_else(|| {
                        BridgeError::message("Relay response ended before headers")
                    })?,
                    headers,
                    body,
                });
            }
            RESPONSE_ERROR => {
                let error: Value = serde_json::from_slice(&payload)?;
                return Err(BridgeError::message(format!(
                    "Coworker Relay request failed: {}",
                    error["error"].as_str().unwrap_or("unknown error")
                )));
            }
            _ => return Err(BridgeError::message("unexpected Relay response frame")),
        }
    }
}

fn response_metadata(payload: &[u8]) -> Result<(u16, Vec<(String, String)>)> {
    let metadata: Value = serde_json::from_slice(payload)?;
    let status = metadata["status"]
        .as_u64()
        .filter(|value| *value <= u16::MAX as u64)
        .ok_or_else(|| BridgeError::message("Relay response lacks a valid status"))?
        as u16;
    let headers = metadata["headers"]
        .as_array()
        .into_iter()
        .flatten()
        .filter_map(|item| {
            let values = item.as_array()?;
            Some((
                values.first()?.as_str()?.to_owned(),
                values.get(1)?.as_str()?.to_owned(),
            ))
        })
        .collect();
    Ok((status, headers))
}

#[derive(Clone)]
struct PinnedServerVerifier {
    expected_key: [u8; 32],
    algorithms: WebPkiSupportedAlgorithms,
}

impl fmt::Debug for PinnedServerVerifier {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("PinnedServerVerifier")
            .field("expected_key", &"<redacted>")
            .finish()
    }
}

impl ServerCertVerifier for PinnedServerVerifier {
    fn verify_server_cert(
        &self,
        end_entity: &CertificateDer<'_>,
        _intermediates: &[CertificateDer<'_>],
        _server_name: &ServerName<'_>,
        _ocsp_response: &[u8],
        _now: UnixTime,
    ) -> std::result::Result<ServerCertVerified, rustls::Error> {
        let (_, certificate) = parse_x509_certificate(end_entity.as_ref())
            .map_err(|_| rustls::Error::General("invalid Coworker E2EE certificate".into()))?;
        let key = certificate
            .tbs_certificate
            .subject_pki
            .subject_public_key
            .data
            .as_ref();
        if key != self.expected_key {
            return Err(rustls::Error::General(
                "Coworker E2EE certificate pin mismatch".into(),
            ));
        }
        Ok(ServerCertVerified::assertion())
    }

    fn verify_tls12_signature(
        &self,
        message: &[u8],
        cert: &CertificateDer<'_>,
        dss: &DigitallySignedStruct,
    ) -> std::result::Result<HandshakeSignatureValid, rustls::Error> {
        rustls::crypto::verify_tls12_signature(message, cert, dss, &self.algorithms)
    }

    fn verify_tls13_signature(
        &self,
        message: &[u8],
        cert: &CertificateDer<'_>,
        dss: &DigitallySignedStruct,
    ) -> std::result::Result<HandshakeSignatureValid, rustls::Error> {
        rustls::crypto::verify_tls13_signature(message, cert, dss, &self.algorithms)
    }

    fn supported_verify_schemes(&self) -> Vec<SignatureScheme> {
        self.algorithms.supported_schemes()
    }
}

#[cfg(test)]
mod tests {
    use super::{derive_signing_key, is_relay_update_target, relay_endpoint, relay_token_bytes};
    use base64::Engine as _;

    #[test]
    fn detects_only_exact_relay_base_urls() {
        let endpoint =
            relay_endpoint("http://127.0.0.1:8443/i/cw_abcdefgh").expect("relay endpoint");
        assert_eq!(endpoint.instance_id, "cw_abcdefgh");
        assert_eq!(
            endpoint.websocket_url,
            "ws://127.0.0.1:8443/i/cw_abcdefgh/_relay/v1/connect"
        );
        assert_eq!(
            relay_endpoint("http://[2001:db8::1]:8443/i/cw_abcdefgh")
                .expect("IPv6 Relay endpoint")
                .websocket_url,
            "ws://[2001:db8::1]:8443/i/cw_abcdefgh/_relay/v1/connect"
        );
        assert!(relay_endpoint("http://127.0.0.1:8000").is_none());
        assert!(relay_endpoint("http://host/i/cw_short").is_none());
        assert!(relay_endpoint("http://host/i/cw_abcdefgh/extra").is_none());
    }

    #[test]
    fn accepts_only_fixed_same_instance_update_routes() {
        assert!(is_relay_update_target(
            "/api/desktop-updates/darwin/aarch64/1.2.3"
        ));
        assert!(is_relay_update_target(
            "/api/desktop-updates/assets/1.2.3/app.tar.gz"
        ));
        assert!(!is_relay_update_target(
            "/api/desktop-updates/assets/../secret"
        ));
        assert!(!is_relay_update_target(
            "/api/desktop-updates/feed/v1/releases"
        ));
    }

    #[test]
    fn rejects_legacy_tokens_and_derives_stable_domain_keys() {
        assert!(relay_token_bytes("legacy-secret").is_err());
        let token = "cwct_v1_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA";
        assert_eq!(relay_token_bytes(token).expect("token").len(), 32);
        let entry =
            derive_signing_key(token, "cw_abcdefgh", "relay-entry-auth").expect("entry key");
        let inner =
            derive_signing_key(token, "cw_abcdefgh", "inner-client-proof").expect("inner key");
        assert_ne!(entry.to_bytes(), inner.to_bytes());
        assert_eq!(
            base64::engine::general_purpose::URL_SAFE_NO_PAD
                .encode(entry.verifying_key().to_bytes()),
            "_HrMmpvtYBpH-TfU1g5CzpFIz-JkUzOc9q8TTm_RRJw"
        );
    }
}
