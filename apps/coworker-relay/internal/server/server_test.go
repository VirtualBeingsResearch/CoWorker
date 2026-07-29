package server

import (
	"crypto/ed25519"
	"crypto/hmac"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"net/netip"
	"path/filepath"
	"strings"
	"testing"

	"github.com/VirtualBeingsResearch/CoWorker/apps/coworker-relay/internal/store"
	"github.com/coder/websocket"
)

func testServer(t *testing.T) (*Server, *store.Store) {
	t.Helper()
	database, err := store.Open(filepath.Join(t.TempDir(), "relay.db"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = database.Close() })
	_, privateKey, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	return New(Config{
		PublicURL: "http://relay.test:8443", AdminToken: strings.Repeat("a", 32),
		RelayPrivateKey: privateKey,
	}, database, slog.New(slog.NewTextHandler(io.Discard, nil))), database
}

func TestPublicHandlerDoesNotExposeAdminOrPlaintextFacade(t *testing.T) {
	service, _ := testServer(t)
	for _, path := range []string{
		"/i/cw_abcdefgh/status",
		"/_relay/v1/admin/instances",
		"/_relay/v1/health",
	} {
		request := httptest.NewRequest(http.MethodGet, path, nil)
		response := httptest.NewRecorder()
		service.PublicHandler().ServeHTTP(response, request)
		if response.Code != http.StatusNotFound {
			t.Fatalf("%s returned %d", path, response.Code)
		}
	}
}

func TestPairingUsesChallengeHMACAndPinsRelayKey(t *testing.T) {
	service, database := testServer(t)
	_, pairingCode, err := database.CreateInstance("home")
	if err != nil {
		t.Fatal(err)
	}
	code := strings.TrimPrefix(pairingCode, "pair_")
	pairingID, secret, ok := strings.Cut(code, ".")
	if !ok {
		t.Fatal("invalid test pairing code")
	}
	public := httptest.NewServer(service.PublicHandler())
	defer public.Close()
	wsURL := "ws" + strings.TrimPrefix(public.URL, "http") + "/_relay/v1/pair"
	ctx := t.Context()
	connection, _, err := websocket.Dial(ctx, wsURL, nil)
	if err != nil {
		t.Fatal(err)
	}
	defer connection.Close(websocket.StatusNormalClosure, "")
	_, raw, err := connection.Read(ctx)
	if err != nil {
		t.Fatal(err)
	}
	var challenge wireMessage
	if err := json.Unmarshal(raw, &challenge); err != nil {
		t.Fatal(err)
	}
	instancePublic := base64.RawURLEncoding.EncodeToString(make([]byte, 32))
	mac := hmac.New(sha256.New, []byte(secret))
	_, _ = mac.Write([]byte(strings.Join([]string{
		"coworker-relay-v1", "pair", pairingID, challenge.Nonce, instancePublic,
	}, "\n")))
	proof, _ := json.Marshal(wireMessage{
		Type: "pair_proof", PairingID: pairingID,
		InstancePublicKey: instancePublic,
		Proof:             base64.RawURLEncoding.EncodeToString(mac.Sum(nil)),
	})
	if err := connection.Write(ctx, websocket.MessageText, proof); err != nil {
		t.Fatal(err)
	}
	_, raw, err = connection.Read(ctx)
	if err != nil {
		t.Fatal(err)
	}
	var result wireMessage
	if err := json.Unmarshal(raw, &result); err != nil {
		t.Fatal(err)
	}
	if result.Type != "pair_ok" || result.InstanceID == "" ||
		result.RelayPublicKey == "" || result.Signature == "" {
		t.Fatalf("unexpected pairing result: %#v", result)
	}
}

