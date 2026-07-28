#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 3 ]]; then
  echo "用法: $0 <搭档地址> <通信token> <消息> [发送方ID]" >&2
  exit 2
fi

coworker_url="${1%/}"
communication_token="$2"
message="$3"
sender_id="${4:-jenkins}"

payload="$(
  jq -n \
    --arg sender_id "$sender_id" \
    --arg content "$message" \
    '{sender_id: $sender_id, content: $content}'
)"

curl --fail --silent --show-error \
  -X POST "${coworker_url}/messages" \
  -H "Authorization: Bearer ${communication_token}" \
  -H "Content-Type: application/json" \
  --data "$payload"
