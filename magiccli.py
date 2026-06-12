#!/usr/bin/env python3
"""
MagicCLI — a Claude Code style coding agent for local models (Ollama).
Default: qwen3:4b-instruct. Zero dependencies. Reads, edits, runs, plans.
"""

import fnmatch
import itertools
import json
import os
import random
import re
import readline  # noqa: F401
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

# ---------------------------------------------------------------- config ----

OLLAMA_URL   = os.environ.get("MAGIC_OLLAMA_URL", "http://localhost:11434")
DEFAULT_MODEL = os.environ.get("MAGIC_MODEL", "qwen3:4b-instruct")
NUM_CTX      = int(os.environ.get("MAGIC_NUM_CTX", "32768"))
MAX_TOOL_RESULT_CHARS = 6000
MAX_AGENT_STEPS       = 20
HISTORY_CHAR_BUDGET   = 80000

# ----------------------------------------------------------------- colors ---

def _c(code):
    return lambda s: f"\033[{code}m{s}\033[0m" if sys.stdout.isatty() else s

bold, dim, red, green, yellow, blue, magenta, cyan = (
    _c("1"), _c("2"), _c("31"), _c("32"), _c("33"), _c("34"), _c("35"), _c("36"),
)

BANNER = "  ✦ MagicCLI"

# ------------------------------------------------------------------ tools ---

def _clip(text, limit=MAX_TOOL_RESULT_CHARS):
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [truncated — {len(text)} chars total]"

def _safe_path(path):
    return os.path.abspath(os.path.expanduser(path))


# --- filesystem ---

def tool_read_file(path, offset=0, limit=250):
    p = _safe_path(path)
    if not os.path.isfile(p):
        return f"ERROR: file not found: {p}"
    with open(p, "r", errors="replace") as f:
        lines = f.readlines()
    offset, limit = max(0, int(offset)), max(1, int(limit))
    chunk = lines[offset:offset + limit]
    numbered = "".join(f"{i+1+offset}: {l}" for i, l in enumerate(chunk))
    info = f"[{p} — {len(lines)} lines, showing {offset+1}–{offset+len(chunk)}]\n"
    return _clip(info + numbered)


def tool_write_file(path, content):
    p = _safe_path(path)
    os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
    with open(p, "w") as f:
        f.write(content)
    return f"OK: wrote {len(content)} chars to {p}"


def tool_edit_file(path, old_text, new_text):
    p = _safe_path(path)
    if not os.path.isfile(p):
        return f"ERROR: file not found: {p}"
    src = open(p, "r", errors="replace").read()
    count = src.count(old_text)
    if count == 0:
        return "ERROR: old_text not found. Read the file first and copy the EXACT text."
    if count > 1:
        return f"ERROR: old_text matches {count} places — include more surrounding lines to make it unique."
    with open(p, "w") as f:
        f.write(src.replace(old_text, new_text, 1))
    return f"OK: edited {p}"


def tool_list_dir(path="."):
    p = _safe_path(path)
    if not os.path.isdir(p):
        return f"ERROR: not a directory: {p}"
    entries = sorted(os.listdir(p))
    out = [e + "/" if os.path.isdir(os.path.join(p, e)) else e for e in entries[:120]]
    extra = f"\n... and {len(entries)-120} more" if len(entries) > 120 else ""
    return _clip(f"[{p}]\n" + "\n".join(out) + extra)


def tool_find_file(name, search_path="~"):
    """Find files by name anywhere on the filesystem."""
    p = _safe_path(search_path)
    try:
        r = subprocess.run(
            ["find", p, "-iname", f"*{name}*", "-not", "-path", "*/.git/*",
             "-not", "-path", "*/node_modules/*", "-not", "-path", "*/__pycache__/*"],
            capture_output=True, text=True, timeout=15)
    except subprocess.TimeoutExpired:
        return "ERROR: find timed out — try a more specific search_path"
    results = r.stdout.strip()
    if not results:
        return f"No files matching '{name}' found under {p}"
    return _clip(results)


def tool_glob(pattern, path="."):
    """Find files by glob pattern (e.g. '**/*.py', 'src/*.ts')."""
    p = _safe_path(path)
    matches = []
    _SKIP = {'.git', 'node_modules', '__pycache__', '.venv', 'venv', 'dist', 'build', '.next'}
    for root, dirs, files in os.walk(p):
        dirs[:] = [d for d in dirs if d not in _SKIP]
        for fname in files:
            fpath = os.path.join(root, fname)
            rel = os.path.relpath(fpath, p)
            if fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(fname, pattern):
                matches.append(rel)
        if len(matches) > 300:
            break
    if not matches:
        return f"No files matching pattern '{pattern}' under {p}"
    extra = f"\n... and {len(matches)-300} more" if len(matches) > 300 else ""
    return _clip("\n".join(sorted(matches[:300])) + extra)


