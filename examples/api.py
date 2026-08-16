#!/usr/bin/env python3
"""Coworker API interactive shell.

Commands:
  status                        — 查看 Agent 状态
  switch <provider> <model_id>  — 切换模型
  msg <content>                 — 发送消息
  sender <id>                   — 设置 sender_id（默认 api-cli）
  conversation <id|clear>       — 设置 conversation_id（默认不传）
  base <url>                    — 设置服务器地址
  token <value|clear>           — 设置通信令牌（默认读 COWORKER_API_TOKEN）
  help / ?                      — 显示帮助
  exit / quit / q               — 退出
"""
from __future__ import annotations

import json
import os
import shlex
import urllib.request
from urllib.error import HTTPError, URLError

BASE = "http://localhost:8000"
SENDER = "api-cli"
CONVERSATION_ID = ""
TOKEN = os.getenv("COWORKER_API_TOKEN", "")

PROVIDERS = ["anthropic", "openai", "deepseek", "qwen", "zhipu"]
MODEL_HINTS = {
    "anthropic": "claude-sonnet-4-6",
    "openai":    "gpt-4o",
    "deepseek":  "deepseek-chat",
    "qwen":      "qwen-turbo",
    "zhipu":     "glm-4",
}

# ANSI colors
GREEN  = "\033[32m"
RED    = "\033[31m"
YELLOW = "\033[33m"
CYAN   = "\033[36m"
GRAY   = "\033[90m"
BOLD   = "\033[1m"
RESET  = "\033[0m"


def do_request(method: str, path: str, body: dict | None = None) -> tuple[dict, bool]:
    url = BASE + path
    data = json.dumps(body).encode() if body else None
    headers = {"Content-Type": "application/json"} if data else {}
    if TOKEN:
        headers["Authorization"] = f"Bearer {TOKEN}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read()), True
    except HTTPError as e:
        try:
            detail = json.loads(e.read()).get("detail", str(e))
        except Exception:
            detail = str(e)
        return {"error": e.code, "detail": detail}, False
    except URLError as e:
        return {"error": "connection_failed", "detail": str(e.reason)}, False


def print_result(data: dict, ok: bool) -> None:
    color = GREEN if ok else RED
    text = json.dumps(data, indent=2, ensure_ascii=False)
    print(color + text + RESET)


def cmd_status() -> None:
    data, ok = do_request("GET", "/status")
    if ok and "is_running" in data:
        running  = f"{GREEN}true{RESET}"  if data.get("is_running")  else f"{RED}false{RESET}"
        sleeping = f"{YELLOW}true{RESET}" if data.get("is_sleeping") else "false"
        print(f"  running   {running}")
        print(f"  sleeping  {sleeping}")
        print(f"  provider  {CYAN}{data.get('provider', '—')}{RESET}")
        print(f"  model     {BOLD}{data.get('model', '—')}{RESET}")
        print(f"  cycles    {data.get('cycle_count', '—')}")
        if "provider" not in data:
            if data.get("communication_token_configured") is False:
                print(f"{YELLOW}  提示: 服务端尚未配置通信令牌，请在管理端设置 API__COMMUNICATION_TOKEN。{RESET}")
            else:
                print(f"{YELLOW}  提示: 使用 token <value> 设置令牌后查看完整模型与用量。{RESET}")
    else:
        print_result(data, ok)


def cmd_switch(args: list[str]) -> None:
    if len(args) < 2:
        print(f"{YELLOW}用法: switch <provider> <model_id>{RESET}")
        print(f"      provider: {', '.join(PROVIDERS)}")
        return
    provider, model_id = args[0], args[1]
    data, ok = do_request("POST", "/switch_model", {"provider": provider, "model_id": model_id})
    print_result(data, ok)


def cmd_msg(args: list[str]) -> None:
    if not args:
        print(f"{YELLOW}用法: msg <content>{RESET}")
        return
    content = " ".join(args)
    body = {"sender_id": SENDER, "content": content}
    if CONVERSATION_ID:
        body["conversation_id"] = CONVERSATION_ID
    data, ok = do_request("POST", "/messages", body)
    print_result(data, ok)


def print_help() -> None:
    print(f"""
{BOLD}命令列表{RESET}
  {CYAN}status{RESET}                        查看 Agent 状态
  {CYAN}switch{RESET} <provider> <model_id>  切换模型
  {CYAN}msg{RESET} <content>                 发送消息
  {CYAN}sender{RESET} <id>                   设置 sender_id（当前: {SENDER}）
  {CYAN}conversation{RESET} <id|clear>       设置 conversation_id
                                      当前: {CONVERSATION_ID or '不传'}
  {CYAN}base{RESET} <url>                    设置服务器地址（当前: {BASE}）
  {CYAN}token{RESET} <value|clear>           设置通信令牌（当前: {'已设置' if TOKEN else '未设置'}）
  {CYAN}help{RESET} / ?                      显示此帮助
  {CYAN}exit{RESET} / quit / q              退出

{BOLD}支持的 provider{RESET}: {', '.join(PROVIDERS)}
{BOLD}model 示例{RESET}: { ', '.join(MODEL_HINTS.values()) }
""")


def repl() -> None:
    global BASE, SENDER, CONVERSATION_ID, TOKEN
    print(f"{BOLD}Coworker API Shell{RESET}  {GRAY}输入 help 查看命令{RESET}")
    print(f"{GRAY}服务器: {BASE}  |  sender: {SENDER}  |  token: {'已设置' if TOKEN else '未设置'}{RESET}\n")

    while True:
        try:
            line = input(f"{CYAN}>{RESET} ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if not line:
            continue

        try:
            parts = shlex.split(line)
        except ValueError as e:
            print(f"{RED}解析错误: {e}{RESET}")
            continue

        cmd, args = parts[0].lower(), parts[1:]

        if cmd in ("exit", "quit", "q"):
            break
        elif cmd == "status":
            cmd_status()
        elif cmd == "switch":
            cmd_switch(args)
        elif cmd == "msg":
            cmd_msg(args)
        elif cmd == "sender":
            if args:
                SENDER = args[0]
                print(f"sender 已设为 {BOLD}{SENDER}{RESET}")
            else:
                print(f"当前 sender: {BOLD}{SENDER}{RESET}")
        elif cmd == "conversation":
            if args:
                CONVERSATION_ID = "" if args[0].lower() == "clear" else args[0]
                shown = CONVERSATION_ID or "不传"
                print(f"conversation_id 已设为 {BOLD}{shown}{RESET}")
            else:
                print(f"当前 conversation_id: {BOLD}{CONVERSATION_ID or '不传'}{RESET}")
        elif cmd == "base":
            if args:
                BASE = args[0].rstrip("/")
                print(f"base URL 已设为 {BOLD}{BASE}{RESET}")
            else:
                print(f"当前 base: {BOLD}{BASE}{RESET}")
        elif cmd == "token":
            if args:
                TOKEN = "" if args[0].lower() == "clear" else args[0]
                print(f"通信令牌已{'清除' if not TOKEN else '设置'}")
            else:
                print(f"通信令牌: {BOLD}{'已设置' if TOKEN else '未设置'}{RESET}")
        elif cmd in ("help", "?"):
            print_help()
        else:
            print(f"{YELLOW}未知命令: {cmd}  （输入 help 查看命令列表）{RESET}")


if __name__ == "__main__":
    repl()
