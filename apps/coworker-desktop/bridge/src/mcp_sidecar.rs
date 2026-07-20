use chrono::{Duration as ChronoDuration, Utc};
use std::path::Path;

use serde_json::{Value, json};
use tokio::{
    io::{AsyncBufReadExt, AsyncWriteExt, BufReader},
    net::{TcpListener, TcpStream},
    task::JoinHandle,
};

use crate::{
    actor::{ActorStreamEvent, publish_actor_stream_event},
    config::DesktopConfig,
    conversation_store::{ApprovalRequest, ConversationStore},
    coworker::CoworkerHttpClient,
    desktop_protocol::{ActorId, DesktopEnvelopeV1, DesktopEventType},
    error::{BridgeError, Result},
    ids::new_compact_id,
};

pub async fn run_proxy(ipc_port: u16, sidecar_token: &str) -> Result<()> {
    let stream = TcpStream::connect(("127.0.0.1", ipc_port)).await?;
    let (read, mut write) = stream.into_split();
    write.write_all(sidecar_token.as_bytes()).await?;
    write.write_all(b"\n").await?;
    write.flush().await?;
    let mut responses = BufReader::new(read).lines();
    let mut requests = BufReader::new(tokio::io::stdin()).lines();
    let mut stdout = tokio::io::stdout();
    while let Some(line) = requests.next_line().await? {
        write.write_all(line.as_bytes()).await?;
        write.write_all(b"\n").await?;
        write.flush().await?;
        let response = responses
            .next_line()
            .await?
            .ok_or_else(|| BridgeError::message("Desktop MCP IPC closed before responding"))?;
        stdout.write_all(response.as_bytes()).await?;
        stdout.write_all(b"\n").await?;
        stdout.flush().await?;
    }
    Ok(())
}

pub async fn serve_loopback(
    config_path: &Path,
    run_id: &str,
    sidecar_token: &str,
) -> Result<(u16, JoinHandle<()>)> {
    let config = DesktopConfig::from_file(config_path)?;
    let store = ConversationStore::open(config.storage_dir.join("desktop.sqlite3"))?;
    let http = CoworkerHttpClient::new()?;
    let listener = TcpListener::bind(("127.0.0.1", 0)).await?;
    let port = listener.local_addr()?.port();
    let expected_token = sidecar_token.to_owned();
    let run_id = run_id.to_owned();
    let handle = tokio::spawn(async move {
        let result: Result<()> = async {
            let (stream, peer) = listener.accept().await?;
            if !peer.ip().is_loopback() {
                return Err(BridgeError::message("MCP IPC rejected a non-loopback peer"));
            }
            let (read, mut write) = stream.into_split();
            let mut lines = BufReader::new(read).lines();
            let presented = lines.next_line().await?.unwrap_or_default();
            if presented != expected_token || !store.consume_actor_run_token(&run_id, &presented)? {
                return Err(BridgeError::message(
                    "invalid or already-used MCP sidecar token",
                ));
            }
            while let Some(line) = lines.next_line().await? {
                if line.trim().is_empty() {
                    continue;
                }
                let request: Value = serde_json::from_str(&line)?;
                let response = process_request(&config, &store, &http, &run_id, &request).await;
                write
                    .write_all(serde_json::to_string(&response)?.as_bytes())
                    .await?;
                write.write_all(b"\n").await?;
                write.flush().await?;
            }
            Ok(())
        }
        .await;
        if let Err(error) = result {
            tracing::warn!(%error, "Claude MCP loopback broker stopped");
        }
    });
    Ok((port, handle))
}

