package server

import (
	"bytes"
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"net/url"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/VirtualBeingsResearch/CoWorker/apps/coworker-relay/internal/store"
)

func testServer(t *testing.T) (*Server, *store.Store) {
	t.Helper()
	database, err := store.Open(filepath.Join(t.TempDir(), "relay.db"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = database.Close() })
	return New(Config{
		PublicURL:  "https://relay.example.com",
		AdminToken: "administrator-token-long-enough",
	}, database, slog.New(slog.NewTextHandler(io.Discard, nil))), database
}

func TestAnonymousStatusDoesNotNeedTunnel(t *testing.T) {
	service, database := testServer(t)
	instance, _, err := database.CreateInstance("home")
	if err != nil {
		t.Fatal(err)
	}
	request := httptest.NewRequest(http.MethodGet, "/i/"+instance.ID+"/status", nil)
	request.RemoteAddr = "203.0.113.4:1234"
	response := httptest.NewRecorder()
	service.Handler().ServeHTTP(response, request)
	if response.Code != http.StatusOK || strings.TrimSpace(response.Body.String()) != "{}" {
		t.Fatalf("unexpected status response: %d %q", response.Code, response.Body.String())
	}
}

func TestReadinessTurnsUnavailableWhenDraining(t *testing.T) {
	service, _ := testServer(t)
	response := httptest.NewRecorder()
	service.Handler().ServeHTTP(
		response,
		httptest.NewRequest(http.MethodGet, "/_relay/v1/readyz", nil),
	)
	if response.Code != http.StatusOK {
		t.Fatalf("ready server returned %d", response.Code)
	}
	service.Drain()
	response = httptest.NewRecorder()
	service.Handler().ServeHTTP(
		response,
		httptest.NewRequest(http.MethodGet, "/_relay/v1/readyz", nil),
	)
	if response.Code != http.StatusServiceUnavailable {
		t.Fatalf("draining server returned %d", response.Code)
	}
}

func TestInstanceCanRotateItsOwnCredential(t *testing.T) {
	service, database := testServer(t)
	instance, pairing, err := database.CreateInstance("home")
	if err != nil {
		t.Fatal(err)
	}
	_, previous, err := database.Enroll(pairing, "$argon2id$test")
	if err != nil {
		t.Fatal(err)
	}
	request := httptest.NewRequest(
		http.MethodPost,
		"/_relay/v1/credential/rotate",
		nil,
	)
	request.Header.Set("Authorization", "Bearer "+previous)
	request.Header.Set("X-Coworker-Relay-Instance", instance.ID)
	response := httptest.NewRecorder()
	service.Handler().ServeHTTP(response, request)
	if response.Code != http.StatusOK {
		t.Fatalf("credential rotation returned %d: %s", response.Code, response.Body.String())
	}
	if _, err := database.AuthenticateInstance(instance.ID, previous); err != nil {
		t.Fatalf("old credential was invalidated before new credential login: %v", err)
	}
	var payload map[string]string
	if err := json.Unmarshal(response.Body.Bytes(), &payload); err != nil {
		t.Fatal(err)
	}
	if _, err := database.AuthenticateInstance(
		instance.ID,
		payload["instance_credential"],
	); err != nil {
		t.Fatalf("new credential could not promote: %v", err)
	}
	if _, err := database.AuthenticateInstance(instance.ID, previous); err == nil {
		t.Fatal("old instance credential remained valid after promotion")
	}
}

func TestUnknownAndManagementRoutesAreNotExposed(t *testing.T) {
	service, database := testServer(t)
	instance, _, err := database.CreateInstance("home")
	if err != nil {
		t.Fatal(err)
	}
	for _, path := range []string{"/api/admin/config", "/api/desktop-updates/releases", "/unknown"} {
		request := httptest.NewRequest(http.MethodGet, "/i/"+instance.ID+path, nil)
		response := httptest.NewRecorder()
		service.Handler().ServeHTTP(response, request)
		if response.Code != http.StatusNotFound {
			t.Fatalf("%s returned %d", path, response.Code)
		}
	}
}

