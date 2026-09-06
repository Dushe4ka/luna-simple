# Changelog

All notable changes to Luna are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] - 2026-09-06

### Added

- Lightweight CLI coding agent built on `deepagents` (`luna` / `luna-simple`).
- Streaming `rich` REPL and one-shot mode (`luna "…"`).
- ANSI splash screen (`--no-splash` to skip).
- Five model providers via LangChain `init_chat_model`: `anthropic` (default),
  `deepseek`, `openai`, `google`, `ollama`.
- Layered configuration: CLI flags > environment > `./.luna.toml` >
  `~/.config/luna/config.toml` > defaults.
- Human-in-the-loop approval for `write_file`, `edit_file`, `delete`, and
  `execute`; `--yolo` disables all prompts.
- Real-filesystem backend rooted at the working directory.
- `luna setup` wizard and `luna config` subcommands (`path`, `show`, `set`,
  `set-key`, `unset-key`). API keys stored in `~/.config/luna/credentials.toml`
  (mode `0600`); environment variables still take precedence. First run without
  a key offers to launch the wizard. `--no-input` opts out of all prompting.
