package auth

import (
	"encoding/base64"
	"fmt"
	"strings"
	"testing"

	"golang.org/x/crypto/argon2"
)

func verifier(password string) string {
	salt := []byte("0123456789abcdef")
	digest := argon2.IDKey([]byte(password), salt, 2, 19*1024, 1, 32)
	return fmt.Sprintf(
		"$argon2id$v=19$m=19456,t=2,p=1$%s$%s",
		base64.RawStdEncoding.EncodeToString(salt),
		base64.RawStdEncoding.EncodeToString(digest),
	)
}

func TestVerifyArgon2id(t *testing.T) {
	encoded := verifier("desktop-secret")
	if err := ValidateArgon2id(encoded); err != nil {
		t.Fatalf("valid verifier was rejected: %v", err)
	}
	ok, err := VerifyArgon2id(encoded, "desktop-secret")
	if err != nil || !ok {
		t.Fatalf("expected verifier to match: ok=%v err=%v", ok, err)
	}
	ok, err = VerifyArgon2id(encoded, "wrong")
	if err != nil || ok {
		t.Fatalf("expected wrong token to be rejected: ok=%v err=%v", ok, err)
	}
}

func TestValidateArgon2idRejectsExcessiveCost(t *testing.T) {
	encoded := strings.Replace(verifier("desktop-secret"), "m=19456", "m=1048576", 1)
	if err := ValidateArgon2id(encoded); err == nil {
		t.Fatal("excessive verifier cost was accepted")
	}
}

func TestParseBearer(t *testing.T) {
	if token, ok := ParseBearer("Bearer abc_123"); !ok || token != "abc_123" {
		t.Fatalf("unexpected bearer result: %q %v", token, ok)
	}
	for _, value := range []string{"", "Basic abc", "Bearer ", "Bearer a b", "bearer abc"} {
		if _, ok := ParseBearer(value); ok {
			t.Fatalf("expected %q to be rejected", value)
		}
	}
}
