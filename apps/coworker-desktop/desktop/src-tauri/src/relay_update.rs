use std::sync::Arc;

use coworker_desktop_core::relay_transport;
use serde_json::Value;
use tokio::{
    io::{AsyncReadExt, AsyncWriteExt},
    net::{TcpListener, TcpStream},
    sync::{mpsc, watch},
    task::JoinHandle,
};
use uuid::Uuid;

pub struct RelayUpdateAdapter {
    endpoint: String,
    shutdown: Option<watch::Sender<bool>>,
    task: JoinHandle<()>,
}

impl RelayUpdateAdapter {
    pub async fn start(relay_base: String, token: String) -> Result<Self, String> {
        if relay_transport::relay_endpoint(&relay_base).is_none() {
            return Err("update adapter requires a Relay Base URL".into());
        }
        let listener = TcpListener::bind("127.0.0.1:0")
            .await
            .map_err(|error| error.to_string())?;
        let address = listener.local_addr().map_err(|error| error.to_string())?;
        let capability = Uuid::new_v4().simple().to_string();
        let local_base = format!("http://{address}/{capability}");
        let endpoint = format!(
            "{local_base}/api/desktop-updates/{{{{target}}}}/{{{{arch}}}}/{{{{current_version}}}}"
        );
        let (shutdown, mut shutdown_rx) = watch::channel(false);
        let relay_base = Arc::new(relay_base);
        let token = Arc::new(token);
        let local_base = Arc::new(local_base);
        let capability = Arc::new(capability);
        let task = tokio::spawn(async move {
            loop {
                tokio::select! {
                    result = shutdown_rx.changed() => {
                        if result.is_err() || *shutdown_rx.borrow() {
                            break;
                        }
                    },
                    accepted = listener.accept() => {
                        let Ok((stream, peer)) = accepted else { break };
                        if !peer.ip().is_loopback() {
                            continue;
                        }
                        let relay_base = relay_base.clone();
                        let token = token.clone();
                        let local_base = local_base.clone();
                        let capability = capability.clone();
                        let mut request_shutdown = shutdown_rx.clone();
                        tokio::spawn(async move {
                            tokio::select! {
                                _ = request_shutdown.changed() => {}
                                _ = handle(
                                    stream,
                                    relay_base.as_str(),
                                    token.as_str(),
                                    local_base.as_str(),
                                    capability.as_str(),
                                ) => {}
                            }
                        });
                    }
                }
            }
        });
        Ok(Self {
            endpoint,
            shutdown: Some(shutdown),
            task,
        })
    }

    pub fn endpoint(&self) -> &str {
        &self.endpoint
    }
}

impl Drop for RelayUpdateAdapter {
    fn drop(&mut self) {
        if let Some(shutdown) = self.shutdown.take() {
            let _ = shutdown.send(true);
        }
        self.task.abort();
    }
}

async fn handle(
    mut stream: TcpStream,
    relay_base: &str,
    token: &str,
    local_base: &str,
    capability: &str,
) -> Result<(), String> {
    let mut buffer = Vec::with_capacity(4096);
    let mut chunk = [0_u8; 2048];
    loop {
        let length = stream
            .read(&mut chunk)
            .await
            .map_err(|error| error.to_string())?;
        if length == 0 || buffer.len() + length > 32 * 1024 {
            return send_error(&mut stream, 400, "invalid_request").await;
        }
        buffer.extend_from_slice(&chunk[..length]);
        if buffer.windows(4).any(|window| window == b"\r\n\r\n") {
            break;
        }
    }
    let head = std::str::from_utf8(&buffer).map_err(|error| error.to_string())?;
    let request_line = head.lines().next().ok_or("missing request line")?;
    let mut parts = request_line.split_whitespace();
    let method = parts.next().unwrap_or("");
    let local_target = parts.next().unwrap_or("");
    if method != "GET" {
        return send_error(&mut stream, 405, "method_not_allowed").await;
    }
    let prefix = format!("/{capability}");
    let Some(target) = local_target.strip_prefix(&prefix) else {
        return send_error(&mut stream, 404, "not_found").await;
    };
    if !relay_transport::is_relay_update_target(target) {
        return send_error(&mut stream, 404, "route_not_exposed").await;
    }
    if target.starts_with("/api/desktop-updates/assets/") {
        return stream_asset(&mut stream, relay_base, token, target).await;
    }
    let response = relay_transport::request(
        relay_base,
        token,
        "GET",
        target,
        vec![("accept".into(), "*/*".into())],
        vec![],
    )
    .await
    .map_err(|error| error.to_string())?;
    if (300..400).contains(&response.status) {
        return send_error(&mut stream, 502, "redirect_rejected").await;
    }
    let mut body = response.body;
    let content_type = safe_content_type(&response.headers);
    if response.status == 200 {
        let mut manifest: Value =
            serde_json::from_slice(&body).map_err(|error| error.to_string())?;
        let url = manifest
            .get_mut("url")
            .and_then(|value| value.as_str())
            .ok_or("update manifest lacks url")?
            .to_owned();
        let asset_target = url
            .strip_prefix(relay_base.trim_end_matches('/'))
            .ok_or("update asset is outside the selected Relay instance")?;
        if !relay_transport::is_relay_update_target(asset_target)
            || !asset_target.starts_with("/api/desktop-updates/assets/")
        {
            return send_error(&mut stream, 502, "update_asset_rejected").await;
        }
        manifest["url"] = Value::String(format!("{local_base}{asset_target}"));
        body = serde_json::to_vec(&manifest).map_err(|error| error.to_string())?;
    }
    let reason = match response.status {
        200 => "OK",
        204 => "No Content",
        404 => "Not Found",
        401 => "Unauthorized",
        _ => "Relay Response",
    };
    let head = format!(
        "HTTP/1.1 {} {}\r\nContent-Type: {}\r\nContent-Length: {}\r\n\
         Cache-Control: no-store\r\nConnection: close\r\n\r\n",
        response.status,
        reason,
        content_type,
        body.len(),
    );
    stream
        .write_all(head.as_bytes())
        .await
        .map_err(|error| error.to_string())?;
    stream
        .write_all(&body)
        .await
        .map_err(|error| error.to_string())?;
    Ok(())
}

