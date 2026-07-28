package main

import (
	"bufio"
	"bytes"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestLoadLocalEnvironmentUsesCurrentDirectoryAndPreservesEnvironment(t *testing.T) {
	directory := t.TempDir()
	if err := os.WriteFile(
		filepath.Join(directory, ".env"),
		[]byte(
			"RELAY_TEST_FROM_FILE=file-value\n"+
				"RELAY_TEST_EXPLICIT=file-value\n"+
				"UNRELATED_VALUE=ignored\n",
		),
		0o600,
	); err != nil {
		t.Fatal(err)
	}
	workingDirectory, err := os.Getwd()
	if err != nil {
		t.Fatal(err)
	}
	if err := os.Chdir(directory); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = os.Chdir(workingDirectory) })

	restoreEnvironment := preserveEnvironment(
		t,
		"RELAY_CONFIG",
		"RELAY_TEST_FROM_FILE",
		"RELAY_TEST_EXPLICIT",
		"UNRELATED_VALUE",
	)
	t.Cleanup(restoreEnvironment)
	_ = os.Unsetenv("RELAY_CONFIG")
	_ = os.Unsetenv("RELAY_TEST_FROM_FILE")
	_ = os.Unsetenv("UNRELATED_VALUE")
	if err := os.Setenv("RELAY_TEST_EXPLICIT", "process-value"); err != nil {
		t.Fatal(err)
	}

	if err := loadLocalEnvironment(); err != nil {
		t.Fatal(err)
	}
	if value := os.Getenv("RELAY_TEST_FROM_FILE"); value != "file-value" {
		t.Fatalf("file value = %q", value)
	}
	if value := os.Getenv("RELAY_TEST_EXPLICIT"); value != "process-value" {
		t.Fatalf("explicit environment was replaced with %q", value)
	}
	if _, exists := os.LookupEnv("UNRELATED_VALUE"); exists {
		t.Fatal("non-RELAY value was loaded")
	}
}

func TestLoadLocalEnvironmentReportsMissingExplicitConfig(t *testing.T) {
	restoreEnvironment := preserveEnvironment(t, "RELAY_CONFIG")
	t.Cleanup(restoreEnvironment)
	if err := os.Setenv(
		"RELAY_CONFIG",
		filepath.Join(t.TempDir(), "missing.env"),
	); err != nil {
		t.Fatal(err)
	}
	if err := loadLocalEnvironment(); err == nil {
		t.Fatal("missing explicit configuration was ignored")
	}
}

func preserveEnvironment(t *testing.T, names ...string) func() {
	t.Helper()
	type savedValue struct {
		value  string
		exists bool
	}
	saved := make(map[string]savedValue, len(names))
	for _, name := range names {
		value, exists := os.LookupEnv(name)
		saved[name] = savedValue{value: value, exists: exists}
	}
	return func() {
		for name, value := range saved {
			if value.exists {
				_ = os.Setenv(name, value.value)
			} else {
				_ = os.Unsetenv(name)
			}
		}
	}
}

func TestInitializeCreatesSecureComposeDeployment(t *testing.T) {
	directory := filepath.Join(t.TempDir(), "deployment")
	token, publicURL, err := initialize(initOptions{
		directory:    directory,
		publicURL:    "https://relay.example.com",
		externalPort: 8443,
		image:        "coworker-relay:test",
	})
	if err != nil {
		t.Fatal(err)
	}
	if len(token) < 40 || publicURL != "https://relay.example.com:8443" {
		t.Fatalf("unexpected initialization result: token=%q url=%q", token, publicURL)
	}
	envPath := filepath.Join(directory, ".env")
	info, err := os.Stat(envPath)
	if err != nil {
		t.Fatal(err)
	}
	if info.Mode().Perm() != 0o600 {
		t.Fatalf(".env mode is %o", info.Mode().Perm())
	}
	env, _ := os.ReadFile(envPath)
	if !strings.Contains(string(env), "RELAY_ADMIN_TOKEN="+token) ||
		!strings.Contains(string(env), "RELAY_URL="+publicURL) ||
		!strings.Contains(string(env), "RELAY_ACME_DOMAIN=relay.example.com") {
		t.Fatalf("unexpected .env: %s", env)
	}
	compose, _ := os.ReadFile(filepath.Join(directory, "compose.yaml"))
	if !strings.Contains(string(compose), `"${RELAY_EXTERNAL_PORT}:8443"`) ||
		!strings.Contains(string(compose), `"80:8080"`) ||
		!strings.Contains(string(compose), `["CMD", "coworker-relay", "health"]`) {
		t.Fatalf("unexpected compose file: %s", compose)
	}
	if _, _, err := initialize(initOptions{
		directory:    directory,
		publicURL:    publicURL,
		externalPort: 8443,
		image:        "coworker-relay:test",
	}); err == nil {
		t.Fatal("initialization overwrote an existing deployment without --force")
	}
}

