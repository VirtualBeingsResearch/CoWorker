#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 3 || $# -gt 4 ]]; then
  echo "Usage: $0 <Coworker URL> <communication token> [sender ID] <message>" >&2
  echo "用法: $0 <搭档地址> <通信token> [发送方ID] <消息>" >&2
  exit 2
fi

coworker_url="${1%/}"
communication_token="$2"

if [[ $# -eq 3 ]]; then
  sender_id="external:anonymous-notification"
  message="$3"
else
  sender_id="${3:-external:anonymous-notification}"
  message="$4"
fi

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
