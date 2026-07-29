package main

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestInitializeCreatesPlainWebSocketComposeAndLoopbackAdmin(t *testing.T) {
	directory := filepath.Join(t.TempDir(), "relay")
	token, publicURL, err := initialize(initOptions{
		directory: directory, publicURL: "http://203.0.113.10:8443",
		externalPort: 8443, adminPort: 9444, deployment: deploymentContainer,
		image: "example/relay:test",
	})
	if err != nil {
		t.Fatal(err)
	}
	if len(token) < 32 || publicURL != "http://203.0.113.10:8443" {
		t.Fatalf("unexpected init result: %q %q", token, publicURL)
	}
	environment, _ := os.ReadFile(filepath.Join(directory, ".env"))
	compose, _ := os.ReadFile(filepath.Join(directory, "compose.yaml"))
	if strings.Contains(string(environment), "TLS") ||
		strings.Contains(string(environment), "ACME") ||
		strings.Contains(string(environment), "CACHE") {
		t.Fatalf("obsolete settings remain:\n%s", environment)
	}
	if !strings.Contains(string(compose), `"${RELAY_EXTERNAL_PORT}:8443"`) ||
		!strings.Contains(string(compose), `"127.0.0.1:${RELAY_ADMIN_PORT}:8444"`) ||
		!strings.Contains(
			string(compose),
			"RELAY_ADMIN_URL: http://127.0.0.1:8444",
		) {
		t.Fatalf("unexpected compose ports:\n%s", compose)
	}
	info, err := os.Stat(filepath.Join(directory, ".env"))
	if err != nil || info.Mode().Perm() != 0o600 {
		t.Fatalf(".env permissions=%v err=%v", info.Mode().Perm(), err)
	}
}

func TestInitializeCreatesNativeConfigurationWithoutCompose(t *testing.T) {
	directory := filepath.Join(t.TempDir(), "relay")
	_, _, err := initialize(initOptions{
		directory: directory, publicURL: "http://203.0.113.10:9443",
		externalPort: 9443, adminPort: 9444, deployment: deploymentNative,
	})
	if err != nil {
		t.Fatal(err)
	}
	environment, err := os.ReadFile(filepath.Join(directory, ".env"))
	if err != nil {
		t.Fatal(err)
	}
	text := string(environment)
	for _, required := range []string{
		"RELAY_LISTEN=:9443",
		"RELAY_ADMIN_LISTEN=127.0.0.1:9444",
		"RELAY_DATABASE=" + filepath.Join(directory, "data", "relay.db"),
		"RELAY_SIGNING_KEY=" + filepath.Join(directory, "data", "relay-signing.key"),
	} {
		if !strings.Contains(text, required) {
			t.Fatalf("native environment lacks %q:\n%s", required, text)
		}
	}
	for _, containerOnly := range []string{"RELAY_IMAGE=", "RELAY_EXTERNAL_PORT="} {
		if strings.Contains(text, containerOnly) {
			t.Fatalf("native environment contains %q:\n%s", containerOnly, text)
		}
	}
	if _, err := os.Stat(filepath.Join(directory, "compose.yaml")); !os.IsNotExist(err) {
		t.Fatalf("native init generated compose.yaml: %v", err)
	}
	gitignore, _ := os.ReadFile(filepath.Join(directory, ".gitignore"))
	if !strings.Contains(string(gitignore), "data/") {
		t.Fatalf("native data directory is not ignored:\n%s", gitignore)
	}
}

func TestPromptInitOptionsChoosesNativeDeployment(t *testing.T) {
	input := strings.NewReader(
		"\nhttp://203.0.113.10:8443\n\n\nno\nyes\n",
	)
	var output strings.Builder
	options, err := promptInitOptions(input, &output, defaultInitOptions())
	if err != nil {
		t.Fatal(err)
	}
	if options.deployment != deploymentNative {
		t.Fatalf("deployment=%q, want native", options.deployment)
	}
	if strings.Contains(output.String(), "Container image") {
		t.Fatalf("native setup prompted for an image:\n%s", output.String())
	}
}

func TestParseInitOptionsRejectsUnknownDeployment(t *testing.T) {
	_, err := parseInitOptions([]string{
		"--public-url", "http://203.0.113.10:8443",
		"--deployment", "binary",
	})
	if err == nil || !strings.Contains(err.Error(), "container or native") {
		t.Fatalf("unexpected error: %v", err)
	}
}

func TestNativeForceRemovesGeneratedComposeFile(t *testing.T) {
	directory := filepath.Join(t.TempDir(), "relay")
	container := initOptions{
		directory: directory, publicURL: "http://203.0.113.10:8443",
		externalPort: 8443, adminPort: 8444, deployment: deploymentContainer,
		image: "example/relay:test",
	}
	if _, _, err := initialize(container); err != nil {
		t.Fatal(err)
	}
	container.deployment = deploymentNative
	container.force = true
	if _, _, err := initialize(container); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(filepath.Join(directory, "compose.yaml")); !os.IsNotExist(err) {
		t.Fatalf("native force left stale compose.yaml: %v", err)
	}
}

func TestNormalizePublicURLAcceptsHTTPAndRequiresMatchingPort(t *testing.T) {
	_, normalized, err := normalizePublicURL("http://relay.test", 8443)
	if err != nil || normalized != "http://relay.test:8443" {
		t.Fatalf("unexpected result: %q %v", normalized, err)
	}
	if _, _, err := normalizePublicURL("https://relay.test:443", 8443); err == nil {
		t.Fatal("mismatched port was accepted")
	}
}