def tool_search(pattern, path="."):
    p = _safe_path(path)
    cmd = ["grep", "-rn", "-I",
           "--exclude-dir=.git", "--exclude-dir=node_modules",
           "--exclude-dir=.venv", "--exclude-dir=__pycache__",
           pattern, p]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
    except subprocess.TimeoutExpired:
        return "ERROR: search timed out"
    if r.returncode == 1:
        return "No matches found."
    if r.returncode > 1:
        return f"ERROR: {r.stderr.strip()}"
    return _clip(r.stdout)


def tool_run_command(command):
    try:
        r = subprocess.run(command, shell=True, capture_output=True,
                           text=True, timeout=120, cwd=os.getcwd())
    except subprocess.TimeoutExpired:
        return "ERROR: command timed out after 120s"
    out = (r.stdout or "") + (("\nSTDERR: " + r.stderr) if r.stderr else "")
    out = out.strip() or "(no output)"
    if r.returncode != 0:
        out = f"[exit {r.returncode}]\n{out}"
    return _clip(out)


# --- git ---

def tool_git_status():
    r = subprocess.run(["git", "status", "--short", "--branch"],
                       capture_output=True, text=True, cwd=os.getcwd())
    if r.returncode != 0:
        return r.stderr.strip() or "Not a git repo"
    return r.stdout.strip() or "Nothing to commit, working tree clean"


def tool_git_diff(staged=False):
    args = ["git", "diff"]
    if staged:
        args.append("--staged")
    r = subprocess.run(args, capture_output=True, text=True, cwd=os.getcwd())
    return _clip(r.stdout.strip() or "No diff") if r.returncode == 0 else r.stderr.strip()


def tool_git_log(n=10):
    r = subprocess.run(
        ["git", "log", f"-{n}", "--oneline", "--decorate"],
        capture_output=True, text=True, cwd=os.getcwd())
    return r.stdout.strip() or "No commits yet" if r.returncode == 0 else r.stderr.strip()


def tool_git_commit(message, files=None):
    """Stage specific files (or all with '.') then commit."""
    targets = files if isinstance(files, list) and files else ["."]
    add_r = subprocess.run(["git", "add"] + targets,
                            capture_output=True, text=True, cwd=os.getcwd())
    if add_r.returncode != 0:
        return f"ERROR staging: {add_r.stderr.strip()}"
    r = subprocess.run(["git", "commit", "-m", message],
                       capture_output=True, text=True, cwd=os.getcwd())
    return r.stdout.strip() + r.stderr.strip()


# ---------------------------------------------------------------- registry --

TOOLS = {
    "read_file":   {"fn": tool_read_file,   "danger": False},
    "write_file":  {"fn": tool_write_file,  "danger": True},
    "edit_file":   {"fn": tool_edit_file,   "danger": True},
    "list_dir":    {"fn": tool_list_dir,    "danger": False},
    "find_file":   {"fn": tool_find_file,   "danger": False},
    "glob":        {"fn": tool_glob,        "danger": False},
    "search":      {"fn": tool_search,      "danger": False},
    "run_command": {"fn": tool_run_command, "danger": True},
    "git_status":  {"fn": tool_git_status,  "danger": False},
    "git_diff":    {"fn": tool_git_diff,    "danger": False},
    "git_log":     {"fn": tool_git_log,     "danger": False},
    "git_commit":  {"fn": tool_git_commit,  "danger": True},
}

