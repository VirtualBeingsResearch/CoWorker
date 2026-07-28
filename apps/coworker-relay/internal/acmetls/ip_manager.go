package acmetls

import (
	"context"
	"crypto"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"encoding/json"
	"encoding/pem"
	"errors"
	"fmt"
	"log/slog"
	mathrand "math/rand/v2"
	"net"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/VirtualBeingsResearch/CoWorker/apps/coworker-relay/internal/netutil"
	"github.com/mholt/acmez/v3"
	"github.com/mholt/acmez/v3/acme"
)

const (
	defaultDirectoryURL  = "https://acme-v02.api.letsencrypt.org/directory"
	defaultCheckInterval = time.Hour
	defaultRetryInitial  = 30 * time.Second
	defaultRetryMaximum  = 30 * time.Minute
	shortLivedProfile    = "shortlived"
)

type IPConfig struct {
	Identifier   string
	Email        string
	CacheDir     string
	HTTPListen   string
	DirectoryURL string
	Logger       *slog.Logger
}

type IPManager struct {
	config          IPConfig
	checkInterval   time.Duration
	retryInitial    time.Duration
	retryMaximum    time.Duration
	now             func() time.Time
	certificate     atomic.Pointer[tls.Certificate]
	renewMu         sync.Mutex
	challengeMu     sync.RWMutex
	challenges      map[string]string
	challengeServer *http.Server
	issue           func(context.Context) ([]byte, []byte, error)
}

type storedAccount struct {
	DirectoryURL string       `json:"directory_url"`
	Account      acme.Account `json:"account"`
}

func NewIPManager(config IPConfig) (*IPManager, error) {
	ip := net.ParseIP(config.Identifier)
	if !netutil.IsPublicIP(ip) {
		return nil, errors.New("ACME IP identifier must be a public, globally routable IP address")
	}
	config.Identifier = ip.String()
	if config.CacheDir == "" {
		return nil, errors.New("ACME cache directory is required")
	}
	if config.HTTPListen == "" {
		config.HTTPListen = ":80"
	}
	if _, _, err := net.SplitHostPort(config.HTTPListen); err != nil {
		return nil, fmt.Errorf("invalid ACME HTTP listen address: %w", err)
	}
	if config.DirectoryURL == "" {
		config.DirectoryURL = defaultDirectoryURL
	}
	directory, err := url.Parse(config.DirectoryURL)
	if err != nil || directory.Scheme != "https" || directory.Host == "" {
		return nil, errors.New("ACME directory URL must be HTTPS")
	}
	if config.Logger == nil {
		config.Logger = slog.Default()
	}
	if err := os.MkdirAll(config.CacheDir, 0o700); err != nil {
		return nil, fmt.Errorf("create ACME cache: %w", err)
	}
	if err := os.Chmod(config.CacheDir, 0o700); err != nil {
		return nil, fmt.Errorf("secure ACME cache: %w", err)
	}

	manager := &IPManager{
		config:        config,
		checkInterval: defaultCheckInterval,
		retryInitial:  defaultRetryInitial,
		retryMaximum:  defaultRetryMaximum,
		now:           time.Now,
		challenges:    make(map[string]string),
	}
	manager.issue = manager.obtain
	loaded, loadErr := manager.loadCertificate()
	if loadErr != nil {
		return nil, loadErr
	}
	if loaded != nil {
		manager.certificate.Store(loaded)
	}
	return manager, nil
}