async fn stream_asset(
    stream: &mut TcpStream,
    relay_base: &str,
    token: &str,
    target: &str,
) -> Result<(), String> {
    let (events_tx, mut events_rx) = mpsc::channel(8);
    let relay_base = relay_base.to_owned();
    let token = token.to_owned();
    let target = target.to_owned();
    let task = tokio::spawn(async move {
        relay_transport::stream_request(
            &relay_base,
            &token,
            "GET",
            &target,
            vec![("accept".into(), "application/octet-stream".into())],
            events_tx,
        )
        .await
    });
    let mut started = false;
    while let Some(event) = events_rx.recv().await {
        match event {
            relay_transport::RelayStreamEvent::Start { status, headers } => {
                if (300..400).contains(&status) {
                    task.abort();
                    return send_error(stream, 502, "redirect_rejected").await;
                }
                let reason = match status {
                    200 => "OK",
                    204 => "No Content",
                    401 => "Unauthorized",
                    404 => "Not Found",
                    _ => "Relay Response",
                };
                let head = format!(
                    "HTTP/1.1 {status} {reason}\r\nContent-Type: {}\r\n\
                     Transfer-Encoding: chunked\r\nCache-Control: no-store\r\n\
                     Connection: close\r\n\r\n",
                    safe_content_type(&headers),
                );
                stream
                    .write_all(head.as_bytes())
                    .await
                    .map_err(|error| error.to_string())?;
                started = true;
            }
            relay_transport::RelayStreamEvent::Body(body) => {
                if !started {
                    task.abort();
                    return send_error(stream, 502, "response_headers_missing").await;
                }
                stream
                    .write_all(format!("{:X}\r\n", body.len()).as_bytes())
                    .await
                    .map_err(|error| error.to_string())?;
                stream
                    .write_all(&body)
                    .await
                    .map_err(|error| error.to_string())?;
                stream
                    .write_all(b"\r\n")
                    .await
                    .map_err(|error| error.to_string())?;
            }
            relay_transport::RelayStreamEvent::End => {
                if !started {
                    task.abort();
                    return send_error(stream, 502, "response_headers_missing").await;
                }
                stream
                    .write_all(b"0\r\n\r\n")
                    .await
                    .map_err(|error| error.to_string())?;
                break;
            }
        }
    }
    task.await
        .map_err(|error| error.to_string())?
        .map_err(|error| error.to_string())
}

fn safe_content_type(headers: &[(String, String)]) -> &str {
    headers
        .iter()
        .find(|(name, value)| {
            name.eq_ignore_ascii_case("content-type")
                && value.is_ascii()
                && value.len() <= 200
                && !value.contains(['\r', '\n'])
        })
        .map(|(_, value)| value.as_str())
        .unwrap_or("application/octet-stream")
}

async fn send_error(stream: &mut TcpStream, status: u16, code: &str) -> Result<(), String> {
    let body = serde_json::to_vec(&serde_json::json!({"error": code}))
        .map_err(|error| error.to_string())?;
    let head = format!(
        "HTTP/1.1 {status} Error\r\nContent-Type: application/json\r\n\
         Content-Length: {}\r\nCache-Control: no-store\r\nConnection: close\r\n\r\n",
        body.len(),
    );
    stream
        .write_all(head.as_bytes())
        .await
        .map_err(|error| error.to_string())?;
    stream
        .write_all(&body)
        .await
        .map_err(|error| error.to_string())
}

#[cfg(test)]
mod tests {
    use super::safe_content_type;

    #[test]
    fn content_type_rejects_response_splitting() {
        let safe = vec![("Content-Type".into(), "application/json".into())];
        assert_eq!(safe_content_type(&safe), "application/json");
        let injected = vec![(
            "Content-Type".into(),
            "application/octet-stream\r\nX-Forged: yes".into(),
        )];
        assert_eq!(safe_content_type(&injected), "application/octet-stream");
    }
}
