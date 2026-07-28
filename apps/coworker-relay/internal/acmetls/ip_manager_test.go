package acmetls

import (
	"context"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/pem"
	"errors"
	"io"
	"log/slog"
	"math/big"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/mholt/acmez/v3/acme"
)

const testPublicIP = "221.228.203.18"

func TestIPManagerLoadsCacheAndServesWithoutSNI(t *testing.T) {
	t.Parallel()
	now := time.Now().UTC().Truncate(time.Second)
	certPEM, keyPEM, root := makeIPCertificate(
		t,
		testPublicIP,
		now.Add(-time.Hour),
		now.Add(72*time.Hour),
	)
	cache := t.TempDir()
	writeCertificatePair(t, cache, certPEM, keyPEM)

	manager, err := NewIPManager(IPConfig{
		Identifier: testPublicIP,
		CacheDir:   cache,
		HTTPListen: ":8080",
		Logger:     slog.New(slog.NewTextHandler(io.Discard, nil)),
	})
	if err != nil {
		t.Fatal(err)
	}
	if err := manager.EnsureCertificate(context.Background()); err != nil {
		t.Fatalf("valid cache unexpectedly caused issuance: %v", err)
	}

	serverConfig := manager.TLSConfig()
	originalGetCertificate := serverConfig.GetCertificate
	serverConfig.GetCertificate = func(hello *tls.ClientHelloInfo) (*tls.Certificate, error) {
		if hello.ServerName != "" {
			t.Errorf("IP client unexpectedly sent SNI %q", hello.ServerName)
		}
		return originalGetCertificate(hello)
	}
	listener, err := tls.Listen("tcp", "127.0.0.1:0", serverConfig)
	if err != nil {
		t.Fatal(err)
	}
	defer listener.Close()
	serverDone := make(chan error, 1)
	go func() {
		connection, acceptErr := listener.Accept()
		if acceptErr != nil {
			serverDone <- acceptErr
			return
		}
		defer connection.Close()
		serverDone <- connection.(*tls.Conn).Handshake()
	}()

	roots := x509.NewCertPool()
	roots.AddCert(root)
	connection, err := tls.Dial("tcp", listener.Addr().String(), &tls.Config{
		MinVersion: tls.VersionTLS12,
		RootCAs:    roots,
		ServerName: testPublicIP,
	})
	if err != nil {
		t.Fatal(err)
	}
	_ = connection.Close()
	if err := <-serverDone; err != nil {
		t.Fatal(err)
	}
}

func TestIPManagerUsesShortLivedIPOrder(t *testing.T) {
	t.Parallel()
	manager, err := NewIPManager(IPConfig{
		Identifier: testPublicIP,
		CacheDir:   t.TempDir(),
		HTTPListen: ":8080",
		Logger:     slog.New(slog.NewTextHandler(io.Discard, nil)),
	})
	if err != nil {
		t.Fatal(err)
	}
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	request, err := manager.orderParameters(acme.Account{
		Status: acme.StatusValid,
	}, key)
	if err != nil {
		t.Fatal(err)
	}
	if len(request.Identifiers) != 1 ||
		request.Identifiers[0].Type != "ip" ||
		request.Identifiers[0].Value != testPublicIP {
		t.Fatalf("unexpected identifiers: %#v", request.Identifiers)
	}
	if request.Profile != shortLivedProfile {
		t.Fatalf("unexpected ACME order: %#v", request)
	}
}