// StartHTTPChallenge binds the HTTP-01 listener before any order is created.
// The same listener remains available for later renewals.
func (m *IPManager) StartHTTPChallenge(ctx context.Context) error {
	listener, err := net.Listen("tcp", m.config.HTTPListen)
	if err != nil {
		return fmt.Errorf("listen for public-IP ACME HTTP-01 challenge: %w", err)
	}
	server := &http.Server{
		Addr:              m.config.HTTPListen,
		Handler:           m,
		ReadHeaderTimeout: 10 * time.Second,
	}
	m.challengeMu.Lock()
	if m.challengeServer != nil {
		m.challengeMu.Unlock()
		_ = listener.Close()
		return errors.New("public-IP ACME HTTP-01 listener is already running")
	}
	m.challengeServer = server
	m.challengeMu.Unlock()
	go func() {
		<-ctx.Done()
		shutdownContext, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		_ = server.Shutdown(shutdownContext)
	}()
	go func() {
		if serveErr := server.Serve(listener); serveErr != nil &&
			!errors.Is(serveErr, http.ErrServerClosed) {
			m.config.Logger.Error(
				"public-IP ACME HTTP-01 listener stopped",
				"error",
				serveErr,
			)
		}
	}()
	return nil
}

func (m *IPManager) ServeHTTP(response http.ResponseWriter, request *http.Request) {
	if request.Method != http.MethodGet || !m.matchesChallengeHost(request.Host) {
		http.NotFound(response, request)
		return
	}
	m.challengeMu.RLock()
	keyAuthorization, ok := m.challenges[request.URL.EscapedPath()]
	m.challengeMu.RUnlock()
	if !ok {
		http.NotFound(response, request)
		return
	}
	response.Header().Set("Content-Type", "text/plain")
	response.Header().Set("Cache-Control", "no-store")
	_, _ = response.Write([]byte(keyAuthorization))
}

func (m *IPManager) matchesChallengeHost(rawHost string) bool {
	host := rawHost
	if parsedHost, _, err := net.SplitHostPort(rawHost); err == nil {
		host = parsedHost
	}
	host = strings.Trim(host, "[]")
	requestIP := net.ParseIP(host)
	configuredIP := net.ParseIP(m.config.Identifier)
	return requestIP != nil && configuredIP != nil && requestIP.Equal(configuredIP)
}

func (m *IPManager) Present(
	_ context.Context,
	challenge acme.Challenge,
) error {
	if challenge.Type != acme.ChallengeTypeHTTP01 ||
		!identifierMatchesIP(challenge.Identifier, m.config.Identifier) ||
		challenge.Token == "" ||
		challenge.KeyAuthorization == "" {
		return errors.New("invalid public-IP ACME HTTP-01 challenge")
	}
	m.challengeMu.Lock()
	m.challenges[challenge.HTTP01ResourcePath()] = challenge.KeyAuthorization
	m.challengeMu.Unlock()
	return nil
}

func (m *IPManager) CleanUp(
	_ context.Context,
	challenge acme.Challenge,
) error {
	m.challengeMu.Lock()
	delete(m.challenges, challenge.HTTP01ResourcePath())
	m.challengeMu.Unlock()
	return nil
}

func identifierMatchesIP(identifier acme.Identifier, expected string) bool {
	if identifier.Type != "ip" {
		return false
	}
	actual := net.ParseIP(identifier.Value)
	configured := net.ParseIP(expected)
	return actual != nil && configured != nil && actual.Equal(configured)
}

func (m *IPManager) TLSConfig() *tls.Config {
	return &tls.Config{
		MinVersion:     tls.VersionTLS12,
		GetCertificate: m.GetCertificate,
	}
}

func (m *IPManager) GetCertificate(_ *tls.ClientHelloInfo) (*tls.Certificate, error) {
	current := m.certificate.Load()
	if current == nil {
		return nil, errors.New("public-IP ACME certificate is not available")
	}
	return current, nil
}

// EnsureCertificate obtains a certificate when none is available. If a cached
// certificate is still valid but due for renewal, Relay keeps serving it when
// the renewal attempt fails and the background loop retries later.
func (m *IPManager) EnsureCertificate(ctx context.Context) error {
	current := m.certificate.Load()
	if current != nil && validateCertificateTime(current, m.now()) == nil {
		if !certificateNeedsRenewal(current, m.now()) {
			return nil
		}
		if err := m.renew(ctx); err != nil {
			m.config.Logger.Warn(
				"public-IP ACME renewal failed; continuing with cached certificate",
				"identifier",
				m.config.Identifier,
				"error",
				err,
			)
		}
		return nil
	}
	return m.renew(ctx)
}