func TestInitializePublicIPUsesAutomaticShortLivedACME(t *testing.T) {
	directory := filepath.Join(t.TempDir(), "deployment")
	_, publicURL, err := initialize(initOptions{
		directory:    directory,
		publicURL:    "https://221.228.203.18:8443",
		externalPort: 8443,
		image:        "coworker-relay:test",
	})
	if err != nil {
		t.Fatal(err)
	}
	if publicURL != "https://221.228.203.18:8443" {
		t.Fatalf("unexpected public URL: %s", publicURL)
	}
	env, err := os.ReadFile(filepath.Join(directory, ".env"))
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(env), "RELAY_ACME_IP=221.228.203.18\n") ||
		strings.Contains(string(env), "RELAY_ACME_DOMAIN=") ||
		strings.Contains(string(env), "RELAY_TLS_CERT=") {
		t.Fatalf("unexpected public-IP environment:\n%s", env)
	}
	compose, err := os.ReadFile(filepath.Join(directory, "compose.yaml"))
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(compose), `"80:8080"`) {
		t.Fatalf("public-IP ACME challenge port is missing:\n%s", compose)
	}
}

func TestInitializePublicIPv6UsesCanonicalACMEIdentifier(t *testing.T) {
	directory := filepath.Join(t.TempDir(), "deployment")
	_, publicURL, err := initialize(initOptions{
		directory:    directory,
		publicURL:    "https://[2001:4860:4860::8888]:8443",
		externalPort: 8443,
		image:        "coworker-relay:test",
	})
	if err != nil {
		t.Fatal(err)
	}
	if publicURL != "https://[2001:4860:4860::8888]:8443" {
		t.Fatalf("unexpected public URL: %s", publicURL)
	}
	env, err := os.ReadFile(filepath.Join(directory, ".env"))
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(env), "RELAY_ACME_IP=2001:4860:4860::8888\n") {
		t.Fatalf("unexpected IPv6 environment:\n%s", env)
	}
}

func TestInitializeReservedIPRequiresPEM(t *testing.T) {
	for _, address := range []string{
		"10.0.0.1",
		"100.64.0.1",
		"192.0.2.10",
		"198.51.100.10",
		"203.0.113.10",
	} {
		address := address
		t.Run(address, func(t *testing.T) {
			_, _, err := initialize(initOptions{
				directory:    filepath.Join(t.TempDir(), "deployment"),
				publicURL:    "https://" + address + ":8443",
				externalPort: 8443,
				image:        "coworker-relay:test",
			})
			if err == nil || !strings.Contains(err.Error(), "requires --tls-cert and --tls-key") {
				t.Fatalf("unexpected error for %s: %v", address, err)
			}
		})
	}
}

func TestPromptInitOptionsDefaultsPublicIPToAutomaticACME(t *testing.T) {
	input := strings.NewReader(
		"\n" +
			"\n" +
			"https://221.228.203.18:8443\n" +
			"\n" +
			"\n" +
			"\n",
	)
	var output bytes.Buffer
	options, err := promptInitOptions(input, &output, defaultInitOptions())
	if err != nil {
		t.Fatal(err)
	}
	if options.acmeDomain != "221.228.203.18" ||
		options.tlsCert != "" ||
		options.tlsKey != "" {
		t.Fatalf("unexpected prompted TLS options: %#v", options)
	}
	if !strings.Contains(output.String(), "Automatic public-IP certificate") {
		t.Fatalf("public-IP automatic TLS choice was not shown:\n%s", output.String())
	}
}

func TestPromptConfirmHonorsDefaultOnEnter(t *testing.T) {
	for _, test := range []struct {
		name     string
		fallback bool
	}{
		{name: "yes", fallback: true},
		{name: "no", fallback: false},
	} {
		t.Run(test.name, func(t *testing.T) {
			reader := strings.NewReader("\n")
			var output bytes.Buffer
			actual, err := promptConfirm(
				bufio.NewReader(reader),
				&output,
				"Continue?",
				test.fallback,
			)
			if err != nil {
				t.Fatal(err)
			}
			if actual != test.fallback {
				t.Fatalf("confirmation = %t, want %t", actual, test.fallback)
			}
		})
	}
}

func TestCharacterDeviceIsNotAssumedToBeTerminal(t *testing.T) {
	null, err := os.Open(os.DevNull)
	if err != nil {
		t.Fatal(err)
	}
	defer null.Close()
	if isTerminal(null) {
		t.Fatal("character device without a terminal was accepted as interactive input")
	}
}

func TestInitializePEMModeMountsCertificates(t *testing.T) {
	directory := filepath.Join(t.TempDir(), "deployment")
	cert := filepath.Join(t.TempDir(), "certificate.pem")
	key := filepath.Join(t.TempDir(), "private-key.pem")
	if _, _, err := initialize(initOptions{
		directory:    directory,
		publicURL:    "https://192.0.2.10:8443",
		externalPort: 8443,
		tlsCert:      cert,
		tlsKey:       key,
		image:        "coworker-relay:test",
	}); err != nil {
		t.Fatal(err)
	}
	compose, _ := os.ReadFile(filepath.Join(directory, "compose.yaml"))
	if !strings.Contains(string(compose), cert+":/run/tls/fullchain.pem:ro") ||
		!strings.Contains(string(compose), key+":/run/tls/privkey.pem:ro") {
		t.Fatalf("certificate mounts missing: %s", compose)
	}
}

func TestHTTPClientRejectsInvalidPrivateCA(t *testing.T) {
	path := filepath.Join(t.TempDir(), "invalid-ca.pem")
	if err := os.WriteFile(path, []byte("not a certificate"), 0o600); err != nil {
		t.Fatal(err)
	}
	t.Setenv("RELAY_CA_CERT", path)
	if _, err := newHTTPClient(); err == nil {
		t.Fatal("invalid private CA was accepted")
	}
}
