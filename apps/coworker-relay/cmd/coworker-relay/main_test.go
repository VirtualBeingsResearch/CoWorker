package main

import (
	"os"
	"path/filepath"
	"testing"
)

func TestSecretEnvironmentReadsMountedSecretFile(t *testing.T) {
	path := filepath.Join(t.TempDir(), "admin-token")
	if err := os.WriteFile(path, []byte("secret-from-file\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	t.Setenv("RELAY_ADMIN_TOKEN", "")
	t.Setenv("RELAY_ADMIN_TOKEN_FILE", path)
	value, err := secretEnvironment("RELAY_ADMIN_TOKEN")
	if err != nil || value != "secret-from-file" {
		t.Fatalf("unexpected secret: %q %v", value, err)
	}
}

func TestSecretEnvironmentRejectsAmbiguousSources(t *testing.T) {
	t.Setenv("RELAY_ADMIN_TOKEN", "environment-secret")
	t.Setenv("RELAY_ADMIN_TOKEN_FILE", "/run/secrets/admin-token")
	if _, err := secretEnvironment("RELAY_ADMIN_TOKEN"); err == nil {
		t.Fatal("accepted both environment and file secret sources")
	}
}
