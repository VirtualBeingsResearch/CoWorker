package server

import "testing"

func FuzzValidatePublicURL(f *testing.F) {
	f.Add("http://127.0.0.1:8443")
	f.Add("https://relay.example.com")
	f.Add("http://user:secret@relay.example.com")
	f.Fuzz(func(_ *testing.T, value string) {
		_, _ = ValidatePublicURL(value)
	})
}
