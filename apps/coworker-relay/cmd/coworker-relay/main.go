package main

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"net"
	"net/http"
	"net/url"
	"os"
	"os/signal"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"time"

	"github.com/VirtualBeingsResearch/CoWorker/apps/coworker-relay/internal/acmetls"
	"github.com/VirtualBeingsResearch/CoWorker/apps/coworker-relay/internal/buildinfo"
	relaycache "github.com/VirtualBeingsResearch/CoWorker/apps/coworker-relay/internal/cache"
	"github.com/VirtualBeingsResearch/CoWorker/apps/coworker-relay/internal/server"
	"github.com/VirtualBeingsResearch/CoWorker/apps/coworker-relay/internal/store"
	"golang.org/x/crypto/acme/autocert"
)

func main() {
	args := os.Args[1:]
	if len(args) == 1 && args[0] == "--version" {
		args[0] = "version"
	}
	if handled, err := handleHelp(args, os.Stdout); handled {
		if err != nil {
			fatal(err.Error())
		}
		return
	}
	if args[0] != "serve" {
		runCLI(args)
		return
	}
	if len(args) != 1 {
		usage()
	}
	if err := loadLocalEnvironment(); err != nil {
		fatal(err.Error())
	}
	serveRelay()
}

func serveRelay() {
	logger := slog.New(slog.NewJSONHandler(os.Stdout, nil))
	logger.Info("coworker relay build", "build", buildinfo.Values())
	publicURL, err := server.ValidatePublicURL(os.Getenv("RELAY_PUBLIC_URL"))
	if err != nil {
		logger.Error("invalid relay configuration", "error", err)
		os.Exit(1)
	}
	adminToken, err := secretEnvironment("RELAY_ADMIN_TOKEN")
	if err != nil {
		logger.Error("unable to read Relay administrator token", "error", err)
		os.Exit(1)
	}
	if len(adminToken) < 24 {
		logger.Error("RELAY_ADMIN_TOKEN must contain at least 24 characters")
		os.Exit(1)
	}
	trusted, err := server.ParseTrustedProxies(os.Getenv("RELAY_TRUSTED_PROXY_CIDRS"))
	if err != nil {
		logger.Error("invalid trusted proxy configuration", "error", err)
		os.Exit(1)
	}
	databasePath := env("RELAY_DATABASE", "data/coworker-relay.db")
	dataDirectory := filepath.Dir(databasePath)
	if err := os.MkdirAll(dataDirectory, 0o700); err != nil {
		logger.Error("unable to create relay data directory", "error", err)
		os.Exit(1)
	}
	database, err := store.Open(databasePath)
	if err != nil {
		logger.Error("unable to open relay database", "error", err)
		os.Exit(1)
	}
	defer database.Close()
	failureLimit, err := envInteger("RELAY_BAN_FAILURE_LIMIT", 5, 1, 100)
	if err != nil {
		logger.Error("invalid RELAY_BAN_FAILURE_LIMIT", "error", err)
		os.Exit(1)
	}
	failureWindow, err := envDuration("RELAY_BAN_FAILURE_WINDOW", 10*time.Minute)
	if err != nil {
		logger.Error("invalid RELAY_BAN_FAILURE_WINDOW", "error", err)
		os.Exit(1)
	}
	banDuration, err := envDuration("RELAY_BAN_DURATION", time.Hour)
	if err != nil {
		logger.Error("invalid RELAY_BAN_DURATION", "error", err)
		os.Exit(1)
	}
	if err := database.SetAuthPolicy(failureWindow, failureLimit, banDuration); err != nil {
		logger.Error("invalid Relay authentication policy", "error", err)
		os.Exit(1)
	}
	parallel, err := strconv.Atoi(env("RELAY_VERIFIER_CONCURRENCY", "4"))
	if err != nil || parallel < 1 || parallel > 64 {
		logger.Error("RELAY_VERIFIER_CONCURRENCY must be between 1 and 64")
		os.Exit(1)
	}
	cacheBytes, err := strconv.ParseInt(env("RELAY_CACHE_MAX_BYTES", "4294967296"), 10, 64)
	if err != nil || cacheBytes < 1 {
		logger.Error("RELAY_CACHE_MAX_BYTES must be a positive integer")
		os.Exit(1)
	}
	assetCache, err := relaycache.New(
		env("RELAY_CACHE_DIR", filepath.Join(dataDirectory, "cache")),
		cacheBytes,
	)
	if err != nil {
		logger.Error("unable to initialize relay cache", "error", err)
		os.Exit(1)
	}
	requestLimit, err := envInteger("RELAY_REQUESTS_PER_MINUTE", 600, 1, 1_000_000)
	if err != nil {
		logger.Error("invalid RELAY_REQUESTS_PER_MINUTE", "error", err)
		os.Exit(1)
	}
	anonymousLimit, err := envInteger("RELAY_ANONYMOUS_PER_MINUTE", 60, 1, 1_000_000)
	if err != nil {
		logger.Error("invalid RELAY_ANONYMOUS_PER_MINUTE", "error", err)
		os.Exit(1)
	}
	maxRequestBody, err := envInt64(
		"RELAY_MAX_REQUEST_BODY_BYTES",
		32<<20,
		1,
		32<<20,
	)
	if err != nil {
		logger.Error("invalid RELAY_MAX_REQUEST_BODY_BYTES", "error", err)
		os.Exit(1)
	}
	maxTunnelFrame, err := envInt64(
		"RELAY_MAX_TUNNEL_FRAME_BYTES",
		48<<20,
		1,
		48<<20,
	)
	if err != nil {
		logger.Error("invalid RELAY_MAX_TUNNEL_FRAME_BYTES", "error", err)
		os.Exit(1)
	}
	minimumFrame := ((maxRequestBody + 2) / 3 * 4) + (64 << 10)
	if maxTunnelFrame < minimumFrame {
		logger.Error(
			"RELAY_MAX_TUNNEL_FRAME_BYTES is too small for RELAY_MAX_REQUEST_BODY_BYTES",
			"minimum", minimumFrame,
		)
		os.Exit(1)
	}
	relayServer := server.New(server.Config{
		PublicURL: publicURL, AdminToken: adminToken, TrustedProxies: trusted,
		VerifierParallel: parallel, Cache: assetCache,
		RequestLimit: requestLimit, AnonymousLimit: anonymousLimit,
		MaxRequestBody: maxRequestBody, MaxTunnelFrame: maxTunnelFrame,
	}, database, logger)
	httpServer := &http.Server{
		Addr:              env("RELAY_LISTEN", ":8443"),
		Handler:           relayServer.Handler(),
		ReadHeaderTimeout: 10 * time.Second,
		IdleTimeout:       90 * time.Second,
		MaxHeaderBytes:    32 << 10,
	}
	cert, key := os.Getenv("RELAY_TLS_CERT"), os.Getenv("RELAY_TLS_KEY")
	logger.Info("coworker relay starting", "listen", httpServer.Addr, "public_url", publicURL)
	var challengeServer *http.Server
	runContext, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	serve := func() error {
		if cert != "" || key != "" {
			if cert == "" || key == "" {
				return errors.New("RELAY_TLS_CERT and RELAY_TLS_KEY must be provided together")
			}
			return httpServer.ListenAndServeTLS(cert, key)
		}
		domain := strings.TrimSpace(os.Getenv("RELAY_ACME_DOMAIN"))
		ipIdentifier := strings.TrimSpace(os.Getenv("RELAY_ACME_IP"))
		if domain != "" && ipIdentifier != "" {
			return errors.New("configure only one of RELAY_ACME_DOMAIN and RELAY_ACME_IP")
		}
		if domain == "" && ipIdentifier == "" {
			return errors.New(
				"configure RELAY_TLS_CERT/RELAY_TLS_KEY, RELAY_ACME_DOMAIN, or RELAY_ACME_IP",
			)
		}
		parsedPublicURL, parseErr := url.Parse(publicURL)
		if parseErr != nil {
			return errors.New("unable to parse RELAY_PUBLIC_URL for ACME")
		}
		if ipIdentifier != "" {
			configuredIP := net.ParseIP(ipIdentifier)
			publicIP := net.ParseIP(parsedPublicURL.Hostname())
			if configuredIP == nil {
				return errors.New("RELAY_ACME_IP must be an IP address")
			}
			if publicIP == nil || !configuredIP.Equal(publicIP) {
				return errors.New("RELAY_ACME_IP must match RELAY_PUBLIC_URL hostname")
			}
			manager, managerErr := acmetls.NewIPManager(acmetls.IPConfig{
				Identifier:   ipIdentifier,
				Email:        os.Getenv("RELAY_ACME_EMAIL"),
				CacheDir:     env("RELAY_ACME_CACHE", filepath.Join(dataDirectory, "acme")),
				HTTPListen:   env("RELAY_ACME_HTTP_LISTEN", ":80"),
				DirectoryURL: os.Getenv("RELAY_ACME_DIRECTORY_URL"),
				Logger:       logger,
			})
			if managerErr != nil {
				return managerErr
			}
			if managerErr = manager.StartHTTPChallenge(runContext); managerErr != nil {
				return managerErr
			}
			if managerErr = manager.WaitForCertificate(runContext); managerErr != nil {
				return managerErr
			}
			httpServer.TLSConfig = manager.TLSConfig()
			go manager.Run(runContext)
			return httpServer.ListenAndServeTLS("", "")
		}
		if net.ParseIP(domain) != nil {
			return errors.New("use RELAY_ACME_IP, not RELAY_ACME_DOMAIN, for an IP address")
		}
		if !strings.EqualFold(parsedPublicURL.Hostname(), domain) {
			return errors.New("RELAY_ACME_DOMAIN must match RELAY_PUBLIC_URL hostname")
		}
		manager := &autocert.Manager{
			Prompt:     autocert.AcceptTOS,
			Email:      os.Getenv("RELAY_ACME_EMAIL"),
			HostPolicy: autocert.HostWhitelist(domain),
			Cache:      autocert.DirCache(env("RELAY_ACME_CACHE", filepath.Join(dataDirectory, "acme"))),
		}
		httpServer.TLSConfig = manager.TLSConfig()
		challengeServer = &http.Server{
			Addr:              env("RELAY_ACME_HTTP_LISTEN", ":80"),
			Handler:           manager.HTTPHandler(nil),
			ReadHeaderTimeout: 10 * time.Second,
		}
		go func() {
			if challengeErr := challengeServer.ListenAndServe(); challengeErr != nil && !errors.Is(challengeErr, http.ErrServerClosed) {
				logger.Error("ACME HTTP challenge listener stopped", "error", challengeErr)
			}
		}()
		return httpServer.ListenAndServeTLS("", "")
	}

	go func() {
		ticker := time.NewTicker(time.Hour)
		defer ticker.Stop()
		for {
			select {
			case <-runContext.Done():
				return
			case now := <-ticker.C:
				removed, gcErr := database.GarbageCollect(now.UTC())
				if gcErr != nil {
					logger.Warn("relay garbage collection failed", "error", gcErr)
				} else {
					logger.Info("relay garbage collection completed", "removed", removed)
				}
			}
		}
	}()
	serverErrors := make(chan error, 1)
	go func() { serverErrors <- serve() }()
	select {
	case err = <-serverErrors:
		if err != nil && !errors.Is(err, http.ErrServerClosed) {
			logger.Error("relay stopped", "error", err)
			os.Exit(1)
		}
	case <-runContext.Done():
		logger.Info("coworker relay draining")
		relayServer.Drain()
		time.Sleep(5 * time.Second)
		relayServer.CloseTunnels()
		shutdownContext, cancel := context.WithTimeout(context.Background(), 30*time.Second)
		defer cancel()
		if challengeServer != nil {
			_ = challengeServer.Shutdown(shutdownContext)
		}
		if err = httpServer.Shutdown(shutdownContext); err != nil {
			logger.Error("relay graceful shutdown failed", "error", err)
			os.Exit(1)
		}
		logger.Info("coworker relay stopped")
	}
}

