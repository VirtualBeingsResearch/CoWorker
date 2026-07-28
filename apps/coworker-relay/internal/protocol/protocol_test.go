package protocol

import (
	"encoding/json"
	"os"
	"testing"
)

func TestRequestV1GoldenFixture(t *testing.T) {
	raw, err := os.ReadFile("testdata/request-v1.json")
	if err != nil {
		t.Fatal(err)
	}
	var message Message
	if err := json.Unmarshal(raw, &message); err != nil {
		t.Fatal(err)
	}
	if message.Type != "request" ||
		message.RequestID != "req_fixture" ||
		message.RelayHeaderStart != 2 ||
		len(message.Headers) != 8 ||
		message.Headers[0][1] != "client-value" ||
		message.Headers[2][1] != "v1" ||
		message.Headers[7][0] != "Forwarded" {
		t.Fatalf("unexpected fixture: %#v", message)
	}
}
