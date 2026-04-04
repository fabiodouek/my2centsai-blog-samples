package main

import (
	"context"
	"encoding/json"
	"fmt"
	"os"

	"github.com/aws/aws-sdk-go-v2/config"
	"github.com/aws/aws-sdk-go-v2/service/bedrockagentcore"
	"github.com/aws/aws-sdk-go-v2/service/bedrockagentcore/types"
)

const defaultInterpreterID = "aws.codeinterpreter.v1"

func main() {
	if len(os.Args) < 2 {
		fmt.Fprintf(os.Stderr, "usage: agentcore-sandbox <start|exec|stop> [args...]\n")
		os.Exit(1)
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
		interpreterID = defaultInterpreterID
	}

	switch os.Args[1] {
	case "start":
		startSession(ctx, client, interpreterID)
	case "exec":
		if len(os.Args) < 4 {
			fmt.Fprintf(os.Stderr, "usage: agentcore-sandbox exec <session-id> <code>\n")
			os.Exit(1)
		}
		executeCode(ctx, client, interpreterID, os.Args[2], os.Args[3])
	case "stop":
		if len(os.Args) < 3 {
			fmt.Fprintf(os.Stderr, "usage: agentcore-sandbox stop <session-id>\n")
			os.Exit(1)
		}
		stopSession(ctx, client, interpreterID, os.Args[2])
	default:
		fmt.Fprintf(os.Stderr, "unknown command: %s\n", os.Args[1])
		os.Exit(1)
	}
}

func startSession(ctx context.Context, client *bedrockagentcore.Client, interpreterID string) {
	timeout := int32(900) // 15 minutes
	out, err := client.StartCodeInterpreterSession(ctx, &bedrockagentcore.StartCodeInterpreterSessionInput{
		CodeInterpreterIdentifier: &interpreterID,
		SessionTimeoutSeconds:     &timeout,
	})
	if err != nil {
		fmt.Fprintf(os.Stderr, "error starting session: %v\n", err)
		os.Exit(1)
	}
	fmt.Println(*out.SessionId)
}

func executeCode(ctx context.Context, client *bedrockagentcore.Client, interpreterID, sessionID, code string) {
	argsJSON, _ := json.Marshal(map[string]string{
		"language": "python",
		"code":     code,
	})

	toolName := "executeCode"
	out, err := client.InvokeCodeInterpreter(ctx, &bedrockagentcore.InvokeCodeInterpreterInput{
		CodeInterpreterIdentifier: &interpreterID,
		SessionId:                 &sessionID,
		Name:                      &toolName,
		Arguments:                 argsJSON,
	})
	if err != nil {
		fmt.Fprintf(os.Stderr, "error invoking code interpreter: %v\n", err)
		os.Exit(1)
	}

	stream := out.GetStream()
	defer stream.Close()

	for event := range stream.Events() {
		switch v := event.(type) {
		case *types.CodeInterpreterStreamOutputMemberResult:
			for _, block := range v.Value.Content {
				if block.Type == types.ContentBlockTypeText && block.Text != nil {
					fmt.Print(*block.Text)
				}
			}
			if v.Value.StructuredContent != nil {
				if v.Value.StructuredContent.Stderr != nil {
					fmt.Fprintf(os.Stderr, "%s", *v.Value.StructuredContent.Stderr)
				}
			}
		}
	}

	if err := stream.Err(); err != nil {
		fmt.Fprintf(os.Stderr, "stream error: %v\n", err)
		os.Exit(1)
	}
}

func stopSession(ctx context.Context, client *bedrockagentcore.Client, interpreterID, sessionID string) {
	_, err := client.StopCodeInterpreterSession(ctx, &bedrockagentcore.StopCodeInterpreterSessionInput{
		CodeInterpreterIdentifier: &interpreterID,
		SessionId:                 &sessionID,
	})
	if err != nil {
		fmt.Fprintf(os.Stderr, "error stopping session: %v\n", err)
		os.Exit(1)
	}
	fmt.Println("session stopped")
}