// WaitForCertificate keeps a Relay without a usable cache alive while ACME is
// unavailable. This prevents a container restart loop from rapidly repeating
// new-order attempts. HTTPS remains unavailable until a valid certificate has
// been installed.
func (m *IPManager) WaitForCertificate(ctx context.Context) error {
	delay := m.retryInitial
	for {
		if err := m.EnsureCertificate(ctx); err == nil {
			return nil
		} else {
			retryIn := jitteredDelay(delay)
			m.config.Logger.Warn(
				"public-IP ACME certificate unavailable; HTTPS is waiting",
				"identifier",
				m.config.Identifier,
				"retry_in",
				retryIn,
				"error",
				err,
			)
			timer := time.NewTimer(retryIn)
			select {
			case <-ctx.Done():
				if !timer.Stop() {
					select {
					case <-timer.C:
					default:
					}
				}
				return ctx.Err()
			case <-timer.C:
			}
			if delay < m.retryMaximum {
				delay *= 2
				if delay > m.retryMaximum {
					delay = m.retryMaximum
				}
			}
		}
	}
}

func (m *IPManager) Run(ctx context.Context) {
	ticker := time.NewTicker(m.checkInterval)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			current := m.certificate.Load()
			if current != nil &&
				validateCertificateTime(current, m.now()) == nil &&
				!certificateNeedsRenewal(current, m.now()) {
				continue
			}
			if err := m.renew(ctx); err != nil {
				m.config.Logger.Warn(
					"public-IP ACME renewal failed",
					"identifier",
					m.config.Identifier,
					"error",
					err,
				)
			}
		}
	}
}

func jitteredDelay(delay time.Duration) time.Duration {
	if delay <= 0 {
		return 0
	}
	window := delay / 2
	if window <= 0 {
		return delay
	}
	return delay*3/4 + time.Duration(mathrand.Int64N(int64(window)+1))
}

func (m *IPManager) renew(ctx context.Context) error {
	m.renewMu.Lock()
	defer m.renewMu.Unlock()

	current := m.certificate.Load()
	if current != nil &&
		validateCertificateTime(current, m.now()) == nil &&
		!certificateNeedsRenewal(current, m.now()) {
		return nil
	}
	certPEM, keyPEM, err := m.issue(ctx)
	if err != nil {
		return err
	}
	loaded, err := parseCertificate(certPEM, keyPEM, m.config.Identifier)
	if err != nil {
		return fmt.Errorf("validate issued public-IP certificate: %w", err)
	}
	if err := validateCertificateTime(loaded, m.now()); err != nil {
		return fmt.Errorf("validate issued public-IP certificate: %w", err)
	}
	bundle := make([]byte, 0, len(certPEM)+len(keyPEM))
	bundle = append(bundle, certPEM...)
	bundle = append(bundle, keyPEM...)
	if err := atomicWrite(
		filepath.Join(m.config.CacheDir, "ip-certificate.pem"),
		bundle,
		0o600,
	); err != nil {
		return fmt.Errorf("store public-IP certificate bundle: %w", err)
	}
	m.certificate.Store(loaded)
	m.config.Logger.Info(
		"public-IP ACME certificate ready",
		"identifier",
		m.config.Identifier,
		"not_after",
		loaded.Leaf.NotAfter.UTC(),
	)
	return nil
}

func (m *IPManager) obtain(ctx context.Context) ([]byte, []byte, error) {
	account, err := m.loadOrCreateAccount(ctx)
	if err != nil {
		return nil, nil, err
	}
	certificateKey, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		return nil, nil, fmt.Errorf("generate certificate key: %w", err)
	}
	parameters, err := m.orderParameters(account, certificateKey)
	if err != nil {
		return nil, nil, err
	}
	client := m.acmeClient()
	chains, err := client.ObtainCertificate(ctx, parameters)
	if err != nil {
		return nil, nil, fmt.Errorf("obtain public-IP ACME certificate: %w", err)
	}
	if len(chains) == 0 || len(chains[0].ChainPEM) == 0 {
		return nil, nil, errors.New("ACME server returned no certificate chain")
	}
	keyPEM, err := encodePrivateKey(certificateKey)
	if err != nil {
		return nil, nil, fmt.Errorf("encode certificate key: %w", err)
	}
	return chains[0].ChainPEM, keyPEM, nil
}

