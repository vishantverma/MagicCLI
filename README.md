# ✦ MagicCLI

<p align="center">
  <img src="https://img.shields.io/badge/Local--First-Ollama-blueviolet?style=for-the-badge" alt="Local First">
  <img src="https://img.shields.io/badge/Zero--Dependencies-Pure_Python-green?style=for-the-badge" alt="Zero Dependencies">
  <img src="https://img.shields.io/badge/License-MIT-blue?style=for-the-badge" alt="License">
</p>

**MagicCLI** is a Claude Code-style local coding agent built in pure Python with **zero external dependencies**. It runs entirely in your terminal, executing commands, searching codebase, and safely editing files using local LLMs (via Ollama). 

It is specially optimized to make **small local models** (like `qwen2.5-coder:7b`, `gemma2:9b`, or `qwen3:4b-instruct`) behave like heavy-weight agentic coding assistants.

---

## ✨ Why MagicCLI? (Key Features)

* **Zero Dependencies:** No LangChain, no CrewAI, no heavy libraries. Just single python file executing via standard modules.
* **Privacy First:** Your code never leaves your local machine. 100% free, offline, and secure.
* **Local Small-Model Optimizations:**
  * **Auto-Nudge & Correction Engine:** Detects when small models hallucinate access issues, apologize unnecessarily, or dump code blocks in chat instead of writing to file, and automatically corrects them.
  * **Fallback JSON Parser:** Captures messy tool-calling blocks written by smaller models inside raw text or custom `<tool_call>` wrappers.
  * **Token & Context Budgeting:** Automatically trims older conversation turns and truncates heavy tool outputs (>6000 chars) to stay safely within context limits.
* **Hinglish-First Design:** Fully optimized to understand natural Hinglish instructions (e.g. *"main.py me login function thik krdo"*).
* **MAGIC.md Project Memory:** Supports `/init` to automatically generate project notes (key files, conventions, and tech stack) and loads them into the agent's system prompt in future sessions.
* **Modern Terminal UX:** Whimsical loading spinner with random Hinglish verbs, colored diff previews (red/green) for file changes, and post-turn token statistics.

---

## 🚀 Quick Start

### Prerequisites
1. Install [Ollama](https://ollama.com).
2. Pull your favorite coding model:
   ```bash
   ollama pull qwen2.5-coder:7b
   ```

### Installation
1. Clone this repository:
   ```bash
   git clone https://github.com/vishantverma/MagicCLI.git
   cd MagicCLI
   ```
2. Symlink the launcher to your PATH (macOS/Linux):
   ```bash
   chmod +x magic magiccli.py
   sudo ln -s "$(pwd)/magic" /usr/local/bin/magic
   ```

---

## 💡 Usage

```bash
# Start interactive REPL
magic

# Run in YOLO mode (runs write/run actions without asking for permission)
magic -y

# Start with a specific model
magic -m qwen2.5-coder:14b

# Run a one-shot query
magic "find all python files and run pytest"
```

### REPL Commands

| Command | Action |
|---|---|
| `/help` | Show available commands |
| `/clear` | Reset chat history |
| `/model <name>` | Switch active Ollama model |
| `/models` | Interactive menu to select pulled Ollama models |
| `/cwd <dir>` | Change active working directory inside agent |
| `/add <file>` | Inject a file's content directly into LLM context |
| `/init` | Analyze project and write `MAGIC.md` memory |
| `/memory` | Print the current `MAGIC.md` |
| `/git` | Check quick status and last 5 commits |
| `/yolo` | Toggle auto-approval of dangerous tools |
| `/retry` | Remove last assistant answer and regenerate |
| `/exit` | Exit the REPL |

---

## 🛠️ Architecture

MagicCLI runs a classic agent loop:
1. **Initialize:** Loads `MAGIC.md` and reads `cwd` contents directly into system prompt.
2. **Listen:** Streams response from Ollama while tracking token counts and animating the spinner.
3. **Correct:** If no tool was called but user asked for code edits, the Nudge Engine triggers.
4. **Authorize:** Prompts user with a colored diff (`+` / `-`) for approval on file edits or shell runs.
5. **Execute:** Runs tools locally and returns output back to the model context.

---

## 🤝 Contributing

Contributions are welcome! If you want to add new tools, optimize the regex parser, or add new spinner phrases, feel free to open a Pull Request.

## 📄 License
This project is licensed under the **MIT License**. See [LICENSE](LICENSE) for details.

---

## ⚖️ Disclaimer
*MagicCLI is an independent open-source project and is not affiliated with, sponsored by, or endorsed by Anthropic or the creators of Claude Code.*

