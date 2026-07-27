package auth

import (
	"crypto/subtle"
	"encoding/base64"
	"errors"
	"fmt"
	"strconv"
	"strings"

	"golang.org/x/crypto/argon2"
)

type argonParams struct {
	memory      uint32
	iterations  uint32
	parallelism uint8
}

func parseArgon2id(encoded string) (argonParams, []byte, []byte, error) {
	var params argonParams
	parts := strings.Split(encoded, "$")
	if len(parts) != 6 || parts[1] != "argon2id" || parts[2] != "v=19" {
		return params, nil, nil, errors.New("invalid Argon2id verifier")
	}
	if _, err := fmt.Sscanf(parts[3], "m=%d,t=%d,p=%d", &params.memory, &params.iterations, &params.parallelism); err != nil {
		return params, nil, nil, errors.New("invalid Argon2id parameters")
	}
	if fmt.Sprintf(
		"m=%d,t=%d,p=%d",
		params.memory,
		params.iterations,
		params.parallelism,
	) != parts[3] {
		return params, nil, nil, errors.New("invalid Argon2id parameters")
	}
	if params.memory < 8*1024 || params.memory > 64*1024 || params.iterations < 1 || params.iterations > 5 || params.parallelism < 1 || params.parallelism > 4 {
		return params, nil, nil, errors.New("Argon2id parameters outside Relay limits")
	}
	decode := base64.RawStdEncoding.DecodeString
	salt, err := decode(parts[4])
	if err != nil || len(salt) < 8 || len(salt) > 64 {
		return params, nil, nil, errors.New("invalid Argon2id salt")
	}
	expected, err := decode(parts[5])
	if err != nil || len(expected) < 16 || len(expected) > 64 {
		return params, nil, nil, errors.New("invalid Argon2id digest")
	}
	return params, salt, expected, nil
}

func ValidateArgon2id(encoded string) error {
	_, _, _, err := parseArgon2id(encoded)
	return err
}

func VerifyArgon2id(encoded, password string) (bool, error) {
	params, salt, expected, err := parseArgon2id(encoded)
	if err != nil {
		return false, err
	}
	actual := argon2.IDKey([]byte(password), salt, params.iterations, params.memory, params.parallelism, uint32(len(expected)))
	return subtle.ConstantTimeCompare(actual, expected) == 1, nil
}

func ParseBearer(value string) (string, bool) {
	if !strings.HasPrefix(value, "Bearer ") {
		return "", false
	}
	token := strings.TrimSpace(strings.TrimPrefix(value, "Bearer "))
	if token == "" || len(token) > 4096 || strings.ContainsAny(token, " \t\r\n") {
		return "", false
	}
	return token, true
}

func RetryAfterSeconds(untilUnix, nowUnix int64) string {
	remaining := untilUnix - nowUnix
	if remaining < 1 {
		remaining = 1
	}
	return strconv.FormatInt(remaining, 10)
}