func (m *IPManager) orderParameters(
	account acme.Account,
	certificateKey crypto.Signer,
) (acmez.OrderParameters, error) {
	csr, err := acmez.NewCSR(certificateKey, []string{m.config.Identifier})
	if err != nil {
		return acmez.OrderParameters{}, fmt.Errorf("create public-IP certificate request: %w", err)
	}
	parameters, err := acmez.OrderParametersFromCSR(account, csr)
	if err != nil {
		return acmez.OrderParameters{}, fmt.Errorf("create public-IP ACME order: %w", err)
	}
	parameters.Profile = shortLivedProfile
	return parameters, nil
}

func (m *IPManager) acmeClient() *acmez.Client {
	return &acmez.Client{
		Client: &acme.Client{
			Directory: m.config.DirectoryURL,
			UserAgent: "coworker-relay",
			Logger:    m.config.Logger,
		},
		ChallengeSolvers: map[string]acmez.Solver{
			acme.ChallengeTypeHTTP01: m,
		},
	}
}

func (m *IPManager) loadCertificate() (*tls.Certificate, error) {
	bundle, err := os.ReadFile(filepath.Join(m.config.CacheDir, "ip-certificate.pem"))
	if errors.Is(err, os.ErrNotExist) {
		return nil, nil
	}
	if err != nil {
		return nil, fmt.Errorf("read cached public-IP ACME certificate: %w", err)
	}
	loaded, err := parseCertificate(bundle, bundle, m.config.Identifier)
	if err != nil {
		return nil, fmt.Errorf("load cached public-IP ACME certificate: %w", err)
	}
	return loaded, nil
}

func parseCertificate(
	certPEM []byte,
	keyPEM []byte,
	identifier string,
) (*tls.Certificate, error) {
	loaded, err := tls.X509KeyPair(certPEM, keyPEM)
	if err != nil {
		return nil, err
	}
	if len(loaded.Certificate) == 0 {
		return nil, errors.New("certificate chain is empty")
	}
	loaded.Leaf, err = x509.ParseCertificate(loaded.Certificate[0])
	if err != nil {
		return nil, err
	}
	if err := loaded.Leaf.VerifyHostname(identifier); err != nil {
		return nil, err
	}
	return &loaded, nil
}

func validateCertificateTime(loaded *tls.Certificate, now time.Time) error {
	if !now.Before(loaded.Leaf.NotAfter) {
		return errors.New("certificate is expired")
	}
	if now.Before(loaded.Leaf.NotBefore) {
		return errors.New("certificate is not valid yet")
	}
	return nil
}

func (m *IPManager) loadOrCreateAccount(ctx context.Context) (acme.Account, error) {
	privateKey, err := m.loadOrCreateAccountKey()
	if err != nil {
		return acme.Account{}, err
	}
	accountPath := filepath.Join(m.config.CacheDir, "ip-account.json")
	raw, readErr := os.ReadFile(accountPath)
	if readErr == nil {
		var stored storedAccount
		if err := json.Unmarshal(raw, &stored); err != nil {
			return acme.Account{}, fmt.Errorf("parse ACME account: %w", err)
		}
		if stored.DirectoryURL == m.config.DirectoryURL &&
			stored.Account.Location != "" &&
			stored.Account.Status == acme.StatusValid {
			stored.Account.PrivateKey = privateKey
			return stored.Account, nil
		}
	} else if !errors.Is(readErr, os.ErrNotExist) {
		return acme.Account{}, fmt.Errorf("read ACME account: %w", readErr)
	}

	contact := []string(nil)
	if email := strings.TrimSpace(m.config.Email); email != "" {
		contact = []string{"mailto:" + email}
	}
	account, err := m.acmeClient().Client.NewAccount(ctx, acme.Account{
		Contact:              contact,
		TermsOfServiceAgreed: true,
		PrivateKey:           privateKey,
	})
	if err != nil {
		return acme.Account{}, fmt.Errorf("register ACME account: %w", err)
	}
	if err := m.saveAccount(storedAccount{
		DirectoryURL: m.config.DirectoryURL,
		Account:      account,
	}); err != nil {
		return acme.Account{}, err
	}
	return account, nil
}