async fn process_request(
    config: &DesktopConfig,
    store: &ConversationStore,
    http: &CoworkerHttpClient,
    run_id: &str,
    request: &Value,
) -> Value {
    let Some(id) = request.get("id").cloned() else {
        return json!({"jsonrpc":"2.0","id":null,"error":{"code":-32600,"message":"Request id is required"}});
    };
    let method = request
        .get("method")
        .and_then(Value::as_str)
        .unwrap_or_default();
    match method {
        "initialize" => json!({
            "jsonrpc": "2.0", "id": id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "coworker-desktop", "version": env!("CARGO_PKG_VERSION")}
            }
        }),
        "tools/list" => json!({"jsonrpc":"2.0","id":id,"result":{"tools":tool_specs()}}),
        "tools/call" => {
            let params = request.get("params").and_then(Value::as_object);
            let name = params
                .and_then(|value| value.get("name"))
                .and_then(Value::as_str)
                .unwrap_or_default();
            let arguments = params
                .and_then(|value| value.get("arguments"))
                .cloned()
                .unwrap_or_else(|| json!({}));
            match handle_tool(config, store, http, run_id, name, &arguments).await {
                Ok(text) => {
                    json!({"jsonrpc":"2.0","id":id,"result":{"content":[{"type":"text","text":text}],"isError":false}})
                }
                Err(error) => {
                    json!({"jsonrpc":"2.0","id":id,"result":{"content":[{"type":"text","text":error.to_string()}],"isError":true}})
                }
            }
        }
        _ => json!({"jsonrpc":"2.0","id":id,"error":{"code":-32601,"message":"Method not found"}}),
    }
}

fn tool_specs() -> Value {
    json!([
        {
            "name": "list_coworkers",
            "description": "List Coworkers connected through CoWorker Desktop.",
            "inputSchema": {"type":"object","properties":{}}
        },
        {
            "name": "send_to_coworker",
            "description": "Explicitly send progress, a question, or a final handoff to a Coworker. A normal final answer is not forwarded.",
            "inputSchema": {
                "type":"object",
                "properties": {
                    "coworker_id":{"type":"string"},
                    "message":{"type":"string"}
                },
                "required":["coworker_id","message"]
            }
        },
        {
            "name": "request_permission",
            "description": "Route a Claude Code tool permission request through CoWorker Desktop.",
            "inputSchema": {
                "type":"object",
                "properties":{
                    "tool_name":{"type":"string"},
                    "input":{"type":"object"}
                },
                "required":["tool_name","input"]
            }
        }
    ])
}

async fn handle_tool(
    config: &DesktopConfig,
    store: &ConversationStore,
    http: &CoworkerHttpClient,
    run_id: &str,
    name: &str,
    arguments: &Value,
) -> Result<String> {
    match name {
        "list_coworkers" => Ok(serde_json::to_string(&config.codex.coworkers)?),
        "request_permission" => request_permission(config, store, http, run_id, arguments).await,
        "send_to_coworker" => {
            let coworker_id = arguments
                .get("coworker_id")
                .and_then(Value::as_str)
                .unwrap_or_default();
            let message = arguments
                .get("message")
                .and_then(Value::as_str)
                .unwrap_or_default();
            if coworker_id.is_empty() || message.trim().is_empty() {
                return Err(BridgeError::message("coworker_id and message are required"));
            }
            let coworker = config
                .codex
                .coworkers
                .iter()
                .find(|item| item.coworker_id == coworker_id)
                .ok_or_else(|| BridgeError::message(format!("Unknown Coworker: {coworker_id}")))?;
            let registration = store
                .registration(coworker_id, ActorId::Claude)?
                .ok_or_else(|| BridgeError::message("Claude participant is not registered"))?;
            let conversation_id = store
                .actor_run_conversation(run_id)?
                .ok_or_else(|| BridgeError::message("Claude session id is not available yet"))?;
            let mut envelope = DesktopEnvelopeV1::new(
                DesktopEventType::ThreadEvent,
                json!({
                    "message": message,
                }),
            );
            envelope.conversation_id = Some(conversation_id);
            store.enqueue(coworker_id, &envelope)?;
            let token = config
                .security
                .bearer_tokens
                .get(coworker_id)
                .map(String::as_str);
            let ack = http
                .post_desktop_envelope(
                    coworker,
                    &registration.participant_id,
                    &envelope,
                    token,
                    config.security.development_mode,
                )
                .await?;
            if ack.accepted {
                store.acknowledge(&envelope.message_id)?;
            }
            Ok(format!("Message sent to {coworker_id}"))
        }
        _ => Err(BridgeError::message(format!("Unknown MCP tool: {name}"))),
    }
}