TOOL_SCHEMAS = [
    {"type": "function", "function": {
        "name": "read_file",
        "description": "Read a file. Returns numbered lines.",
        "parameters": {"type": "object", "required": ["path"], "properties": {
            "path":   {"type": "string"},
            "offset": {"type": "integer", "description": "start line (0-based)"},
            "limit":  {"type": "integer", "description": "max lines to return"},
        }}}},
    {"type": "function", "function": {
        "name": "write_file",
        "description": "Create or overwrite a file.",
        "parameters": {"type": "object", "required": ["path", "content"], "properties": {
            "path":    {"type": "string"},
            "content": {"type": "string"},
        }}}},
    {"type": "function", "function": {
        "name": "edit_file",
        "description": "Replace an exact unique snippet in a file. Read the file first to get exact text.",
        "parameters": {"type": "object", "required": ["path", "old_text", "new_text"], "properties": {
            "path":     {"type": "string"},
            "old_text": {"type": "string", "description": "exact text to replace (must be unique in file)"},
            "new_text": {"type": "string"},
        }}}},
    {"type": "function", "function": {
        "name": "list_dir",
        "description": "List contents of a directory.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "directory path, default ."},
        }}}},
    {"type": "function", "function": {
        "name": "find_file",
        "description": "Find files by name anywhere on disk. Use when you don't know the exact path.",
        "parameters": {"type": "object", "required": ["name"], "properties": {
            "name":        {"type": "string", "description": "filename or partial name to search for"},
            "search_path": {"type": "string", "description": "where to search, default ~ (home)"},
        }}}},
    {"type": "function", "function": {
        "name": "glob",
        "description": "Find files by glob pattern (e.g. '**/*.py', 'src/*.ts', '*.md'). Faster than find_file when you know the extension or structure.",
        "parameters": {"type": "object", "required": ["pattern"], "properties": {
            "pattern": {"type": "string", "description": "glob pattern like '**/*.py' or 'src/**/*.ts'"},
            "path":    {"type": "string", "description": "root directory to search, default ."},
        }}}},
    {"type": "function", "function": {
        "name": "search",
        "description": "Search for text inside files (grep). Returns file:line matches.",
        "parameters": {"type": "object", "required": ["pattern"], "properties": {
            "pattern": {"type": "string"},
            "path":    {"type": "string", "description": "directory to search, default ."},
        }}}},
    {"type": "function", "function": {
        "name": "run_command",
        "description": "Run a shell command and return its output. Use for tests, builds, installs, etc.",
        "parameters": {"type": "object", "required": ["command"], "properties": {
            "command": {"type": "string"},
        }}}},
    {"type": "function", "function": {
        "name": "git_status",
        "description": "Show git working tree status and current branch.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "git_diff",
        "description": "Show git diff of uncommitted changes.",
        "parameters": {"type": "object", "properties": {
            "staged": {"type": "boolean", "description": "show staged diff, default false"},
        }}}},
    {"type": "function", "function": {
        "name": "git_log",
        "description": "Show recent git commit history.",
        "parameters": {"type": "object", "properties": {
            "n": {"type": "integer", "description": "number of commits, default 10"},
        }}}},
    {"type": "function", "function": {
        "name": "git_commit",
        "description": "Stage files and create a git commit.",
        "parameters": {"type": "object", "required": ["message"], "properties": {
            "message": {"type": "string"},
            "files":   {"type": "array", "items": {"type": "string"},
                        "description": "files to stage; omit to stage everything (git add .)"},
        }}}},
]

# ------------------------------------------------------------- permissions --

class Permissions:
    def __init__(self, yolo=False):
        self.always = set()
        self.yolo = yolo

    def ask(self, tool, args):
        if self.yolo or not TOOLS[tool]["danger"] or tool in self.always:
            return True
        print(f"\n{yellow('⚠  permission:')} {bold(tool)}")
        print(self._preview(tool, args))
        try:
            ans = input(f"{yellow('   allow? [y]es / [a]lways / [n]o: ')}").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print(); return False
        if ans == "a":
            self.always.add(tool); return True
        return ans in ("y", "yes", "")

    @staticmethod
    def _preview(tool, args):
        if tool == "run_command":
            return dim(f"   $ {args.get('command','')[:300]}")
        if tool == "write_file":
            lines = str(args.get("content", "")).splitlines()
            head = "\n".join(green(f"   + {l[:110]}") for l in lines[:8])
            more = dim(f"\n   … +{len(lines)-8} more lines") if len(lines) > 8 else ""
            return dim(f"   → {args.get('path')}\n") + head + more
        if tool == "edit_file":
            old = str(args.get("old_text", "")).splitlines()[:6]
            new = str(args.get("new_text", "")).splitlines()[:6]
            diff = "\n".join(red(f"   - {l[:110]}") for l in old)
            diff += "\n" + "\n".join(green(f"   + {l[:110]}") for l in new)
            return dim(f"   → {args.get('path')}\n") + diff
        if tool == "git_commit":
            return dim(f"   git commit -m {args.get('message','')!r}")
        return dim(f"   {json.dumps(args)[:300]}")

# ---------------------------------------------------------------- ollama ----

