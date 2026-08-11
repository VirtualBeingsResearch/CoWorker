#!/bin/bash
# Environment source template (subprocess / shell mode).
#
# Demonstrates the JSON-RPC protocol over stdin/stdout.
# The host reads each line of stdout as a JSON-RPC request and writes
# responses back to stdin.

# Helper: send a JSON-RPC request and read the response.
rpc_call() {
    local id=$1
    local method=$2
    local params=$3
    echo "{\"jsonrpc\":\"2.0\",\"id\":$id,\"method\":\"$method\",\"params\":$params}"
    read -r response
    echo "$response" >&2  # log to stderr for debugging
}

# Get config params (passed as COWORKER_SOURCE_CONFIG env var).
config="${COWORKER_SOURCE_CONFIG:-{}}"
url=$(echo "$config" | python3 -c "import sys,json; print(json.load(sys.stdin).get('url',''))" 2>/dev/null)

if [ -z "$url" ]; then
    rpc_call 1 "emit_signal" "{\"title\":\"Error\",\"content\":\"No url configured\",\"fingerprint\":\"config-error\"}"
    exit 0
fi

# Make an HTTP request via the host's http_get method.
rpc_call 2 "http_get" "{\"url\":\"$url\"}"
# Response is now in $response (parsed by the host).

# Emit a signal about what we found.
rpc_call 3 "emit_signal" "{\"title\":\"Checked $url\",\"content\":\"Poll completed\",\"fingerprint\":\"shell-poll-$(date +%Y%m%d%H%M)\"}"