async fn request_permission(
    config: &DesktopConfig,
    store: &ConversationStore,
    http: &CoworkerHttpClient,
    run_id: &str,
    arguments: &Value,
) -> Result<String> {
    let tool_name = arguments
        .get("tool_name")
        .and_then(Value::as_str)
        .unwrap_or_default();
    let is_user_input = tool_name == "AskUserQuestion";
    let input = arguments.get("input").cloned().unwrap_or(Value::Null);
    if tool_name.is_empty() || !input.is_object() {
        return Err(BridgeError::message(
            "tool_name and object input are required",
        ));
    }
    let coworker_id = store
        .actor_run_coworker_id(run_id)?
        .ok_or_else(|| BridgeError::message("Claude run is not associated with a Coworker"))?;
    let coworker = config
        .codex
        .coworkers
        .iter()
        .find(|item| item.coworker_id == coworker_id)
        .ok_or_else(|| BridgeError::message(format!("Unknown Coworker: {coworker_id}")))?;
    let registration = store
        .registration(&coworker_id, ActorId::Claude)?
        .ok_or_else(|| BridgeError::message("Claude participant is not registered"))?;
    let conversation_id = store
        .actor_run_conversation(run_id)?
        .ok_or_else(|| BridgeError::message("Claude session id is not available yet"))?;
    let request_id = new_compact_id("req_");
    let timeout_seconds = config.codex.approval_timeout_seconds.max(1);
    let request = ApprovalRequest {
        request_id: request_id.clone(),
        actor_id: ActorId::Claude,
        conversation_id: conversation_id.clone(),
        coworker_id: coworker_id.clone(),
        owner_id: config.desktop_id.clone(),
        tool_name: tool_name.to_owned(),
        input: input.clone(),
        status: "pending".to_owned(),
        response: None,
        expires_at: Utc::now() + ChronoDuration::seconds(timeout_seconds as i64),
        server_request_id: None,
    };
    if is_user_input {
        store.create_user_input(&request)?;
    } else {
        store.create_approval(&request)?;
    }
    tracing::info!(
        %request_id,
        %conversation_id,
        %coworker_id,
        desktop_id = %config.desktop_id,
        %tool_name,
        request_kind = if is_user_input { "user_input" } else { "approval" },
        "Created SQLite Claude callback request"
    );
    let event_type = if is_user_input {
        DesktopEventType::UserInputRequested
    } else {
        DesktopEventType::ApprovalRequested
    };
    let payload = if is_user_input {
        json!({
            "actor_id": "claude",
            "request_id": request_id,
            "session_id": conversation_id,
            "coworker_id": coworker_id,
            "method": tool_name,
            "params": input,
            "tool_name": tool_name,
            "input": input,
            "resolve_hint": {
                "field": "answers",
                "request_id_field": "user_input_request_id",
                "note": "Map each questions[].question string to the selected label or free-form answer."
            },
            "expires_at": request.expires_at,
        })
    } else {
        json!({
            "actor_id": "claude",
            "request_id": request_id,
            "session_id": conversation_id,
            "coworker_id": coworker_id,
            "tool_name": tool_name,
            "input": input,
            "expires_at": request.expires_at,
        })
    };
    let mut envelope = DesktopEnvelopeV1::new(event_type, payload);
    envelope.request_id = Some(request_id.clone());
    envelope.conversation_id = Some(conversation_id.clone());
    store.enqueue(&coworker_id, &envelope)?;
    let token = config
        .security
        .bearer_tokens
        .get(&coworker_id)
        .map(String::as_str);
    match http
        .post_desktop_envelope(
            coworker,
            &registration.participant_id,
            &envelope,
            token,
            config.security.development_mode,
        )
        .await
    {
        Ok(ack) if ack.accepted => store.acknowledge(&envelope.message_id)?,
        Ok(_) => store.mark_dead_letter(
            &envelope.message_id,
            if is_user_input {
                "Coworker rejected user input request"
            } else {
                "Coworker rejected approval request"
            },
        )?,
        Err(error) => store.schedule_retry(&envelope.message_id, &error.to_string())?,
    }
    let deadline = tokio::time::Instant::now() + std::time::Duration::from_secs(timeout_seconds);
    while tokio::time::Instant::now() < deadline {
        if let Some(current) = store.approval(&request_id)?
            && current.status == "resolved"
            && let Some(response) = current.response
        {
            let behavior = response
                .get("behavior")
                .and_then(Value::as_str)
                .unwrap_or("deny");
            if !matches!(behavior, "allow" | "deny") {
                return Ok(serde_json::to_string(
                    &json!({"behavior":"deny","message":"Invalid approval response"}),
                )?);
            }
            return Ok(serde_json::to_string(&response)?);
        }
        tokio::time::sleep(std::time::Duration::from_millis(200)).await;
    }
    store.expire_approval(&request_id)?;
    let changed_event_type = if is_user_input {
        "desktop.user_input.changed"
    } else {
        "desktop.approval.changed"
    };
    publish_actor_stream_event(ActorStreamEvent {
        actor_id: ActorId::Claude,
        conversation_id: conversation_id.clone(),
        message_id: None,
        event: json!({
            "type": changed_event_type,
            "request_id": request_id,
            "actor_id": "claude",
            "status": "expired",
            "resolver": "timeout",
        }),
    });
    Ok(serde_json::to_string(&json!({
        "behavior": "deny",
        "message": if is_user_input {
            "CoWorker Desktop user input timed out."
        } else {
            "CoWorker Desktop approval timed out."
        }
    }))?)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::conversation_store::ConversationStore;

    #[test]
    fn permission_tool_uses_claude_code_standard_input() {
        let tools = tool_specs();
        let permission = tools
            .as_array()
            .unwrap()
            .iter()
            .find(|tool| tool["name"] == "request_permission")
            .unwrap();
        assert_eq!(
            permission["inputSchema"]["required"],
            json!(["tool_name", "input"])
        );
        assert!(permission["inputSchema"]["properties"]["coworker_id"].is_null());
    }

    #[tokio::test]
    async fn loopback_broker_consumes_token_and_serves_mcp() {
        let root =
            std::env::temp_dir().join(format!("coworker-mcp-loopback-{}", uuid::Uuid::new_v4()));
        std::fs::create_dir_all(&root).unwrap();
        let config_path = root.join("coworker_desktop.json");
        std::fs::write(
            &config_path,
            serde_json::to_string(&json!({
                "schema_version": 2,
                "desktop_id": "desk-test",
                "codex_id": "codex-test",
                "storage_dir": root,
                "coworkers": [{"coworker_id":"cw-test","base_url":"http://localhost:1"}],
                "security": {"development_mode": true},
                "actors": {"codex":{"enabled":false},"claude":{"enabled":true}}
            }))
            .unwrap(),
        )
        .unwrap();
        let store = ConversationStore::open(root.join("desktop.sqlite3")).unwrap();
        store
            .set_actor_run("run-test", ActorId::Claude, Some("session-test"))
            .unwrap();
        store.set_actor_run_token("run-test", "one-time").unwrap();
        let (port, broker) = serve_loopback(&config_path, "run-test", "one-time")
            .await
            .unwrap();
        let stream = TcpStream::connect(("127.0.0.1", port)).await.unwrap();
        let (read, mut write) = stream.into_split();
        let mut responses = BufReader::new(read).lines();
        write.write_all(b"one-time\n").await.unwrap();
        write
            .write_all(b"{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"initialize\"}\n")
            .await
            .unwrap();
        write.flush().await.unwrap();
        let response = responses.next_line().await.unwrap().unwrap();
        assert_eq!(serde_json::from_str::<Value>(&response).unwrap()["id"], 1);
        write
            .write_all(
                b"{\"jsonrpc\":\"2.0\",\"id\":2,\"method\":\"tools/call\",\"params\":{\"name\":\"list_coworkers\",\"arguments\":{}}}\n",
            )
            .await
            .unwrap();
        write.flush().await.unwrap();
        let response = responses.next_line().await.unwrap().unwrap();
        let response = serde_json::from_str::<Value>(&response).unwrap();
        assert_eq!(response["id"], 2);
        assert_eq!(response["result"]["isError"], false);
        assert!(
            response["result"]["content"][0]["text"]
                .as_str()
                .unwrap()
                .contains("cw-test")
        );
        assert!(
            !store
                .consume_actor_run_token("run-test", "one-time")
                .unwrap()
        );
        broker.abort();
        let _ = std::fs::remove_dir_all(root);
    }
}