def ollama_chat(model, messages, stream_print=True, spinner=None, counter=None):
    """Stream a chat completion.

    Returns (content, tool_calls, stats, interrupted).
    interrupted=True when the user pressed Ctrl+C mid-stream.
    Streaming runs in a daemon thread so Ctrl+C in the main thread
    is always responsive.
    """
    body = json.dumps({
        "model": model,
        "messages": messages,
        "tools": TOOL_SCHEMAS,
        "stream": True,
        "options": {
            "num_ctx":        NUM_CTX,
            "temperature":    0.15,
            "top_p":          0.9,
            "repeat_penalty": 1.1,
            "num_predict":    -1,
        },
    }).encode()
    req = urllib.request.Request(
        f"{OLLAMA_URL}/api/chat", data=body,
        headers={"Content-Type": "application/json"})

    # --- shared mutable state between threads ---
    parts      = []        # content pieces
    tool_calls = []
    stats      = {}
    errors     = []
    state      = {"printed": False, "in_think": False, "think_buf": ""}
    stop_ev    = threading.Event()

    def _emit(text):
        if not text:
            return
        if not state["printed"]:
            if spinner:
                spinner.stop()
            sys.stdout.write("\r\033[K")
            state["printed"] = True
        sys.stdout.write(text)
        sys.stdout.flush()

    def _stream():
        try:
            with urllib.request.urlopen(req, timeout=600) as resp:
                for raw in resp:
                    if stop_ev.is_set():
                        break
                    if not raw.strip():
                        continue
                    try:
                        chunk = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    msg   = chunk.get("message", {})
                    piece = msg.get("content", "")

                    if piece:
                        parts.append(piece)
                        if counter:
                            counter.inc()
                        if stream_print:
                            if not state["in_think"] and "<think>" in piece:
                                state["in_think"] = True
                                before, _, rest = piece.partition("<think>")
                                _emit(before)
                                state["think_buf"] = rest
                            elif state["in_think"]:
                                state["think_buf"] += piece
                                if "</think>" in state["think_buf"]:
                                    state["in_think"] = False
                                    _, _, after = state["think_buf"].partition("</think>")
                                    state["think_buf"] = ""
                                    _emit(after)
                            else:
                                _emit(piece)

                    for tc in msg.get("tool_calls", []) or []:
                        tool_calls.append(tc)

                    if chunk.get("done"):
                        if chunk.get("eval_count") and chunk.get("eval_duration"):
                            stats.update({
                                "tokens": chunk["eval_count"],
                                "tps":    chunk["eval_count"] / (chunk["eval_duration"] / 1e9),
                            })
                        break
        except Exception as e:
            errors.append(e)

    t = threading.Thread(target=_stream, daemon=True)
    t.start()

    interrupted = False
    try:
        while t.is_alive():
            t.join(timeout=0.05)
    except KeyboardInterrupt:
        interrupted = True
        stop_ev.set()
        t.join(timeout=3)
        if state["printed"]:
            print()
        print(yellow("  ↩ interrupted — type your next message"))

    if not interrupted and state["printed"]:
        print()

    for e in errors:
        if isinstance(e, urllib.error.URLError):
            raise ConnectionError(
                f"Ollama not reachable at {OLLAMA_URL} — run: ollama serve") from e

    return "".join(parts), tool_calls, stats, interrupted


def check_model(model):
    try:
        req = urllib.request.Request(f"{OLLAMA_URL}/api/tags")
        with urllib.request.urlopen(req, timeout=5) as resp:
            tags = json.loads(resp.read())
        names = [m["name"] for m in tags.get("models", [])]
        return model in names or any(n.split(":")[0] == model.split(":")[0] for n in names)
    except Exception:
        return False


def list_ollama_models():
    """Return list of (name, size_gb) tuples from local Ollama."""
    try:
        req = urllib.request.Request(f"{OLLAMA_URL}/api/tags")
        with urllib.request.urlopen(req, timeout=5) as resp:
            tags = json.loads(resp.read())
        out = []
        for m in tags.get("models", []):
            gb = m.get("size", 0) / 1e9
            out.append((m["name"], gb))
        return sorted(out, key=lambda x: x[1])
    except Exception:
        return []

# ------------------------------------------------- fallback tool parse ------

