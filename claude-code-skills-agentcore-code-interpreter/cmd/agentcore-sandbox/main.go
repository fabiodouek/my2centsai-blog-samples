package main

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"strings"

	"github.com/aws/aws-sdk-go-v2/config"
	"github.com/aws/aws-sdk-go-v2/service/bedrockagentcore"
	"github.com/aws/aws-sdk-go-v2/service/bedrockagentcore/types"
	flag "github.com/spf13/pflag"
)

func main() {
	if len(os.Args) < 2 {
		printHelp()
		os.Exit(1)
	}

	// Handle help before loading AWS config so it works without credentials
	switch os.Args[1] {
	case "help", "--help", "-h":
		printHelp()
		return
	}

	ctx := context.Background()
	var opts []func(*config.LoadOptions) error
	if profile := os.Getenv("CUSTOM_AGENTCORE_AWS_PROFILE"); profile != "" {
		opts = append(opts, config.WithSharedConfigProfile(profile))
	}
	if region := os.Getenv("CUSTOM_AGENTCORE_AWS_REGION"); region != "" {
		opts = append(opts, config.WithRegion(region))
	}
	cfg, err := config.LoadDefaultConfig(ctx, opts...)
	if err != nil {
		fmt.Fprintf(os.Stderr, "error loading AWS config: %v\n", err)
		os.Exit(1)
	}

	client := bedrockagentcore.NewFromConfig(cfg)
	interpreterID := os.Getenv("CUSTOM_AGENTCORE_INTERPRETER_ID")
	if interpreterID == "" {
		fmt.Fprintf(os.Stderr, "error: CUSTOM_AGENTCORE_INTERPRETER_ID environment variable is required\n")
		os.Exit(1)
	}

	switch os.Args[1] {
	case "start":
		cmdStart(ctx, client, interpreterID, os.Args[2:])
	case "stop":
		cmdStop(ctx, client, interpreterID, os.Args[2:])
	case "exec":
		cmdExec(ctx, client, interpreterID, os.Args[2:])
	case "run":
		cmdRun(ctx, client, interpreterID, os.Args[2:])
	default:
		fmt.Fprintf(os.Stderr, "unknown command: %s\n", os.Args[1])
		os.Exit(1)
	}
}

// cmdStart handles the "start" command.
func cmdStart(ctx context.Context, client *bedrockagentcore.Client, interpreterID string, args []string) {
	fs := flag.NewFlagSet("start", flag.ContinueOnError)
	timeout := fs.Int32("timeout", 900, "session timeout in seconds")
	jsonOutput := fs.Bool("json", false, "output JSON")
	if err := fs.Parse(args); err != nil {
		os.Exit(1)
	}

	sessionID, err := startSession(ctx, client, interpreterID, *timeout)
	if err != nil {
		fmt.Fprintf(os.Stderr, "error starting session: %v\n", err)
		os.Exit(1)
	}
	if *jsonOutput {
		out, err := json.Marshal(map[string]string{"sessionId": sessionID})
		if err != nil {
			fmt.Fprintf(os.Stderr, "error marshaling JSON: %v\n", err)
			os.Exit(1)
		}
		fmt.Println(string(out))
	} else {
		fmt.Println(sessionID)
	}
}

// cmdStop handles the "stop" command.
func cmdStop(ctx context.Context, client *bedrockagentcore.Client, interpreterID string, args []string) {
	if len(args) < 1 {
		fmt.Fprintf(os.Stderr, "usage: agentcore-sandbox stop <session-id>\n")
		os.Exit(1)
	}
	if err := stopSession(ctx, client, interpreterID, args[0]); err != nil {
		fmt.Fprintf(os.Stderr, "error stopping session: %v\n", err)
		os.Exit(1)
	}
	fmt.Println("session stopped")
}