func TestIPManagerServesOnlyPresentedChallengeForConfiguredIP(t *testing.T) {
	t.Parallel()
	manager, err := NewIPManager(IPConfig{
		Identifier: testPublicIP,
		CacheDir:   t.TempDir(),
		HTTPListen: ":8080",
		Logger:     slog.New(slog.NewTextHandler(io.Discard, nil)),
	})
	if err != nil {
		t.Fatal(err)
	}
	challenge := acme.Challenge{
		Type:             acme.ChallengeTypeHTTP01,
		Token:            "test-token",
		KeyAuthorization: "test-token.thumbprint",
		Identifier:       acme.Identifier{Type: "ip", Value: testPublicIP},
	}
	if err := manager.Present(context.Background(), challenge); err != nil {
		t.Fatal(err)
	}

	request := httptest.NewRequest(
		http.MethodGet,
		"http://"+testPublicIP+challenge.HTTP01ResourcePath(),
		nil,
	)
	response := httptest.NewRecorder()
	manager.ServeHTTP(response, request)
	if response.Code != http.StatusOK ||
		strings.TrimSpace(response.Body.String()) != challenge.KeyAuthorization {
		t.Fatalf("unexpected challenge response: %d %q", response.Code, response.Body.String())
	}

	wrongHost := httptest.NewRequest(
		http.MethodGet,
		"http://203.0.113.1"+challenge.HTTP01ResourcePath(),
		nil,
	)
	wrongHostResponse := httptest.NewRecorder()
	manager.ServeHTTP(wrongHostResponse, wrongHost)
	if wrongHostResponse.Code != http.StatusNotFound {
		t.Fatalf("wrong host response = %d", wrongHostResponse.Code)
	}

	if err := manager.CleanUp(context.Background(), challenge); err != nil {
		t.Fatal(err)
	}
	cleanedResponse := httptest.NewRecorder()
	manager.ServeHTTP(cleanedResponse, request)
	if cleanedResponse.Code != http.StatusNotFound {
		t.Fatalf("cleaned challenge response = %d", cleanedResponse.Code)
	}
}

func TestIPManagerRenewalFailureKeepsValidCertificate(t *testing.T) {
	t.Parallel()
	now := time.Now().UTC()
	certPEM, keyPEM, _ := makeIPCertificate(
		t,
		testPublicIP,
		now.Add(-48*time.Hour),
		now.Add(12*time.Hour),
	)
	cache := t.TempDir()
	writeCertificatePair(t, cache, certPEM, keyPEM)
	manager, err := NewIPManager(IPConfig{
		Identifier: testPublicIP,
		CacheDir:   cache,
		HTTPListen: ":8080",
		Logger:     slog.New(slog.NewTextHandler(io.Discard, nil)),
	})
	if err != nil {
		t.Fatal(err)
	}
	before, err := manager.GetCertificate(nil)
	if err != nil {
		t.Fatal(err)
	}
	manager.issue = func(context.Context) ([]byte, []byte, error) {
		return nil, nil, errors.New("issuer unavailable")
	}
	if err := manager.EnsureCertificate(context.Background()); err != nil {
		t.Fatalf("renewal failure should keep an unexpired certificate: %v", err)
	}
	after, err := manager.GetCertificate(nil)
	if err != nil {
		t.Fatal(err)
	}
	if before != after {
		t.Fatal("failed renewal replaced the cached certificate")
	}
}

