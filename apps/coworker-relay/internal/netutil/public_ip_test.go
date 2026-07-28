package netutil

import (
	"net"
	"testing"
)

func TestIsPublicIP(t *testing.T) {
	t.Parallel()
	tests := map[string]bool{
		"221.228.203.18":  true,
		"8.8.8.8":         true,
		"2001:4860::8888": true,
		"10.0.0.1":        false,
		"100.64.0.1":      false,
		"127.0.0.1":       false,
		"192.0.2.10":      false,
		"198.18.0.1":      false,
		"198.51.100.1":    false,
		"203.0.113.1":     false,
		"224.0.0.1":       false,
		"::1":             false,
		"2001:db8::1":     false,
		"3fff::1":         false,
		"fc00::1":         false,
		"fe80::1":         false,
	}
	for value, expected := range tests {
		value, expected := value, expected
		t.Run(value, func(t *testing.T) {
			t.Parallel()
			if actual := IsPublicIP(net.ParseIP(value)); actual != expected {
				t.Fatalf("IsPublicIP(%q) = %t, want %t", value, actual, expected)
			}
		})
	}
}
