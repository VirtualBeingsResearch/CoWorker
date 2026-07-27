package server

import "testing"

func FuzzSplitInstancePath(f *testing.F) {
	for _, seed := range []string{
		"/i/cw_abcdefgh/status",
		"/i/cw_abcdefgh/messages",
		"/i/cw_abcdefgh/../api/admin/config",
		"/i//status",
		"/unknown",
	} {
		f.Add(seed)
	}
	f.Fuzz(func(t *testing.T, value string) {
		instance, path, ok := splitInstancePath(value)
		if ok {
			if instance == "" || path == "" || path[0] != '/' {
				t.Fatalf("accepted invalid split: %q %q from %q", instance, path, value)
			}
		}
	})
}