func TestIPManagerRenewalAtomicallySwitchesAndPersistsCertificate(t *testing.T) {
	t.Parallel()
	now := time.Now().UTC().Truncate(time.Second)
	oldCert, oldKey, _ := makeIPCertificate(
		t,
		testPublicIP,
		now.Add(-48*time.Hour),
		now.Add(12*time.Hour),
	)
	newNotAfter := now.Add(160 * time.Hour)
	newCert, newKey, _ := makeIPCertificate(
		t,
		testPublicIP,
		now.Add(-time.Hour),
		newNotAfter,
	)
	cache := t.TempDir()
	writeCertificatePair(t, cache, oldCert, oldKey)
	manager, err := NewIPManager(IPConfig{
		Identifier: testPublicIP,
		CacheDir:   cache,
		HTTPListen: ":8080",
		Logger:     slog.New(slog.NewTextHandler(io.Discard, nil)),
	})
	if err != nil {
		t.Fatal(err)
	}
	before, err := manager.GetCertificate(nil)
	if err != nil {
		t.Fatal(err)
	}
	manager.issue = func(context.Context) ([]byte, []byte, error) {
		return newCert, newKey, nil
	}
	if err := manager.EnsureCertificate(context.Background()); err != nil {
		t.Fatal(err)
	}
	after, err := manager.GetCertificate(nil)
	if err != nil {
		t.Fatal(err)
	}
	if before == after || !after.Leaf.NotAfter.Equal(newNotAfter) {
		t.Fatal("successful renewal did not switch the in-memory certificate")
	}
	info, err := os.Stat(filepath.Join(cache, "ip-certificate.pem"))
	if err != nil {
		t.Fatal(err)
	}
	if info.Mode().Perm() != 0o600 {
		t.Fatalf("certificate bundle mode is %o", info.Mode().Perm())
	}
	restarted, err := NewIPManager(IPConfig{
		Identifier: testPublicIP,
		CacheDir:   cache,
		HTTPListen: ":8080",
		Logger:     slog.New(slog.NewTextHandler(io.Discard, nil)),
	})
	if err != nil {
		t.Fatal(err)
	}
	reloaded, err := restarted.GetCertificate(nil)
	if err != nil {
		t.Fatal(err)
	}
	if !reloaded.Leaf.NotAfter.Equal(newNotAfter) {
		t.Fatal("renewed certificate was not loaded after restart")
	}
}

func TestIPManagerInitialIssuanceFailureIsFatal(t *testing.T) {
	t.Parallel()
	manager, err := NewIPManager(IPConfig{
		Identifier: testPublicIP,
		CacheDir:   t.TempDir(),
		HTTPListen: ":8080",
		Logger:     slog.New(slog.NewTextHandler(io.Discard, nil)),
	})
	if err != nil {
		t.Fatal(err)
	}
	manager.issue = func(context.Context) ([]byte, []byte, error) {
		return nil, nil, errors.New("issuer unavailable")
	}
	if err := manager.EnsureCertificate(context.Background()); err == nil {
		t.Fatal("initial issuance failure was ignored")
	}
}

func TestIPManagerReplacesCachedCertificateThatIsNotYetValid(t *testing.T) {
	t.Parallel()
	now := time.Now().UTC().Truncate(time.Second)
	futureCert, futureKey, _ := makeIPCertificate(
		t,
		testPublicIP,
		now.Add(time.Hour),
		now.Add(161*time.Hour),
	)
	validCert, validKey, _ := makeIPCertificate(
		t,
		testPublicIP,
		now.Add(-time.Hour),
		now.Add(159*time.Hour),
	)
	cache := t.TempDir()
	writeCertificatePair(t, cache, futureCert, futureKey)
	manager, err := NewIPManager(IPConfig{
		Identifier: testPublicIP,
		CacheDir:   cache,
		HTTPListen: ":8080",
		Logger:     slog.New(slog.NewTextHandler(io.Discard, nil)),
	})
	if err != nil {
		t.Fatal(err)
	}
	manager.issue = func(context.Context) ([]byte, []byte, error) {
		return validCert, validKey, nil
	}
	if err := manager.EnsureCertificate(context.Background()); err != nil {
		t.Fatal(err)
	}
	current, err := manager.GetCertificate(nil)
	if err != nil {
		t.Fatal(err)
	}
	if current.Leaf.NotBefore.After(now) {
		t.Fatal("future-dated cached certificate was retained")
	}
}

func TestIPManagerWaitRetriesInitialIssuanceInProcess(t *testing.T) {
	t.Parallel()
	now := time.Now().UTC()
	certPEM, keyPEM, _ := makeIPCertificate(
		t,
		testPublicIP,
		now.Add(-time.Hour),
		now.Add(160*time.Hour),
	)
	manager, err := NewIPManager(IPConfig{
		Identifier: testPublicIP,
		CacheDir:   t.TempDir(),
		HTTPListen: ":8080",
		Logger:     slog.New(slog.NewTextHandler(io.Discard, nil)),
	})
	if err != nil {
		t.Fatal(err)
	}
	manager.retryInitial = time.Millisecond
	manager.retryMaximum = 2 * time.Millisecond
	attempts := 0
	manager.issue = func(context.Context) ([]byte, []byte, error) {
		attempts++
		if attempts < 3 {
			return nil, nil, errors.New("issuer unavailable")
		}
		return certPEM, keyPEM, nil
	}
	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	if err := manager.WaitForCertificate(ctx); err != nil {
		t.Fatal(err)
	}
	if attempts != 3 {
		t.Fatalf("issuance attempts = %d, want 3", attempts)
	}
}

