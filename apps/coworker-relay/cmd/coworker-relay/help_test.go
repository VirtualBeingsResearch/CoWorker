package main

import (
	"bytes"
	"strings"
	"testing"
)

func TestHelpDocumentsE2EEDeploymentWithoutObsoleteCommands(t *testing.T) {
	var output bytes.Buffer
	handled, err := handleHelp([]string{"--help"}, &output)
	if err != nil || !handled {
		t.Fatalf("root help failed: handled=%v err=%v", handled, err)
	}
	text := output.String()
	for _, required := range []string{
		"RELAY_PUBLIC_URL=http://relay.example.com:8443",
		"RELAY_ADMIN_URL=http://127.0.0.1:8444",
		"instance",
		"bans",
	} {
		if !strings.Contains(text, required) {
			t.Fatalf("root help lacks %q:\n%s", required, text)
		}
	}
	for _, obsolete := range []string{"cache", "update-auth", "rotate-credential", "ACME"} {
		if strings.Contains(text, obsolete) {
			t.Fatalf("root help contains obsolete %q:\n%s", obsolete, text)
		}
	}
}

func TestInitHelpIncludesHTTPExampleAndNoCertificateFlags(t *testing.T) {
	var output bytes.Buffer
	handled, err := handleHelp([]string{"init", "--help"}, &output)
	if err != nil || !handled {
		t.Fatalf("init help failed: handled=%v err=%v", handled, err)
	}
	text := output.String()
	if !strings.Contains(text, "http://203.0.113.10:8443") ||
		!strings.Contains(text, "--admin-port") ||
		!strings.Contains(text, "--deployment") ||
		!strings.Contains(text, "default: current directory") {
		t.Fatalf("unexpected init help:\n%s", text)
	}
	if strings.Contains(text, "--tls-cert") || strings.Contains(text, "--acme-domain") {
		t.Fatalf("obsolete TLS setup remains:\n%s", text)
	}
}

func TestInstanceCreateHelpDocumentsExpiredInstanceCleanup(t *testing.T) {
	var output bytes.Buffer
	handled, err := handleHelp([]string{"instance", "create", "--help"}, &output)
	if err != nil || !handled {
		t.Fatalf("instance create help failed: handled=%v err=%v", handled, err)
	}
	text := output.String()
	if !strings.Contains(text, "removed automatically") ||
		!strings.Contains(text, "pairing code expires") {
		t.Fatalf("instance create help lacks expiration cleanup:\n%s", text)
	}
}

func TestEverySupportedSubcommandHasHelp(t *testing.T) {
	for _, topic := range []string{
		"serve", "init", "health", "version", "instance", "instance create",
		"instance list", "instance revoke", "bans", "bans list", "bans remove",
		"backup", "restore", "gc", "metrics", "help",
	} {
		var output bytes.Buffer
		handled, err := handleHelp(append([]string{"help"}, strings.Split(topic, " ")...), &output)
		if err != nil || !handled || output.Len() == 0 {
			t.Fatalf("help %q failed: handled=%v err=%v", topic, handled, err)
		}
	}
}

func TestExtractConfigArgument(t *testing.T) {
	args, path, err := extractConfigArgument(
		[]string{"health", "--config", "/tmp/relay.env"},
	)
	if err != nil {
		t.Fatal(err)
	}
	if path != "/tmp/relay.env" || len(args) != 1 || args[0] != "health" {
		t.Fatalf("unexpected extraction: args=%v path=%q", args, path)
	}
	if _, _, err := extractConfigArgument(
		[]string{"--config=one", "--config", "two", "health"},
	); err == nil {
		t.Fatal("duplicate --config was accepted")
	}
}
