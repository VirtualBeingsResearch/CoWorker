package main

import (
	"bytes"
	"strings"
	"testing"
)

func TestHandleHelpRootForms(t *testing.T) {
	t.Parallel()
	for _, args := range [][]string{
		nil,
		{"-h"},
		{"--help"},
		{"help"},
		{"help", "--help"},
	} {
		args := args
		t.Run(strings.Join(args, "_"), func(t *testing.T) {
			t.Parallel()
			var output bytes.Buffer
			handled, err := handleHelp(args, &output)
			if err != nil {
				t.Fatal(err)
			}
			if !handled {
				t.Fatal("root help was not handled")
			}
			if !strings.Contains(output.String(), "Run and administer Coworker Relay") {
				t.Fatalf("unexpected root help:\n%s", output.String())
			}
		})
	}
}

func TestHandleHelpCoversEveryCommand(t *testing.T) {
	t.Parallel()
	tests := []struct {
		topic    string
		contains string
	}{
		{"help", "coworker-relay help <command>"},
		{"serve", "coworker-relay serve"},
		{"init", "coworker-relay init"},
		{"health", "coworker-relay health"},
		{"version", "coworker-relay version"},
		{"backup", "coworker-relay backup"},
		{"restore", "coworker-relay restore"},
		{"gc", "coworker-relay gc"},
		{"metrics", "coworker-relay metrics"},
		{"instance", "coworker-relay instance <subcommand>"},
		{"instance create", "coworker-relay instance create"},
		{"instance list", "coworker-relay instance list"},
		{"instance revoke", "coworker-relay instance revoke"},
		{"instance rotate-credential", "coworker-relay instance rotate-credential"},
		{"instance update-auth", "coworker-relay instance update-auth"},
		{"instance update-stats", "coworker-relay instance update-stats"},
		{"bans", "coworker-relay bans <subcommand>"},
		{"bans list", "coworker-relay bans list"},
		{"bans remove", "coworker-relay bans remove"},
		{"cache", "coworker-relay cache <subcommand>"},
		{"cache inspect", "coworker-relay cache inspect"},
		{"cache purge", "coworker-relay cache purge"},
	}
	for _, test := range tests {
		test := test
		t.Run(test.topic, func(t *testing.T) {
			t.Parallel()
			args := append([]string{"help"}, strings.Fields(test.topic)...)
			var output bytes.Buffer
			handled, err := handleHelp(args, &output)
			if err != nil {
				t.Fatal(err)
			}
			if !handled {
				t.Fatal("help request was not handled")
			}
			if !strings.Contains(output.String(), test.contains) {
				t.Fatalf(
					"help for %q does not contain %q:\n%s",
					test.topic,
					test.contains,
					output.String(),
				)
			}
		})
	}
}

func TestHandleHelpAfterCommandsAndOptions(t *testing.T) {
	t.Parallel()
	tests := []struct {
		args     []string
		contains string
	}{
		{[]string{"init", "--help"}, "coworker-relay init"},
		{[]string{"backup", "--output", "backup.db", "-h"}, "coworker-relay backup"},
		{[]string{"instance", "--help"}, "coworker-relay instance <subcommand>"},
		{[]string{"instance", "revoke", "cw_example", "--help"}, "instance revoke"},
		{
			[]string{
				"bans",
				"remove",
				"--instance",
				"cw_example",
				"--ip",
				"192.0.2.1",
				"--help",
			},
			"bans remove",
		},
		{[]string{"cache", "inspect", "-h"}, "cache inspect"},
	}
	for _, test := range tests {
		test := test
		t.Run(strings.Join(test.args, "_"), func(t *testing.T) {
			t.Parallel()
			var output bytes.Buffer
			handled, err := handleHelp(test.args, &output)
			if err != nil {
				t.Fatal(err)
			}
			if !handled {
				t.Fatal("help flag was not handled")
			}
			if !strings.Contains(output.String(), test.contains) {
				t.Fatalf("unexpected help:\n%s", output.String())
			}
		})
	}
}

func TestHandleHelpShowsBareGroupWithoutConfiguration(t *testing.T) {
	t.Parallel()
	for _, group := range []string{"instance", "bans", "cache"} {
		group := group
		t.Run(group, func(t *testing.T) {
			t.Parallel()
			var output bytes.Buffer
			handled, err := handleHelp([]string{group}, &output)
			if err != nil {
				t.Fatal(err)
			}
			if !handled {
				t.Fatalf("bare %s group was not handled", group)
			}
			if !strings.Contains(output.String(), "coworker-relay "+group+" <subcommand>") {
				t.Fatalf("unexpected group help:\n%s", output.String())
			}
		})
	}
}

func TestHandleHelpRejectsUnknownTopics(t *testing.T) {
	t.Parallel()
	for _, args := range [][]string{
		{"help", "unknown"},
		{"help", "instance", "unknown"},
		{"unknown", "--help"},
		{"instance", "unknown", "--help"},
	} {
		args := args
		t.Run(strings.Join(args, "_"), func(t *testing.T) {
			t.Parallel()
			var output bytes.Buffer
			handled, err := handleHelp(args, &output)
			if !handled {
				t.Fatal("unknown help request was not handled")
			}
			if err == nil || !strings.Contains(err.Error(), "unknown help topic") {
				t.Fatalf("unexpected error: %v", err)
			}
			if output.Len() != 0 {
				t.Fatalf("unknown help unexpectedly wrote output: %q", output.String())
			}
		})
	}
}

func TestHandleHelpIgnoresOrdinaryCommands(t *testing.T) {
	t.Parallel()
	for _, args := range [][]string{
		{"health"},
		{"instance", "list"},
		{"bans", "list"},
		{"cache", "inspect"},
	} {
		var output bytes.Buffer
		handled, err := handleHelp(args, &output)
		if err != nil {
			t.Fatal(err)
		}
		if handled {
			t.Fatalf("ordinary command %v was treated as help", args)
		}
		if output.Len() != 0 {
			t.Fatalf("ordinary command wrote help: %q", output.String())
		}
	}
}
