#!/usr/bin/env python3
"""支持 Kimi 开放平台 API 的极简 coding agent"""

import glob as globlib
import json
import os
import re
import subprocess
import urllib.error
import urllib.request


API_KEY = os.environ.get("KIMI_API_KEY")
BASE_URL = os.environ.get("KIMI_BASE_URL", "https://api.moonshot.cn/v1").rstrip("/")
MODEL = os.environ.get("MODEL", "kimi-k3")
MAX_TOKENS = int(os.environ.get("MAX_COMPLETION_TOKENS", "32768"))


# ---------- 工具实现 ----------

def read(args):
    with open(args["path"], encoding="utf-8") as f:
        lines = f.readlines()
    offset = int(args.get("offset", 0))
    limit = int(args.get("limit", 200))
    return "".join(
        f"{offset + i + 1:4}| {line}"
        for i, line in enumerate(lines[offset:offset + limit])
    ) or "(empty)"


def write(args):
    with open(args["path"], "w", encoding="utf-8") as f:
        f.write(args["content"])
    return "ok"


def edit(args):
    with open(args["path"], encoding="utf-8") as f:
        text = f.read()
    old, new = args["old"], args["new"]
    count = text.count(old)
    if count == 0:
        return "error: old_string not found"
    if not args.get("all") and count > 1:
        return f"error: old_string appears {count} times, use all=true"
    text = text.replace(old, new) if args.get("all") else text.replace(old, new, 1)
    with open(args["path"], "w", encoding="utf-8") as f:
        f.write(text)
    return "ok"


def glob(args):
    root = args.get("path", ".")
    files = globlib.glob(os.path.join(root, args["pat"]), recursive=True)
    files.sort(key=lambda p: os.path.getmtime(p) if os.path.isfile(p) else 0, reverse=True)
    return "\n".join(files) or "none"


def grep(args):
    pattern = re.compile(args["pat"])
    root = args.get("path", ".")
    hits = []
    for path in globlib.glob(os.path.join(root, "**"), recursive=True):
        if not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8", errors="ignore") as f:
                for n, line in enumerate(f, 1):
                    if pattern.search(line):
                        hits.append(f"{path}:{n}:{line.rstrip()}")
                        if len(hits) == 50:
                            return "\n".join(hits)
        except OSError:
            pass
    return "\n".join(hits) or "none"


def bash(args):
    try:
        p = subprocess.run(
            args["cmd"], shell=True, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=30,
        )
        out = p.stdout.rstrip()
        return (f"exit code: {p.returncode}\n{out}" if p.returncode else out) or "(empty)"
    except subprocess.TimeoutExpired:
        return "timed out after 30s"


# 每个工具包含：描述、参数 schema、Python 实现
TOOLS = {
    "read": (
        "Read a file with line numbers.",
        {"path": {"type": "string"}, "offset": {"type": "integer"}, "limit": {"type": "integer"}},
        read,
    ),
    "write": (
        "Write or overwrite a file.",
        {"path": {"type": "string"}, "content": {"type": "string"}},
        write,
    ),
    "edit": (
        "Replace old with new in a file. old must be unique unless all=true.",
        {"path": {"type": "string"}, "old": {"type": "string"}, "new": {"type": "string"}, "all": {"type": "boolean"}},
        edit,
    ),
    "glob": (
        "Find files by glob pattern.",
        {"pat": {"type": "string"}, "path": {"type": "string"}},
        glob,
    ),
    "grep": (
        "Search files with a regular expression.",
        {"pat": {"type": "string"}, "path": {"type": "string"}},
        grep,
    ),
    "bash": (
        "Run a shell command.",
        {"cmd": {"type": "string"}},
        bash,
    ),
}


# 将工具定义转换成 Kimi (OpenAI-compatible) 的 Function Calling 格式
def make_tools():
    tools = []
    for name, (description, properties, _) in TOOLS.items():
        required = [k for k, v in properties.items() if k in ("path", "content", "old", "new", "pat", "cmd")]
        tools.append({
            "type": "function",
            "function": {
                "name": name,
                "description": description,
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": required,
                },
            },
        })
    return tools


def run_tool(name, args):
    try:
        return TOOLS[name][2](args)
    except Exception as e:
        return f"error: {type(e).__name__}: {e}"


# ---------- Kimi API ----------

def call_kimi(messages, system_prompt):
    if not API_KEY:
        raise RuntimeError("请设置 KIMI_API_KEY")

    body = {
        "model": MODEL,
        "messages": [{"role": "system", "content": system_prompt}] + messages,
        "tools": make_tools(),
        "max_completion_tokens": MAX_TOKENS,
    }

    effort = os.environ.get("REASONING_EFFORT")
    if effort:
        body["reasoning_effort"] = effort

    req = urllib.request.Request(
        f"{BASE_URL}/chat/completions",
        data=json.dumps(body, ensure_ascii=False).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {API_KEY}",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            data = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        raise RuntimeError(f"Kimi API HTTP {e.code}: {detail}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"连接 Kimi API 失败: {e.reason}") from e

    try:
        return data["choices"][0]["message"]
    except (KeyError, IndexError, TypeError) as e:
        raise RuntimeError(f"Kimi API 返回格式异常: {data}") from e


# ---------- Agent ----------

def run_agent(user_input, messages, system_prompt):
    # messages 为 Agent 的记忆：保存用户消息、模型消息、工具结果。
    messages.append({"role": "user", "content": user_input})

    while True:
        assistant = call_kimi(messages, system_prompt)

        # 保存完整的 assistant message，不能丢 K3 的 reasoning_content
        messages.append(assistant)

        if assistant.get("content"):
            print(assistant["content"])

        tool_calls = assistant.get("tool_calls") or []
        if not tool_calls:
            return

        for call in tool_calls:
            name = call["function"]["name"]
            args = json.loads(call["function"]["arguments"])
            print(f"[tool] {name} {json.dumps(args, ensure_ascii=False)}")
            result = run_tool(name, args)
            messages.append({
                "role": "tool",
                "tool_call_id": call["id"],
                "content": result,
            })


def main():
    print(f"nanocode-kimi | {MODEL} | {os.getcwd()}")
    print("/q 退出，/c 清空对话")

    messages = []
    system_prompt = (
        "You are a concise coding agent. Work directly in the current working directory. "
        "Use the provided tools to inspect, modify, and test code. "
        "Verify important changes before finishing."
    )

    while True:
        try:
            user_input = input("\n> ").strip()
        except (KeyboardInterrupt, EOFError):
            print()
            return

        if not user_input:
            continue
        if user_input in ("/q", "exit"):
            return
        if user_input == "/c":
            messages.clear()
            print("conversation cleared")
            continue

        try:
            run_agent(user_input, messages, system_prompt)
        except Exception as e:
            print(f"error: {e}")


if __name__ == "__main__":
    main()