func (m *IPManager) loadOrCreateAccountKey() (crypto.Signer, error) {
	keyPath := filepath.Join(m.config.CacheDir, "ip-account.key")
	keyPEM, err := os.ReadFile(keyPath)
	switch {
	case err == nil:
		privateKey, parseErr := parsePrivateKey(keyPEM)
		if parseErr != nil {
			return nil, fmt.Errorf("parse ACME account key: %w", parseErr)
		}
		return privateKey, nil
	case errors.Is(err, os.ErrNotExist):
		privateKey, generateErr := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
		if generateErr != nil {
			return nil, fmt.Errorf("generate ACME account key: %w", generateErr)
		}
		encoded, encodeErr := encodePrivateKey(privateKey)
		if encodeErr != nil {
			return nil, fmt.Errorf("encode ACME account key: %w", encodeErr)
		}
		if err := atomicWrite(keyPath, encoded, 0o600); err != nil {
			return nil, fmt.Errorf("store ACME account key: %w", err)
		}
		return privateKey, nil
	default:
		return nil, fmt.Errorf("read ACME account key: %w", err)
	}
}

func parsePrivateKey(raw []byte) (crypto.Signer, error) {
	block, _ := pem.Decode(raw)
	if block == nil {
		return nil, errors.New("private key is not PEM")
	}
	parsed, err := x509.ParsePKCS8PrivateKey(block.Bytes)
	if err != nil {
		return nil, err
	}
	signer, ok := parsed.(crypto.Signer)
	if !ok {
		return nil, errors.New("private key does not implement crypto.Signer")
	}
	return signer, nil
}

func encodePrivateKey(privateKey crypto.Signer) ([]byte, error) {
	der, err := x509.MarshalPKCS8PrivateKey(privateKey)
	if err != nil {
		return nil, err
	}
	return pem.EncodeToMemory(&pem.Block{Type: "PRIVATE KEY", Bytes: der}), nil
}

func (m *IPManager) saveAccount(stored storedAccount) error {
	raw, err := json.MarshalIndent(stored, "", "  ")
	if err != nil {
		return fmt.Errorf("encode ACME account: %w", err)
	}
	raw = append(raw, '\n')
	if err := atomicWrite(
		filepath.Join(m.config.CacheDir, "ip-account.json"),
		raw,
		0o600,
	); err != nil {
		return fmt.Errorf("store ACME account: %w", err)
	}
	return nil
}

func certificateNeedsRenewal(certificate *tls.Certificate, now time.Time) bool {
	if certificate == nil || certificate.Leaf == nil {
		return true
	}
	if !now.Before(certificate.Leaf.NotAfter) {
		return true
	}
	validity := certificate.Leaf.NotAfter.Sub(certificate.Leaf.NotBefore)
	return !now.Before(certificate.Leaf.NotBefore.Add(validity * 2 / 3))
}

func atomicWrite(path string, body []byte, mode os.FileMode) (resultErr error) {
	file, err := os.CreateTemp(filepath.Dir(path), "."+filepath.Base(path)+"-*")
	if err != nil {
		return err
	}
	temporary := file.Name()
	defer func() {
		if resultErr != nil {
			_ = os.Remove(temporary)
		}
	}()
	if err := file.Chmod(mode); err != nil {
		_ = file.Close()
		return err
	}
	if _, err := file.Write(body); err != nil {
		_ = file.Close()
		return err
	}
	if err := file.Sync(); err != nil {
		_ = file.Close()
		return err
	}
	if err := file.Close(); err != nil {
		return err
	}
	return os.Rename(temporary, path)
}