func TestMissingBearerIsNotCountedAsPasswordFailure(t *testing.T) {
	service, database := testServer(t)
	instance, _, err := database.CreateInstance("home")
	if err != nil {
		t.Fatal(err)
	}
	for attempt := 0; attempt < 6; attempt++ {
		request := httptest.NewRequest(http.MethodPost, "/i/"+instance.ID+"/messages", nil)
		request.RemoteAddr = "203.0.113.4:1234"
		response := httptest.NewRecorder()
		service.Handler().ServeHTTP(response, request)
		if response.Code != http.StatusUnauthorized {
			t.Fatalf("attempt %d returned %d", attempt, response.Code)
		}
	}
	if _, active, _ := database.ActiveBan(instance.ID, "203.0.113.4", testNow()); active {
		t.Fatal("missing bearer caused a password ban")
	}
}

func TestSecurityLogDoesNotContainTokenOrRequestBody(t *testing.T) {
	database, err := store.Open(filepath.Join(t.TempDir(), "relay.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer database.Close()
	var logs bytes.Buffer
	service := New(
		Config{
			PublicURL: "https://relay.example.com", AdminToken: "administrator-token-long-enough",
		},
		database,
		slog.New(slog.NewJSONHandler(&logs, nil)),
	)
	instance, _, err := database.CreateInstance("home")
	if err != nil {
		t.Fatal(err)
	}
	request := httptest.NewRequest(
		http.MethodPost,
		"/i/"+instance.ID+"/messages",
		strings.NewReader("private-message-body"),
	)
	request.Header.Set("Authorization", "Bearer secret-that-must-not-be-logged")
	request.RemoteAddr = "203.0.113.4:1234"
	response := httptest.NewRecorder()
	service.Handler().ServeHTTP(response, request)

	if response.Code != http.StatusUnauthorized {
		t.Fatalf("unexpected response: %d", response.Code)
	}
	for _, secret := range []string{
		"secret-that-must-not-be-logged",
		"private-message-body",
		"Authorization",
	} {
		if strings.Contains(logs.String(), secret) {
			t.Fatalf("security log contains %q: %s", secret, logs.String())
		}
	}
}

func TestPublicURLMustBeAnHTTPSOrigin(t *testing.T) {
	for _, value := range []string{
		"http://relay.example.com",
		"https://relay.example.com/base",
		"https://user@relay.example.com",
	} {
		if _, err := ValidatePublicURL(value); err == nil {
			t.Fatalf("accepted invalid public URL %q", value)
		}
	}
	value, err := ValidatePublicURL("https://relay.example.com:8443/")
	if err != nil || value != "https://relay.example.com:8443" {
		t.Fatalf("unexpected normalized URL %q: %v", value, err)
	}
}

func TestPerInstanceSourceRequestLimit(t *testing.T) {
	service, _ := testServer(t)
	for request := 1; request <= 600; request++ {
		if !service.allowRequest("cw_test", "203.0.113.4") {
			t.Fatalf("request %d was limited too early", request)
		}
	}
	if service.allowRequest("cw_test", "203.0.113.4") {
		t.Fatal("request limit did not reject request 601")
	}
	if !service.allowRequest("cw_other", "203.0.113.4") {
		t.Fatal("request limit leaked across instances")
	}
}

func TestUpdateAssetCacheKeyIgnoresQueryParameters(t *testing.T) {
	first, err := url.Parse("/api/desktop-updates/assets/update.tar.gz?cache_bust=one")
	if err != nil {
		t.Fatal(err)
	}
	second, err := url.Parse("/api/desktop-updates/assets/update.tar.gz?cache_bust=two")
	if err != nil {
		t.Fatal(err)
	}
	if updateAssetCacheKey("cw_test", first) != updateAssetCacheKey("cw_test", second) {
		t.Fatal("query parameters created duplicate update cache entries")
	}
}

func testNow() (value time.Time) { return time.Now().UTC() }
