package main

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"time"

	"github.com/VirtualBeingsResearch/CoWorker/apps/coworker-relay/internal/buildinfo"
	"github.com/VirtualBeingsResearch/CoWorker/apps/coworker-relay/internal/server"
	"github.com/VirtualBeingsResearch/CoWorker/apps/coworker-relay/internal/store"
)

func main() {
	args := os.Args[1:]
	var configPath string
	var err error
	args, configPath, err = extractConfigArgument(args)
	if err != nil {
		fatal(err.Error())
	}
	if configPath != "" {
		if err := os.Setenv("RELAY_CONFIG", configPath); err != nil {
			fatal(err.Error())
		}
	}
	if len(args) == 0 {
		args = []string{"help"}
	}
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

func extractConfigArgument(args []string) ([]string, string, error) {
	result := make([]string, 0, len(args))
	configPath := ""
	for index := 0; index < len(args); index++ {
		arg := args[index]
		switch {
		case arg == "--config":
			if configPath != "" || index+1 >= len(args) ||
				strings.TrimSpace(args[index+1]) == "" {
				return nil, "", errors.New("--config requires one path")
			}
			configPath = args[index+1]
			index++
		case strings.HasPrefix(arg, "--config="):
			if configPath != "" {
				return nil, "", errors.New("--config may only be specified once")
			}
			configPath = strings.TrimPrefix(arg, "--config=")
			if strings.TrimSpace(configPath) == "" {
				return nil, "", errors.New("--config requires one path")
			}
		default:
			result = append(result, arg)
		}
	}
	return result, configPath, nil
}

func serveRelay() {
	logger := slog.New(slog.NewJSONHandler(os.Stdout, nil))
	logger.Info("coworker relay build", "build", buildinfo.Values())
	publicURL, err := server.ValidatePublicURL(os.Getenv("RELAY_PUBLIC_URL"))
	if err != nil {
		fatalConfiguration(logger, err)
	}
	adminToken, err := secretEnvironment("RELAY_ADMIN_TOKEN")
	if err != nil {
		fatalConfiguration(logger, err)
	}
	if len(adminToken) < 24 {
		fatalConfiguration(logger, errors.New("RELAY_ADMIN_TOKEN must contain at least 24 characters"))
	}
	trusted, err := server.ParseTrustedProxies(os.Getenv("RELAY_TRUSTED_PROXY_CIDRS"))
	if err != nil {
		fatalConfiguration(logger, fmt.Errorf("invalid trusted proxy configuration: %w", err))
	}
	databasePath := env("RELAY_DATABASE", "data/coworker-relay.db")
	dataDirectory := filepath.Dir(databasePath)
	if err := os.MkdirAll(dataDirectory, 0o700); err != nil {
		fatalConfiguration(logger, fmt.Errorf("create relay data directory: %w", err))
	}
	database, err := store.Open(databasePath)
	if err != nil {
		fatalConfiguration(logger, fmt.Errorf("open relay database: %w", err))
	}
	defer database.Close()
	failureLimit, err := envInteger("RELAY_BAN_FAILURE_LIMIT", 5, 1, 100)
	if err != nil {
		fatalConfiguration(logger, err)
	}
	failureWindow, err := envDuration("RELAY_BAN_FAILURE_WINDOW", 10*time.Minute)
	if err != nil {
		fatalConfiguration(logger, err)
	}
	banDuration, err := envDuration("RELAY_BAN_DURATION", time.Hour)
	if err != nil {
		fatalConfiguration(logger, err)
	}
	if err := database.SetAuthPolicy(failureWindow, failureLimit, banDuration); err != nil {
		fatalConfiguration(logger, err)
	}
	authParallel, err := envInteger("RELAY_AUTH_CONCURRENCY", 32, 1, 1024)
	if err != nil {
		fatalConfiguration(logger, err)
	}
	connectionLimit, err := envInteger("RELAY_CONNECTIONS_PER_MINUTE", 60, 1, 100_000)
	if err != nil {
		fatalConfiguration(logger, err)
	}
	frameLimit, err := envInteger("RELAY_FRAMES_PER_MINUTE", 2400, 1, 10_000_000)
	if err != nil {
		fatalConfiguration(logger, err)
	}
	maxFrame, err := envInt64("RELAY_MAX_FRAME_BYTES", 256<<10, 1024, 4<<20)
	if err != nil {
		fatalConfiguration(logger, err)
	}
	signingKey, err := server.LoadOrCreateSigningKey(
		env("RELAY_SIGNING_KEY", filepath.Join(dataDirectory, "relay-signing.key")),
	)
	if err != nil {
		fatalConfiguration(logger, fmt.Errorf("load Relay signing key: %w", err))
	}
	relayServer := server.New(server.Config{
		PublicURL: publicURL, AdminToken: adminToken, TrustedProxies: trusted,
		AuthParallel: authParallel, ConnectionLimit: connectionLimit,
		FrameLimit: frameLimit, MaxFrame: maxFrame, RelayPrivateKey: signingKey,
	}, database, logger)
	publicServer := &http.Server{
		Addr: env("RELAY_LISTEN", ":8443"), Handler: relayServer.PublicHandler(),
		ReadHeaderTimeout: 10 * time.Second, IdleTimeout: 90 * time.Second,
		MaxHeaderBytes: 32 << 10,
	}
	adminServer := &http.Server{
		Addr:              env("RELAY_ADMIN_LISTEN", "127.0.0.1:8444"),
		Handler:           relayServer.AdminHandler(),
		ReadHeaderTimeout: 10 * time.Second, IdleTimeout: 60 * time.Second,
		MaxHeaderBytes: 16 << 10,
	}
	logger.Info(
		"coworker relay starting",
		"listen", publicServer.Addr, "admin_listen", adminServer.Addr,
		"public_url", publicURL, "transport", "ws", "e2ee", true,
	)
	runContext, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
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
	serverErrors := make(chan error, 2)
	go func() { serverErrors <- publicServer.ListenAndServe() }()
	go func() { serverErrors <- adminServer.ListenAndServe() }()
	select {
	case err = <-serverErrors:
		if err != nil && !errors.Is(err, http.ErrServerClosed) {
			logger.Error("relay stopped", "error", err)
			os.Exit(1)
		}
	case <-runContext.Done():
		logger.Info("coworker relay draining")
		relayServer.Drain()
		relayServer.CloseTunnels()
		shutdownContext, cancel := context.WithTimeout(context.Background(), 30*time.Second)
		defer cancel()
		publicErr := publicServer.Shutdown(shutdownContext)
		adminErr := adminServer.Shutdown(shutdownContext)
		if publicErr != nil || adminErr != nil {
			logger.Error(
				"relay graceful shutdown failed",
				"public_error", publicErr, "admin_error", adminErr,
			)
			os.Exit(1)
		}
		logger.Info("coworker relay stopped")
	}
}

func fatalConfiguration(logger *slog.Logger, err error) {
	logger.Error("invalid relay configuration", "error", err)
	os.Exit(1)
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