func TestSessionBindingUsesCurrentCoworkerControlConnection(t *testing.T) {
	service, database := testServer(t)
	instance, pairingCode, err := database.CreateInstance("home")
	if err != nil {
		t.Fatal(err)
	}
	_, instancePrivate, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	pairingID := strings.SplitN(strings.TrimPrefix(pairingCode, "pair_"), ".", 2)[0]
	if _, err := database.CompletePairing(
		pairingID,
		base64.RawURLEncoding.EncodeToString(instancePrivate.Public().(ed25519.PublicKey)),
	); err != nil {
		t.Fatal(err)
	}
	_, authPrivate, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	if err := database.UpdateAuthKey(
		instance.ID,
		base64.RawURLEncoding.EncodeToString(authPrivate.Public().(ed25519.PublicKey)),
		1,
	); err != nil {
		t.Fatal(err)
	}

	public := httptest.NewServer(service.PublicHandler())
	defer public.Close()
	ctx := t.Context()
	control, _, err := websocket.Dial(
		ctx,
		"ws"+strings.TrimPrefix(public.URL, "http")+
			"/_relay/v1/coworker?instance_id="+instance.ID,
		nil,
	)
	if err != nil {
		t.Fatal(err)
	}
	defer control.Close(websocket.StatusNormalClosure, "")
	var controlChallenge wireMessage
	_, raw, err := control.Read(ctx)
	if err != nil || json.Unmarshal(raw, &controlChallenge) != nil {
		t.Fatalf("read control challenge: %v", err)
	}
	controlPayload := challengePayload(
		"control", instance.ID, controlChallenge.ConnectionID,
		controlChallenge.Nonce, controlChallenge.Epoch, controlChallenge.ExpiresAt,
	)
	if err := writeText(ctx, control, wireMessage{
		Type: "control_proof", ConnectionID: controlChallenge.ConnectionID,
		Signature: sign(instancePrivate, controlPayload),
	}); err != nil {
		t.Fatal(err)
	}
	var ready wireMessage
	_, raw, err = control.Read(ctx)
	if err != nil || json.Unmarshal(raw, &ready) != nil || ready.Type != "control_ready" {
		t.Fatalf("read control ready: %#v %v", ready, err)
	}

	desktop, _, err := websocket.Dial(
		ctx,
		"ws"+strings.TrimPrefix(public.URL, "http")+
			"/i/"+instance.ID+"/_relay/v1/connect",
		nil,
	)
	if err != nil {
		t.Fatal(err)
	}
	defer desktop.Close(websocket.StatusNormalClosure, "")
	var desktopChallenge wireMessage
	_, raw, err = desktop.Read(ctx)
	if err != nil || json.Unmarshal(raw, &desktopChallenge) != nil {
		t.Fatalf("read desktop challenge: %v", err)
	}
	desktopPayload := challengePayload(
		"desktop", instance.ID, desktopChallenge.ConnectionID,
		desktopChallenge.Nonce, desktopChallenge.Epoch, desktopChallenge.ExpiresAt,
	)
	if err := writeText(ctx, desktop, wireMessage{
		Type: "auth_proof", ConnectionID: desktopChallenge.ConnectionID,
		Signature: sign(authPrivate, desktopPayload),
	}); err != nil {
		t.Fatal(err)
	}
	var opened wireMessage
	_, raw, err = control.Read(ctx)
	if err != nil || json.Unmarshal(raw, &opened) != nil || opened.Type != "session_open" {
		t.Fatalf("read session open: %#v %v", opened, err)
	}
	if opened.ConnectionID != controlChallenge.ConnectionID {
		t.Fatalf(
			"session bound to %q instead of control %q",
			opened.ConnectionID, controlChallenge.ConnectionID,
		)
	}
	if opened.ConnectionID == desktopChallenge.ConnectionID {
		t.Fatal("session was incorrectly bound to the Desktop authentication connection")
	}
}

func TestValidatePublicURLAllowsHTTPWithoutCredentials(t *testing.T) {
	if value, err := ValidatePublicURL("http://203.0.113.10:8443/"); err != nil ||
		value != "http://203.0.113.10:8443" {
		t.Fatalf("HTTP Relay origin rejected: %q %v", value, err)
	}
	for _, value := range []string{"ftp://relay.test", "http://user@relay.test", "http://relay.test/path"} {
		if _, err := ValidatePublicURL(value); err == nil {
			t.Fatalf("invalid origin accepted: %s", value)
		}
	}
}

func TestClientIPRejectsSpoofedForwardingHeaders(t *testing.T) {
	service, _ := testServer(t)
	request := httptest.NewRequest(http.MethodGet, "/healthz", nil)
	request.RemoteAddr = "198.51.100.8:1234"
	request.Header.Set("X-Forwarded-For", "203.0.113.9")
	if got := service.clientIP(request); got != "198.51.100.8" {
		t.Fatalf("untrusted proxy header changed source IP: %s", got)
	}

	prefix, err := netip.ParsePrefix("192.0.2.0/24")
	if err != nil {
		t.Fatal(err)
	}
	service.config.TrustedProxies = []netip.Prefix{prefix}
	request.RemoteAddr = "192.0.2.10:1234"
	request.Header.Set(
		"X-Forwarded-For",
		"198.51.100.99, 203.0.113.20, 192.0.2.11",
	)
	if got := service.clientIP(request); got != "203.0.113.20" {
		t.Fatalf("did not select rightmost untrusted hop: %s", got)
	}
}
