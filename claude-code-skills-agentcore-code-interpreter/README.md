# agentcore-sandbox

A CLI for executing code and shell commands in AWS Bedrock AgentCore Code Interpreter sessions. Supports Python, JavaScript, and TypeScript.

## Prerequisites

- Go 1.24+
- AWS credentials configured (via environment, shared config, or IAM role)
- A Bedrock AgentCore Code Interpreter identifier

## Building

```bash
# Build for current platform
make build

# Cross-compile for all supported platforms (macOS, Linux, Windows on arm64/amd64)
make build-all

# Run unit tests
make test

# Clean build artifacts
make clean
```

Build outputs:

| Target | Output |
|--------|--------|
| Current platform | `dist/agentcore-sandbox` |
| macOS ARM64 | `dist/agentcore-sandbox-darwin-arm64` |
| macOS Intel | `dist/agentcore-sandbox-darwin-amd64` |
| Linux ARM64 | `dist/agentcore-sandbox-linux-arm64` |
| Linux Intel | `dist/agentcore-sandbox-linux-amd64` |
| Windows ARM64 | `dist/agentcore-sandbox-windows-arm64.exe` |
| Windows Intel | `dist/agentcore-sandbox-windows-amd64.exe` |

## Configuration

Set the following environment variables:

| Variable | Required | Description |
|----------|----------|-------------|
| `CUSTOM_AGENTCORE_INTERPRETER_ID` | Yes | Code interpreter identifier |
| `CUSTOM_AGENTCORE_AWS_PROFILE` | No | AWS shared config profile |
| `CUSTOM_AGENTCORE_AWS_REGION` | No | AWS region override |

## Usage

```
agentcore-sandbox <command> [flags] [args...]
```

### Commands

| Command | Description |
|---------|-------------|
| `start` | Start a new session, prints session ID |
| `stop <session-id>` | Stop an existing session |
| `exec <session-id> [flags] <code>` | Execute code or a command in an existing session |
| `run [flags] <code>` | One-shot: start a session, execute, then stop |
| `help` | Show help message |

### Flags

| Flag | Values | Default | Applies to |
|------|--------|---------|------------|
| `--lang` | `python`, `javascript`/`js`, `typescript`/`ts` | `python` | `exec`, `run` |
| `--cmd` | | | `exec`, `run` |
| `--runtime` | `nodejs`, `deno`, `python` | auto | `exec`, `run` |
| `--clear-context` | | | `exec`, `run` |
| `--json` | | | `start`, `exec`, `run` |
| `--timeout` | seconds | `900` | `start`, `run` |

- `--lang` and `--cmd` are mutually exclusive. Use `--cmd` for shell commands, `--lang` for code execution.
- When `--runtime` is omitted, it defaults to `python` for Python and `deno` for JavaScript/TypeScript.

### Examples

**One-shot execution (auto-manages session lifecycle):**

```bash
# Python (default language)
agentcore-sandbox run 'print("hello")'

# JavaScript
agentcore-sandbox run --lang js 'console.log("hello")'

# TypeScript
agentcore-sandbox run --lang ts 'const x: number = 42; console.log(x)'

# JavaScript with Node.js runtime instead of Deno
agentcore-sandbox run --lang js --runtime nodejs 'console.log(process.version)'

# Shell command
agentcore-sandbox run --cmd 'ls -la'

# JSON output for programmatic consumption
agentcore-sandbox run --json --lang python 'print(42)'
```

**Session-based workflow (session persists between calls):**

```bash
# Start a session with a 30-minute timeout
SESSION=$(agentcore-sandbox start --timeout 1800)

# Execute multiple commands in the same session (context is preserved)
agentcore-sandbox exec $SESSION --lang ts 'const x = 1'
agentcore-sandbox exec $SESSION --lang ts 'console.log(x)'

# Clear context and start fresh within the same session
agentcore-sandbox exec $SESSION --clear-context --lang ts 'console.log("fresh start")'

# Run a shell command in the session
agentcore-sandbox exec $SESSION --cmd 'pwd'

# Stop the session when done
agentcore-sandbox stop $SESSION
```

### JSON output

When `--json` is passed, output is a single JSON object on stdout:

```json
{
  "stdout": "hello\n",
  "stderr": "",
  "exitCode": 0,
  "executionTime": 123.4,
  "isError": false
}
```

For `start --json`:

```json
{"sessionId": "abc-123"}
```

### Tips

- When running AWS CLI commands inside the sandbox, set `AWS_PAGER=''` to avoid pager errors since `less` is not available in the sandbox environment:
  ```bash
  agentcore-sandbox run --cmd "AWS_PAGER='' aws s3 ls"
  ```