TOOL_CALL_RE = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.DOTALL)
JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(\{[^`]*?\"name\"[^`]*?\})\s*```", re.DOTALL)


def parse_fallback_tool_calls(content):
    calls = []
    for m in TOOL_CALL_RE.findall(content) + JSON_BLOCK_RE.findall(content):
        try:
            obj = json.loads(m)
        except json.JSONDecodeError:
            continue
        name = obj.get("name")
        args = obj.get("arguments", obj.get("parameters", {}))
        if name in TOOLS and isinstance(args, dict):
            calls.append({"function": {"name": name, "arguments": args}})
    return calls

# ------------------------------------------------------------- agent loop ---

def build_system_prompt():
    cwd  = os.getcwd()
    home = os.path.expanduser("~")
    try:
        entries = sorted(os.listdir(cwd))[:40]
        listing = ", ".join(
            e + "/" if os.path.isdir(os.path.join(cwd, e)) else e
            for e in entries)
    except OSError:
        listing = "(unreadable)"

    git_info = ""
    gr = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"],
                        capture_output=True, text=True, cwd=cwd)
    if gr.returncode == 0:
        branch = gr.stdout.strip()
        sr = subprocess.run(["git", "status", "--short"],
                            capture_output=True, text=True, cwd=cwd)
        changed = len(sr.stdout.strip().splitlines())
        git_info = f"\nGit: branch={branch}, {changed} changed file(s)"

    # project memory — like Claude Code's CLAUDE.md
    memory = ""
    magic_md = os.path.join(cwd, "MAGIC.md")
    if os.path.isfile(magic_md):
        try:
            memory = ("\n\n## Project notes (MAGIC.md — follow these)\n"
                      + open(magic_md, errors="replace").read()[:2500])
        except OSError:
            pass

    return (
        "You are MagicCLI, a powerful coding agent in the user's terminal on macOS.\n"
        "You have FULL access to the ENTIRE computer through your tools — every file, "
        "every folder, any path. NEVER say you lack access — use a tool.\n"
        f"Home: {home}  ·  Desktop: {home}/Desktop  ·  cwd: {cwd}{git_info}\n"
        f"Files in cwd: {listing}\n\n"
        "## Tools available\n"
        "- read_file / write_file / edit_file — read and modify files\n"
        "- list_dir — see what's in a folder\n"
        "- glob — find files by pattern (e.g. '**/*.py', 'src/*.ts') — USE THIS first for pattern searches\n"
        "- find_file — locate a file by name anywhere on disk (USE THIS when path unknown)\n"
        "- search — grep text inside files\n"
        "- run_command — run any shell command (tests, builds, git, pip, etc.)\n"
        "- git_status / git_diff / git_log / git_commit — git operations\n\n"
        "## Rules (follow strictly)\n"
        "1. NEVER guess a file path. If unknown → use glob or find_file first, then act.\n"
        "2. NEVER paste code in chat. If user wants code in a file → write_file/edit_file it.\n"
        "3. NEVER say 'I don't have access' — you do. Use tools.\n"
        "4. NEVER ask 'Would you like me to...?' — just do it.\n"
        "5. Before editing a file → read_file first to see exact content.\n"
        "6. After making changes → verify by reading back or running the code.\n"
        "7. For complex tasks: briefly state your plan in 1-2 lines, then execute it.\n"
        "8. One tool call per message. Wait for result before next call.\n"
        "9. When done → short plain-text summary, no more tool calls.\n"
        "10. User writes in Hinglish (Hindi+English mix) — understand and reply simply.\n"
        + memory
    )


# ---- nudges: catch common model mistakes and self-correct ------------------

NO_ACCESS_RE  = re.compile(
    r"(don'?t|do not|cannot|can'?t|unable to|no)\s+(have\s+)?"
    r"(access|permission|see your|view your|read your)|outside the current working",
    re.IGNORECASE)
CODE_BLOCK_RE = re.compile(r"```\w*\n(?:.*\n){3,}?\s*```")
FILE_INTENT_RE = re.compile(
    r"\b\w+\.\w{1,5}\b|file|save|likho|likhd?o|bana|banao|create|add|update|"
    r"fix|isme|esme|usme|daal|write|daaldo|daalna", re.IGNORECASE)
ASK_USER_RE   = re.compile(
    r"(would you like|do you want|should i|want me to|let me know how|"
    r"how would you like|shall i|please (tell|let)|kya aap chahte|batao kya karna)",
    re.IGNORECASE)
APOLOGIZE_RE  = re.compile(
    r"(i apologize|i'm sorry|i cannot|i can't help|as an ai|as a language model)",
    re.IGNORECASE)


def pick_nudge(answer, last_user_msg, used):
    if "no_access" not in used and NO_ACCESS_RE.search(answer):
        used.add("no_access")
        return ("WRONG. You have full access to the entire computer via tools. "
                "Use find_file or list_dir with the right path RIGHT NOW. Do not refuse.")
    if ("code_dump" not in used and CODE_BLOCK_RE.search(answer)
            and FILE_INTENT_RE.search(last_user_msg)):
        used.add("code_dump")
        return ("Do NOT paste code in chat. Use write_file to save it to the correct "
                "file RIGHT NOW.")
    if "ask_user" not in used and ASK_USER_RE.search(answer):
        used.add("ask_user")
        return ("Stop asking — just DO IT. Use your tools to complete the task now.")
    if "apologize" not in used and APOLOGIZE_RE.search(answer):
        used.add("apologize")
        return ("Stop apologizing and just use your tools to complete the task. Act now.")
    return None


def trim_history(messages):
    """Remove oldest full conversation turns (user→assistant→tools) to stay under budget."""
    total = sum(len(str(m.get("content", ""))) for m in messages)
    while total > HISTORY_CHAR_BUDGET and len(messages) > 3:
        # Find first non-system message
        i = 1
        while i < len(messages) and messages[i].get("role") == "system":
            i += 1
        if i >= len(messages):
            break
        # Remove that message and any immediately following assistant/tool messages
        # so we always drop a complete turn
        removed = len(str(messages[i].get("content", "")))
        messages.pop(i)
        total -= removed
        while i < len(messages) and messages[i].get("role") in ("assistant", "tool"):
            removed = len(str(messages[i].get("content", "")))
            messages.pop(i)
            total -= removed
    return messages


# ---- whimsical spinner (the Claude Code "Humming…" thing) ------------------

SPINNER_VERBS = [
    "Humming", "Pondering", "Brewing", "Conjuring", "Noodling", "Tinkering",
    "Scheming", "Vibing", "Crunching", "Summoning", "Whirring", "Cooking",
    "Manifesting", "Percolating", "Mulling", "Marinating", "Wizarding",
    "Jugaad lagana", "Dimaag chalana", "Sochna", "Khichdi pakana",
]
SPINNER_FRAMES = ["✢", "✳", "✶", "✻", "✽", "✻", "✶", "✳"]


class LiveCounter:
    """Shared token counter between ollama_chat (writer) and Spinner (reader)."""
    __slots__ = ("tokens", "t0")

    def __init__(self):
        self.tokens = 0
        self.t0 = time.time()

    def inc(self):
        self.tokens += 1

    @property
    def tps(self):
        elapsed = time.time() - self.t0
        return self.tokens / elapsed if elapsed > 0.5 else 0.0


class Spinner:
    """Animated status line: verb + elapsed time + live token count."""

    def __init__(self):
        self._stop   = threading.Event()
        self._thread = None
        self.counter = None          # set by caller before start()

    def start(self, counter=None):
        if not sys.stdout.isatty():
            return
        self.counter = counter
        self._stop.clear()
        verb = random.choice(SPINNER_VERBS)
        t0 = time.time()
        ctr = self.counter

        def _run():
            for frame in itertools.cycle(SPINNER_FRAMES):
                if self._stop.is_set():
                    return
                secs = int(time.time() - t0)
                tok_str = ""
                if ctr and ctr.tokens > 0:
                    tps = ctr.tps
                    tok_str = (f" · {blue(str(ctr.tokens))} tok"
                               + (f" · {tps:.1f}/s" if tps > 0.5 else ""))
                sys.stdout.write(
                    f"\r\033[K{magenta(frame)} "
                    f"{dim(f'{verb}… ({secs}s{tok_str} · ^C interrupt)')}")
                sys.stdout.flush()
                self._stop.wait(0.1)

        self._thread = threading.Thread(target=_run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=0.5)
            self._thread = None
        sys.stdout.write("\r\033[K")
        sys.stdout.flush()


def run_agent(messages, model, perms):
    # Refresh system prompt so git/file state is always current
    messages[0] = {"role": "system", "content": build_system_prompt()}

    last_user_msg = next(
        (m["content"] for m in reversed(messages) if m.get("role") == "user"), "")
    nudges_used = set()
    steps_taken = 0

    spinner = Spinner()
    for step in range(MAX_AGENT_STEPS):
        trim_history(messages)
        counter = LiveCounter()
        spinner.start(counter=counter)
        try:
            content, tool_calls, stats, interrupted = ollama_chat(
                model, messages, spinner=spinner, counter=counter)
        except ConnectionError as e:
            spinner.stop()
            print(red(f"✗ {e}")); return steps_taken
        finally:
            spinner.stop()

        if interrupted:
            return steps_taken  # drop back to REPL so user can type next message

        clean = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()

        if not tool_calls:
            tool_calls = parse_fallback_tool_calls(clean)
            if tool_calls:
                clean = TOOL_CALL_RE.sub("", JSON_BLOCK_RE.sub("", clean)).strip()

        # Build assistant message — preserve tool_calls with their ids intact
        asst_msg = {"role": "assistant", "content": clean}
        if tool_calls:
            asst_msg["tool_calls"] = tool_calls
        messages.append(asst_msg)

        if not tool_calls:
            nudge = pick_nudge(clean, last_user_msg, nudges_used)
            if nudge:
                print(dim("  ✦ correcting..."))
                messages.append({"role": "user", "content": nudge})
                continue
            if not clean:
                print(dim("(no response)"))
            tail = []
            if steps_taken > 0:
                tail.append(f"{steps_taken} step{'s' if steps_taken != 1 else ''}")
            if stats:
                tail.append(f"{stats['tokens']} tok · {stats['tps']:.1f} tok/s")
            if tail:
                print(dim(f"  ✦ {' · '.join(tail)}"))
            return steps_taken

        for i, tc in enumerate(tool_calls):
            fn   = tc.get("function", {})
            name = fn.get("name", "")
            args = fn.get("arguments", {})
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {}

            # Extract tool_call_id for proper protocol compliance
            tc_id = tc.get("id") or f"call_{step}_{i}"

            if name not in TOOLS:
                result = f"ERROR: unknown tool '{name}'. Available: {', '.join(TOOLS)}"
            else:
                arg_str = ", ".join(f"{k}={str(v)[:50]!r}" for k, v in args.items())
                print(f"{green('⏺')} {bold(name)}({dim(arg_str[:140])})")
                if perms.ask(name, args):
                    t_start = time.time()
                    try:
                        result = TOOLS[name]["fn"](**args)
                    except TypeError as e:
                        result = f"ERROR: bad arguments — {e}"
                    except Exception as e:
                        result = f"ERROR: {e}"
                    elapsed = time.time() - t_start
                    steps_taken += 1
                    # rich result preview
                    lines = result.splitlines()
                    first = lines[0] if lines else ""
                    extra = f" +{len(lines)-1} lines" if len(lines) > 1 else ""
                    time_str = f" ({elapsed:.1f}s)" if elapsed > 0.5 else ""
                    ok_col = red if first.startswith("ERROR") else dim
                    print(ok_col(f"  ⎿ {first[:130]}{extra}{time_str}"))
                    # for edits/writes: show mini diff inline
                    if name == "edit_file" and not result.startswith("ERROR"):
                        old_lines = str(args.get("old_text","")).splitlines()[:4]
                        new_lines = str(args.get("new_text","")).splitlines()[:4]
                        for l in old_lines:
                            print(red(f"    - {l[:100]}"))
                        for l in new_lines:
                            print(green(f"    + {l[:100]}"))
                else:
                    result = "User denied. Ask them what to do instead."
                    print(dim(f"  ⎿ denied"))

            messages.append({
                "role": "tool",
                "content": result,
                "tool_call_id": tc_id,
            })

        if step == MAX_AGENT_STEPS - 2:
            messages.append({"role": "user",
                             "content": "Wrap up: plain-text summary only, no more tools."})

    print(yellow("⚠ max steps reached"))
    return steps_taken

# ------------------------------------------------------------------- REPL ---

def _help(model):
    return f"""
{bold('MagicCLI — commands')}
  /help              this help
  /clear             clear conversation history
  /model [name]      show or switch model  (current: {model})
  /cwd [dir]         show or change working directory
  /add <file>        inject a file's contents into the conversation
  /init              explore the project and generate MAGIC.md (project memory)
  /memory            show current MAGIC.md
  /models            list local Ollama models and pick one
  /git               quick git status + last 5 commits
  /yolo              toggle auto-approve all tools
  /retry             re-run last message with a fresh response
  /exit              quit  (Ctrl+D also works)

{bold('Usage examples')}
  fix the bug in main.py
  desktop wali index.html me badiya HTML daaldo
  run the tests and show me what failed
  git mein commit karo — "fix login bug"
  create a snake game in pygame
  /add src/auth.py    ← file ko context mein daalo
"""


def main():
    argv = sys.argv[1:]
    model = DEFAULT_MODEL
    yolo  = False
    one_shot_parts = []
    i = 0
    while i < len(argv):
        if argv[i] in ("-m", "--model") and i + 1 < len(argv):
            model = argv[i+1]; i += 2
        elif argv[i] in ("-y", "--yolo"):
            yolo = True; i += 1
        elif argv[i] in ("-h", "--help"):
            print(_help(model)); return
        else:
            one_shot_parts.append(argv[i]); i += 1

    perms    = Permissions(yolo=yolo)
    messages = [{"role": "system", "content": build_system_prompt()}]

    if not check_model(model):
        print(yellow(f"⚠  model '{model}' not found — run: ollama pull {model}"))

    if one_shot_parts:
        messages.append({"role": "user", "content": " ".join(one_shot_parts)})
        run_agent(messages, model, perms)
        return

    has_magic_md = os.path.isfile(os.path.join(os.getcwd(), "MAGIC.md"))
    print(magenta(BANNER))
    print(dim(f"  model: {model}  ·  cwd: {os.getcwd()}"
              + ("  ·  MAGIC.md ✓" if has_magic_md else "")))
    print(dim("  /help · Ctrl+D to exit\n"))

    session_t0 = time.time()
    session_turns = 0
    session_tools = 0

    def _goodbye():
        mins = (time.time() - session_t0) / 60
        if session_turns:
            print(dim(f"  ✦ session: {mins:.0f} min · {session_turns} turn(s) · "
                      f"{session_tools} tool call(s)"))
        print(dim(random.choice([
            "bye! ✦", "happy shipping ✦", "phir milenge ✦",
            "code chamka ke jao ✦", "ship it! ✦",
        ])))

    while True:
        try:
            user = input(cyan("❯ ")).strip()
        except (EOFError, KeyboardInterrupt):
            print(); _goodbye(); break

        if not user:
            continue
        if user in ("exit", "quit", "/exit", "/quit"):
            _goodbye(); break
        if user in ("/help", "/?"):
            print(_help(model)); continue
        if user == "/clear":
            messages = [{"role": "system", "content": build_system_prompt()}]
            print(dim("conversation cleared")); continue
        if user == "/models":
            mlist = list_ollama_models()
            if not mlist:
                print(red("✗ Ollama not running or no models — run: ollama serve"))
            else:
                print(dim(f"\n  {'#':<3} {'model':<40} {'size':>6}"))
                print(dim("  " + "─" * 52))
                for idx, (name_m, gb) in enumerate(mlist, 1):
                    marker = green("  ◀ active") if name_m == model else ""
                    print(f"  {dim(str(idx)+':')<5} {name_m:<40} {dim(f'{gb:.1f}G')}{marker}")
                print(dim("\n  /model <name or number> to switch\n"))
            continue
        if user.startswith("/model"):
            parts = user.split(None, 1)
            if len(parts) == 2:
                choice = parts[1].strip()
                # allow picking by number from /models list
                if choice.isdigit():
                    mlist = list_ollama_models()
                    idx = int(choice) - 1
                    if 0 <= idx < len(mlist):
                        choice = mlist[idx][0]
                    else:
                        print(red(f"✗ no model #{choice} — run /models to see the list"))
                        continue
                model = choice
                print(dim(f"model → {model}"))
            else:
                print(dim(f"current model: {model}"))
            continue
        if user.startswith("/cwd"):
            parts = user.split(None, 1)
            if len(parts) == 2:
                try:
                    os.chdir(os.path.expanduser(parts[1].strip()))
                    messages[0] = {"role": "system", "content": build_system_prompt()}
                    print(dim(f"cwd → {os.getcwd()}"))
                except OSError as e:
                    print(red(f"✗ {e}"))
            else:
                print(dim(f"cwd: {os.getcwd()}"))
            continue
        if user.startswith("/add"):
            parts = user.split(None, 1)
            if len(parts) < 2:
                print(dim("usage: /add <file_path>")); continue
            fpath = parts[1].strip()
            result = tool_read_file(fpath)
            if result.startswith("ERROR"):
                print(red(f"✗ {result}")); continue
            messages.append({"role": "user",
                             "content": f"[Context added — {fpath}]\n{result}"})
            messages.append({"role": "assistant",
                             "content": f"Got it, I've read `{fpath}` and have it in context."})
            print(dim(f"✓ added: {fpath}")); continue
        if user == "/init":
            print(dim("exploring project to generate MAGIC.md..."))
            messages.append({"role": "user", "content":
                "Explore this project: list_dir the working directory, read the "
                "most important files (README, main entry points, configs). Then "
                "use write_file to create MAGIC.md in the working directory with: "
                "1-line project description, tech stack, key files and what they do, "
                "how to run/test, and any conventions you noticed. Keep it under "
                "40 lines. Markdown format."})
            try:
                session_tools += run_agent(messages, model, perms) or 0
                session_turns += 1
            except KeyboardInterrupt:
                sys.stdout.write("\r\033[K")
                print(yellow("⚠ interrupted"))
            print(); continue
        if user == "/memory":
            mpath = os.path.join(os.getcwd(), "MAGIC.md")
            if os.path.isfile(mpath):
                print(dim(f"— {mpath} —"))
                print(open(mpath, errors="replace").read())
            else:
                print(dim("no MAGIC.md here — run /init to generate one"))
            continue
        if user == "/git":
            print(dim(tool_git_status()))
            print(dim(tool_git_log(5)))
            print(); continue
        if user == "/yolo":
            perms.yolo = not perms.yolo
            state = "ON — all tools auto-approved ⚡" if perms.yolo else "off"
            print(dim(f"yolo: {state}")); continue
        if user == "/retry":
            # Remove last assistant turn and re-run from last user message
            while messages and messages[-1].get("role") != "user":
                messages.pop()
            if len(messages) <= 1:
                print(dim("nothing to retry")); continue
            print(dim("retrying..."))
            try:
                run_agent(messages, model, perms)
            except KeyboardInterrupt:
                sys.stdout.write("\r\033[K")
                print(yellow("⚠ interrupted"))
            print(); continue
        if user.startswith("/"):
            print(dim("unknown command — /help")); continue

        messages.append({"role": "user", "content": user})
        try:
            session_tools += run_agent(messages, model, perms) or 0
            session_turns += 1
        except KeyboardInterrupt:
            sys.stdout.write("\r\033[K")
            print(yellow("⚠ interrupted"))
        print()


if __name__ == "__main__":
    main()