func env(name, fallback string) string {
	if value := os.Getenv(name); value != "" {
		return value
	}
	return fallback
}

func envInteger(name string, fallback, minimum, maximum int) (int, error) {
	value, err := strconv.Atoi(env(name, strconv.Itoa(fallback)))
	if err != nil || value < minimum || value > maximum {
		return 0, fmt.Errorf("%s must be between %d and %d", name, minimum, maximum)
	}
	return value, nil
}

func envDuration(name string, fallback time.Duration) (time.Duration, error) {
	value, err := time.ParseDuration(env(name, fallback.String()))
	if err != nil || value <= 0 {
		return 0, fmt.Errorf("%s must be a positive Go duration", name)
	}
	return value, nil
}

func envInt64(name string, fallback, minimum, maximum int64) (int64, error) {
	value, err := strconv.ParseInt(env(name, strconv.FormatInt(fallback, 10)), 10, 64)
	if err != nil || value < minimum || value > maximum {
		return 0, fmt.Errorf("%s must be between %d and %d", name, minimum, maximum)
	}
	return value, nil
}

func secretEnvironment(name string) (string, error) {
	value := os.Getenv(name)
	filePath := strings.TrimSpace(os.Getenv(name + "_FILE"))
	if value != "" && filePath != "" {
		return "", fmt.Errorf("%s and %s_FILE cannot both be set", name, name)
	}
	if filePath == "" {
		return value, nil
	}
	raw, err := os.ReadFile(filePath)
	if err != nil {
		return "", fmt.Errorf("read %s_FILE: %w", name, err)
	}
	return strings.TrimSpace(string(raw)), nil
}