// execOpts holds parsed flags for exec/run commands.
type execOpts struct {
	lang         string
	isCmd        bool
	runtime      string
	clearContext bool
	jsonOutput   bool
	timeout      int32
}

// parseExecFlags parses flags common to exec and run, returning remaining positional args.
func parseExecFlags(name string, args []string, includeTimeout bool) (execOpts, []string) {
	fs := flag.NewFlagSet(name, flag.ContinueOnError)
	var o execOpts
	fs.StringVar(&o.lang, "lang", "python", "language: python, javascript/js, typescript/ts")
	fs.BoolVar(&o.isCmd, "cmd", false, "execute as shell command")
	fs.StringVar(&o.runtime, "runtime", "", "runtime: nodejs, deno, python (auto-detected if omitted)")
	fs.BoolVar(&o.clearContext, "clear-context", false, "clear execution context")
	fs.BoolVar(&o.jsonOutput, "json", false, "structured JSON output")
	if includeTimeout {
		fs.Int32Var(&o.timeout, "timeout", 900, "session timeout in seconds")
	}
	if err := fs.Parse(args); err != nil {
		os.Exit(1)
	}

	if o.isCmd && fs.Changed("lang") {
		fmt.Fprintf(os.Stderr, "error: --cmd and --lang are mutually exclusive\n")
		os.Exit(1)
	}

	return o, fs.Args()
}

// cmdExec handles the "exec" command.
func cmdExec(ctx context.Context, client *bedrockagentcore.Client, interpreterID string, args []string) {
	if len(args) < 1 {
		fmt.Fprintf(os.Stderr, "usage: agentcore-sandbox exec <session-id> [flags] <code-or-command>\n")
		os.Exit(1)
	}
	sessionID := args[0]
	opts, positional := parseExecFlags("exec", args[1:], false)
	if len(positional) < 1 {
		fmt.Fprintf(os.Stderr, "usage: agentcore-sandbox exec <session-id> [flags] <code-or-command>\n")
		os.Exit(1)
	}
	codeOrCmd := positional[0]

	exitCode, err := runExecution(ctx, client, interpreterID, sessionID, codeOrCmd, opts)
	if err != nil {
		fmt.Fprintf(os.Stderr, "error: %v\n", err)
		os.Exit(1)
	}
	if exitCode != 0 {
		os.Exit(int(exitCode))
	}
}

// cmdRun handles the "run" command (one-shot: start -> execute -> stop).
func cmdRun(ctx context.Context, client *bedrockagentcore.Client, interpreterID string, args []string) {
	opts, positional := parseExecFlags("run", args, true)
	if len(positional) < 1 {
		fmt.Fprintf(os.Stderr, "usage: agentcore-sandbox run [flags] <code-or-command>\n")
		os.Exit(1)
	}
	codeOrCmd := positional[0]

	sessionID, err := startSession(ctx, client, interpreterID, opts.timeout)
	if err != nil {
		fmt.Fprintf(os.Stderr, "error starting session: %v\n", err)
		os.Exit(1)
	}

	exitCode, err := runExecution(ctx, client, interpreterID, sessionID, codeOrCmd, opts)

	// Always stop the session, even on error or non-zero exit code.
	if stopErr := stopSession(ctx, client, interpreterID, sessionID); stopErr != nil {
		fmt.Fprintf(os.Stderr, "error stopping session: %v\n", stopErr)
	}

	if err != nil {
		fmt.Fprintf(os.Stderr, "error: %v\n", err)
		os.Exit(1)
	}
	if exitCode != 0 {
		os.Exit(int(exitCode))
	}
}

// runExecution dispatches to executeCommand or executeCode based on opts.
func runExecution(ctx context.Context, client *bedrockagentcore.Client, interpreterID, sessionID, codeOrCmd string, opts execOpts) (int32, error) {
	if opts.isCmd {
		return executeCommand(ctx, client, interpreterID, sessionID, codeOrCmd, opts.jsonOutput)
	}
	lang, err := resolveLanguage(opts.lang)
	if err != nil {
		return 1, err
	}
	runtime, err := resolveRuntime(lang, opts.runtime)
	if err != nil {
		return 1, err
	}
	return executeCode(ctx, client, interpreterID, sessionID, codeOrCmd, lang, runtime, opts.clearContext, opts.jsonOutput)
}

