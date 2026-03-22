# wechat-agent-channel

[中文](/e:/play/wechat-agent-channel/README.zh-CN.md)

Use WeChat as a front end for local coding agents. You can route messages to `Codex`, `OpenCode`, or `Claude Code` and talk to them directly from WeChat.

## What it is for

- Ask coding questions from WeChat
- Continue working with your local agent when you are away from your IDE
- Switch between multiple coding providers behind the same WeChat entry point

## Requirements

- Python 3.11+
- Node.js 18+
- A WeChat client connected to ClawBot
- The CLI for the provider you want to use must be installed and available in `PATH`

Examples:

```bash
npm install -g @openai/codex
# Install opencode from the official docs
npm install -g @anthropic-ai/claude-code
```

Install project dependencies:

```bash
npm install
```

## First-time setup

Make sure the CLIs can start:

```bash
codex
opencode
claude
```

Then run:

```bash
npm run setup
```

Setup does two things:

- shows a WeChat login QR code and saves credentials
- asks you to choose the default provider

Options:

- `1` -> `Codex`
- `2` -> `OpenCode`
- `3` -> `Claude Code`

## How to start

If your default provider is `Codex` or `OpenCode`:

```bash
npm start
```

If your default provider is `Claude Code`:

```bash
claude --dangerously-load-development-channels server:wechat
```

## How to use it

After startup, just send a message to ClawBot in WeChat.

Examples:

- `Write a Python script for me`
- `Explain how this project starts`
- `What does this error mean?`

If you already selected a default provider during setup, you do not need to add any provider prefix in the message.

## Optional environment variables

- `BOT_TOKEN`
- `WECHAT_BASE_URL`
- `WECHAT_AGENT_PROVIDER`

## Notes

- `Codex` and `OpenCode` run through their local CLIs
- `Claude Code` uses the official Channels flow
- WeChat credentials are stored in your user directory by default
- It is best to keep only one running instance at a time

## License

MIT
