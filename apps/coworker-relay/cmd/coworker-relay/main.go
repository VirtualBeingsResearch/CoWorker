package main

import (
	"errors"
	"log/slog"
	"net/http"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"

	relaycache "github.com/VirtualBeingsResearch/CoWorker/apps/coworker-relay/internal/cache"
	"github.com/VirtualBeingsResearch/CoWorker/apps/coworker-relay/internal/server"
	"github.com/VirtualBeingsResearch/CoWorker/apps/coworker-relay/internal/store"
	"golang.org/x/crypto/acme/autocert"
)

func main() {
	logger := slog.New(slog.NewJSONHandler(os.Stdout, nil))
	publicURL, err := server.ValidatePublicURL(os.Getenv("RELAY_PUBLIC_URL"))
	if err != nil {
		logger.Error("invalid relay configuration", "error", err)
		os.Exit(1)
	}
	adminToken := os.Getenv("RELAY_ADMIN_TOKEN")
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
	handler := server.New(server.Config{
		PublicURL: publicURL, AdminToken: adminToken, TrustedProxies: trusted,
		VerifierParallel: parallel, Cache: assetCache,
	}, database, logger).Handler()
	httpServer := &http.Server{
		Addr:              env("RELAY_LISTEN", ":8443"),
		Handler:           handler,
		ReadHeaderTimeout: 10 * time.Second,
		IdleTimeout:       90 * time.Second,
		MaxHeaderBytes:    32 << 10,
	}
	cert, key := os.Getenv("RELAY_TLS_CERT"), os.Getenv("RELAY_TLS_KEY")
	logger.Info("coworker relay starting", "listen", httpServer.Addr, "public_url", publicURL)
	if cert != "" || key != "" {
		if cert == "" || key == "" {
			logger.Error("RELAY_TLS_CERT and RELAY_TLS_KEY must be provided together")
			os.Exit(1)
		}
		err = httpServer.ListenAndServeTLS(cert, key)
	} else {
		domain := strings.TrimSpace(os.Getenv("RELAY_ACME_DOMAIN"))
		if domain == "" {
			logger.Error("configure RELAY_TLS_CERT/RELAY_TLS_KEY or RELAY_ACME_DOMAIN")
			os.Exit(1)
		}
		manager := &autocert.Manager{
			Prompt:     autocert.AcceptTOS,
			Email:      os.Getenv("RELAY_ACME_EMAIL"),
			HostPolicy: autocert.HostWhitelist(domain),
			Cache:      autocert.DirCache(env("RELAY_ACME_CACHE", filepath.Join(dataDirectory, "acme"))),
		}
		httpServer.TLSConfig = manager.TLSConfig()
		challengeServer := &http.Server{
			Addr:              env("RELAY_ACME_HTTP_LISTEN", ":80"),
			Handler:           manager.HTTPHandler(nil),
			ReadHeaderTimeout: 10 * time.Second,
		}
		go func() {
			if challengeErr := challengeServer.ListenAndServe(); challengeErr != nil && !errors.Is(challengeErr, http.ErrServerClosed) {
				logger.Error("ACME HTTP challenge listener stopped", "error", challengeErr)
			}
		}()
		err = httpServer.ListenAndServeTLS("", "")
	}
	if err != nil && !errors.Is(err, http.ErrServerClosed) {
		logger.Error("relay stopped", "error", err)
		os.Exit(1)
	}
}

func env(name, fallback string) string {
	if value := os.Getenv(name); value != "" {
		return value
	}
	return fallback
}