// resolveLanguage maps user input to a ProgrammingLanguage enum value.
func resolveLanguage(input string) (types.ProgrammingLanguage, error) {
	switch strings.ToLower(input) {
	case "python":
		return types.ProgrammingLanguagePython, nil
	case "javascript", "js":
		return types.ProgrammingLanguageJavascript, nil
	case "typescript", "ts":
		return types.ProgrammingLanguageTypescript, nil
	default:
		return "", fmt.Errorf("unsupported language %q (valid: python, javascript/js, typescript/ts)", input)
	}
}

// resolveRuntime determines the runtime from the language and optional explicit override.
func resolveRuntime(lang types.ProgrammingLanguage, explicit string) (types.LanguageRuntime, error) {
	if explicit != "" {
		switch strings.ToLower(explicit) {
		case "nodejs":
			return types.LanguageRuntimeNodejs, nil
		case "deno":
			return types.LanguageRuntimeDeno, nil
		case "python":
			return types.LanguageRuntimePython, nil
		default:
			return "", fmt.Errorf("unsupported runtime %q (valid: nodejs, deno, python)", explicit)
		}
	}
	// Auto-detect from language
	switch lang {
	case types.ProgrammingLanguagePython:
		return types.LanguageRuntimePython, nil
	default:
		return types.LanguageRuntimeDeno, nil
	}
}

func startSession(ctx context.Context, client *bedrockagentcore.Client, interpreterID string, timeout int32) (string, error) {
	out, err := client.StartCodeInterpreterSession(ctx, &bedrockagentcore.StartCodeInterpreterSessionInput{
		CodeInterpreterIdentifier: &interpreterID,
		SessionTimeoutSeconds:     &timeout,
	})
	if err != nil {
		return "", err
	}
	if out.SessionId == nil {
		return "", fmt.Errorf("API returned nil session ID")
	}
	return *out.SessionId, nil
}

func stopSession(ctx context.Context, client *bedrockagentcore.Client, interpreterID, sessionID string) error {
	_, err := client.StopCodeInterpreterSession(ctx, &bedrockagentcore.StopCodeInterpreterSessionInput{
		CodeInterpreterIdentifier: &interpreterID,
		SessionId:                 &sessionID,
	})
	return err
}

// executionResult holds collected output for JSON mode.
type executionResult struct {
	Stdout        string  `json:"stdout"`
	Stderr        string  `json:"stderr"`
	ExitCode      int32   `json:"exitCode"`
	ExecutionTime float64 `json:"executionTime"`
	IsError       bool    `json:"isError"`
}

func streamOutput(stream *bedrockagentcore.InvokeCodeInterpreterEventStream, jsonOutput bool) (int32, error) {
	defer stream.Close()

	var result executionResult
	var stdoutBuf, stderrBuf strings.Builder

	for event := range stream.Events() {
		switch v := event.(type) {
		case *types.CodeInterpreterStreamOutputMemberResult:
			if v.Value.IsError != nil {
				result.IsError = *v.Value.IsError
			}
			for _, block := range v.Value.Content {
				if block.Type == types.ContentBlockTypeText && block.Text != nil {
					if jsonOutput {
						stdoutBuf.WriteString(*block.Text)
					} else {
						fmt.Print(*block.Text)
					}
				}
			}
			if sc := v.Value.StructuredContent; sc != nil {
				if sc.Stderr != nil {
					if jsonOutput {
						stderrBuf.WriteString(*sc.Stderr)
					} else {
						fmt.Fprint(os.Stderr, *sc.Stderr)
					}
				}
				if sc.ExitCode != nil {
					result.ExitCode = *sc.ExitCode
				}
				if sc.ExecutionTime != nil {
					result.ExecutionTime = *sc.ExecutionTime
				}
			}
		}
	}

	if err := stream.Err(); err != nil {
		return 1, err
	}

	if jsonOutput {
		result.Stdout = stdoutBuf.String()
		result.Stderr = stderrBuf.String()
		out, err := json.Marshal(result)
		if err != nil {
			return 1, fmt.Errorf("error marshaling JSON: %w", err)
		}
		fmt.Println(string(out))
	}

	return result.ExitCode, nil
}

