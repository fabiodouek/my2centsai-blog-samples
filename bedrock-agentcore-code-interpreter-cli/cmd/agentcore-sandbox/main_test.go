package main

import (
	"testing"

	"github.com/aws/aws-sdk-go-v2/service/bedrockagentcore/types"
)

func TestResolveLanguage(t *testing.T) {
	tests := []struct {
		input   string
		want    types.ProgrammingLanguage
		wantErr bool
	}{
		{"python", types.ProgrammingLanguagePython, false},
		{"Python", types.ProgrammingLanguagePython, false},
		{"PYTHON", types.ProgrammingLanguagePython, false},
		{"javascript", types.ProgrammingLanguageJavascript, false},
		{"js", types.ProgrammingLanguageJavascript, false},
		{"JS", types.ProgrammingLanguageJavascript, false},
		{"typescript", types.ProgrammingLanguageTypescript, false},
		{"ts", types.ProgrammingLanguageTypescript, false},
		{"TS", types.ProgrammingLanguageTypescript, false},
		{"ruby", "", true},
		{"", "", true},
	}

	for _, tt := range tests {
		t.Run(tt.input, func(t *testing.T) {
			got, err := resolveLanguage(tt.input)
			if (err != nil) != tt.wantErr {
				t.Errorf("resolveLanguage(%q) error = %v, wantErr %v", tt.input, err, tt.wantErr)
				return
			}
			if got != tt.want {
				t.Errorf("resolveLanguage(%q) = %v, want %v", tt.input, got, tt.want)
			}
		})
	}
}

func TestResolveRuntime(t *testing.T) {
	tests := []struct {
		name     string
		lang     types.ProgrammingLanguage
		explicit string
		want     types.LanguageRuntime
		wantErr  bool
	}{
		{"explicit nodejs", types.ProgrammingLanguageJavascript, "nodejs", types.LanguageRuntimeNodejs, false},
		{"explicit deno", types.ProgrammingLanguageTypescript, "deno", types.LanguageRuntimeDeno, false},
		{"explicit python", types.ProgrammingLanguagePython, "python", types.LanguageRuntimePython, false},
		{"explicit invalid", types.ProgrammingLanguagePython, "bun", "", true},
		{"auto python", types.ProgrammingLanguagePython, "", types.LanguageRuntimePython, false},
		{"auto javascript", types.ProgrammingLanguageJavascript, "", types.LanguageRuntimeDeno, false},
		{"auto typescript", types.ProgrammingLanguageTypescript, "", types.LanguageRuntimeDeno, false},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got, err := resolveRuntime(tt.lang, tt.explicit)
			if (err != nil) != tt.wantErr {
				t.Errorf("resolveRuntime(%v, %q) error = %v, wantErr %v", tt.lang, tt.explicit, err, tt.wantErr)
				return
			}
			if got != tt.want {
				t.Errorf("resolveRuntime(%v, %q) = %v, want %v", tt.lang, tt.explicit, got, tt.want)
			}
		})
	}
}

func TestParseExecFlags(t *testing.T) {
	t.Run("defaults", func(t *testing.T) {
		opts, pos := parseExecFlags("test", []string{"print('hi')"}, false)
		if opts.lang != "python" {
			t.Errorf("default lang = %q, want %q", opts.lang, "python")
		}
		if opts.isCmd {
			t.Error("default isCmd should be false")
		}
		if opts.jsonOutput {
			t.Error("default jsonOutput should be false")
		}
		if opts.clearContext {
			t.Error("default clearContext should be false")
		}
		if len(pos) != 1 || pos[0] != "print('hi')" {
			t.Errorf("positional args = %v, want [print('hi')]", pos)
		}
	})

	t.Run("with flags", func(t *testing.T) {
		opts, pos := parseExecFlags("test", []string{"--lang", "ts", "--json", "--clear-context", "code"}, false)
		if opts.lang != "ts" {
			t.Errorf("lang = %q, want %q", opts.lang, "ts")
		}
		if !opts.jsonOutput {
			t.Error("jsonOutput should be true")
		}
		if !opts.clearContext {
			t.Error("clearContext should be true")
		}
		if len(pos) != 1 || pos[0] != "code" {
			t.Errorf("positional args = %v, want [code]", pos)
		}
	})

	t.Run("with timeout", func(t *testing.T) {
		opts, _ := parseExecFlags("test", []string{"--timeout", "1800", "code"}, true)
		if opts.timeout != 1800 {
			t.Errorf("timeout = %d, want %d", opts.timeout, 1800)
		}
	})

	t.Run("cmd flag", func(t *testing.T) {
		opts, pos := parseExecFlags("test", []string{"--cmd", "ls -la"}, false)
		if !opts.isCmd {
			t.Error("isCmd should be true")
		}
		if len(pos) != 1 || pos[0] != "ls -la" {
			t.Errorf("positional args = %v, want [ls -la]", pos)
		}
	})
}