func TestIPManagerRejectsUnsafeConfiguration(t *testing.T) {
	t.Parallel()
	tests := []IPConfig{
		{Identifier: "192.0.2.10", CacheDir: t.TempDir(), HTTPListen: ":8080"},
		{Identifier: testPublicIP, CacheDir: t.TempDir(), HTTPListen: "8080"},
		{
			Identifier:   testPublicIP,
			CacheDir:     t.TempDir(),
			HTTPListen:   ":8080",
			DirectoryURL: "http://acme.example.test/directory",
		},
	}
	for index, config := range tests {
		if _, err := NewIPManager(config); err == nil {
			t.Fatalf("unsafe configuration %d was accepted", index)
		}
	}
}

func TestJitteredDelayStaysWithinBounds(t *testing.T) {
	t.Parallel()
	const delay = time.Minute
	for range 100 {
		actual := jitteredDelay(delay)
		if actual < delay*3/4 || actual > delay*5/4 {
			t.Fatalf("jittered delay %s is outside expected bounds", actual)
		}
	}
}

func TestCertificateNeedsRenewalAfterTwoThirdsOfLifetime(t *testing.T) {
	t.Parallel()
	notBefore := time.Date(2026, 7, 1, 0, 0, 0, 0, time.UTC)
	certificate := &tls.Certificate{Leaf: &x509.Certificate{
		NotBefore: notBefore,
		NotAfter:  notBefore.Add(6 * 24 * time.Hour),
	}}
	if certificateNeedsRenewal(certificate, notBefore.Add(95*time.Hour)) {
		t.Fatal("certificate was renewed before two thirds of its lifetime")
	}
	if !certificateNeedsRenewal(certificate, notBefore.Add(96*time.Hour)) {
		t.Fatal("certificate was not renewed at two thirds of its lifetime")
	}
}

func makeIPCertificate(
	t *testing.T,
	ip string,
	notBefore time.Time,
	notAfter time.Time,
) ([]byte, []byte, *x509.Certificate) {
	t.Helper()
	privateKey, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	template := &x509.Certificate{
		SerialNumber:          big.NewInt(1),
		Subject:               pkix.Name{CommonName: ip},
		NotBefore:             notBefore,
		NotAfter:              notAfter,
		KeyUsage:              x509.KeyUsageDigitalSignature | x509.KeyUsageCertSign,
		ExtKeyUsage:           []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth},
		BasicConstraintsValid: true,
		IsCA:                  true,
		IPAddresses:           []net.IP{net.ParseIP(ip)},
	}
	der, err := x509.CreateCertificate(rand.Reader, template, template, &privateKey.PublicKey, privateKey)
	if err != nil {
		t.Fatal(err)
	}
	key, err := x509.MarshalECPrivateKey(privateKey)
	if err != nil {
		t.Fatal(err)
	}
	root, err := x509.ParseCertificate(der)
	if err != nil {
		t.Fatal(err)
	}
	return pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der}),
		pem.EncodeToMemory(&pem.Block{Type: "EC PRIVATE KEY", Bytes: key}),
		root
}

func writeCertificatePair(t *testing.T, directory string, certPEM []byte, keyPEM []byte) {
	t.Helper()
	bundle := append(append([]byte{}, certPEM...), keyPEM...)
	if err := os.WriteFile(
		filepath.Join(directory, "ip-certificate.pem"),
		bundle,
		0o600,
	); err != nil {
		t.Fatal(err)
	}
}