func executeCode(ctx context.Context, client *bedrockagentcore.Client, interpreterID, sessionID, code string, lang types.ProgrammingLanguage, runtime types.LanguageRuntime, clearContext, jsonOutput bool) (int32, error) {
	args := &types.ToolArguments{
		Code:     &code,
		Language: lang,
		Runtime:  runtime,
	}
	if clearContext {
		args.ClearContext = &clearContext
	}
	out, err := client.InvokeCodeInterpreter(ctx, &bedrockagentcore.InvokeCodeInterpreterInput{
		CodeInterpreterIdentifier: &interpreterID,
		SessionId:                 &sessionID,
		Name:                      types.ToolNameExecuteCode,
		Arguments:                 args,
	})
	if err != nil {
		return 1, err
	}
	return streamOutput(out.GetStream(), jsonOutput)
}

func executeCommand(ctx context.Context, client *bedrockagentcore.Client, interpreterID, sessionID, command string, jsonOutput bool) (int32, error) {
	out, err := client.InvokeCodeInterpreter(ctx, &bedrockagentcore.InvokeCodeInterpreterInput{
		CodeInterpreterIdentifier: &interpreterID,
		SessionId:                 &sessionID,
		Name:                      types.ToolNameExecuteCommand,
		Arguments: &types.ToolArguments{
			Command: &command,
		},
	})
	if err != nil {
		return 1, err
	}
	return streamOutput(out.GetStream(), jsonOutput)
}

func printHelp() {
	fmt.Println(`Sample Bedrock AgentCore Code Interpreter wrapper - https://my2cents.ai/

Usage: agentcore-sandbox <command> [flags] [args...]

Commands:
  start [--timeout N] [--json]          Start a new session, prints session ID
  stop <session-id>                     Stop an existing session
  exec <session-id> [flags] <code>      Execute code/command in existing session
  run [flags] <code>                    One-shot: start, execute, stop
  help                                  Show this help message

Flags for exec and run:
  --lang string          Language: python (default), javascript/js, typescript/ts
  --cmd                  Execute as shell command (mutually exclusive with --lang)
  --runtime string       Runtime: nodejs, deno, python (auto-detected if omitted)
  --clear-context        Clear execution context before running
  --json                 Output structured JSON result
  --timeout N            Session timeout in seconds (run and start only, default 900)

Examples:
  agentcore-sandbox run 'print("hello")'
  agentcore-sandbox run --lang js 'console.log("hello")'
  agentcore-sandbox run --lang ts 'const x: number = 42; console.log(x)'
  agentcore-sandbox run --cmd 'ls -la'
  agentcore-sandbox run --json --lang python 'print(42)'

  SESSION=$(agentcore-sandbox start --timeout 1800)
  agentcore-sandbox exec $SESSION --lang ts 'const x = 1'
  agentcore-sandbox exec $SESSION --lang ts 'console.log(x)'
  agentcore-sandbox stop $SESSION

Environment variables:
  CUSTOM_AGENTCORE_INTERPRETER_ID    Code interpreter identifier (required)
  CUSTOM_AGENTCORE_AWS_PROFILE       AWS shared config profile (optional)
  CUSTOM_AGENTCORE_AWS_REGION        AWS region override (optional)`)
}
