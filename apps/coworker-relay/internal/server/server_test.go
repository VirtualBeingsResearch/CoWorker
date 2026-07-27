package server

import (
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
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

func testNow() (value time.Time) { return time.Now().UTC() }
