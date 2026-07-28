package main

import (
	"bufio"
	"bytes"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"encoding/base64"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"

	"github.com/VirtualBeingsResearch/CoWorker/apps/coworker-relay/internal/buildinfo"
	"github.com/VirtualBeingsResearch/CoWorker/apps/coworker-relay/internal/netutil"
	"github.com/VirtualBeingsResearch/CoWorker/apps/coworker-relay/internal/store"
	"golang.org/x/term"
)

type client struct {
	baseURL string
	token   string
	http    *http.Client
}

func runCLI(args []string) {
	if handled, err := handleHelp(args, os.Stdout); handled {
		if err != nil {
			fatal(err.Error())
		}
		return
	}
	if args[0] == "init" {
		initDeployment(args[1:])
		return
	}
	if args[0] == "version" {
		if len(args) != 1 {
			usage()
		}
		fmt.Printf(
			"coworker-relay %s (%s, %s)\n",
			buildinfo.Version,
			buildinfo.Commit,
			buildinfo.Date,
		)
		return
	}
	if args[0] == "restore" {
		restoreDatabase(args[1:])
		return
	}
	switch args[0] {
	case "health", "instance", "bans", "cache", "backup", "gc", "metrics":
	default:
		usage()
	}
	if err := loadLocalEnvironment(); err != nil {
		fatal(err.Error())
	}
	httpClient, err := newHTTPClient()
	if err != nil {
		fatal(err.Error())
	}
	c := client{
		baseURL: strings.TrimRight(firstEnvironment("RELAY_URL", "RELAY_PUBLIC_URL"), "/"),
		token:   os.Getenv("RELAY_ADMIN_TOKEN"),
		http:    httpClient,
	}
	if c.baseURL == "" {
		fatal("RELAY_URL is required")
	}
	switch args[0] {
	case "health":
		c.print("GET", "/_relay/v1/health", nil, false)
	case "instance":
		c.instance(args[1:])
	case "bans":
		c.bans(args[1:])
	case "cache":
		c.cache(args[1:])
	case "backup":
		c.backup(args[1:])
	case "gc":
		c.print("POST", "/_relay/v1/admin/gc", nil, true)
	case "metrics":
		c.print("GET", "/_relay/v1/admin/metrics", nil, true)
	default:
		usage()
	}
}

func firstEnvironment(names ...string) string {
	for _, name := range names {
		if value := os.Getenv(name); value != "" {
			return value
		}
	}
	return ""
}

func newHTTPClient() (*http.Client, error) {
	transport := http.DefaultTransport.(*http.Transport).Clone()
	if path := strings.TrimSpace(os.Getenv("RELAY_CA_CERT")); path != "" {
		certificate, err := os.ReadFile(path)
		if err != nil {
			return nil, fmt.Errorf("read RELAY_CA_CERT: %w", err)
		}
		roots, err := x509.SystemCertPool()
		if err != nil || roots == nil {
			roots = x509.NewCertPool()
		}
		if !roots.AppendCertsFromPEM(certificate) {
			return nil, errors.New("RELAY_CA_CERT contains no valid PEM certificates")
		}
		transport.TLSClientConfig = &tls.Config{
			MinVersion: tls.VersionTLS12,
			RootCAs:    roots,
		}
	}
	return &http.Client{Timeout: 20 * time.Second, Transport: transport}, nil
}

type initOptions struct {
	directory    string
	publicURL    string
	externalPort int
	acmeDomain   string
	tlsCert      string
	tlsKey       string
	image        string
	force        bool
}

var errInitCancelled = errors.New("initialization cancelled")

func initDeployment(args []string) {
	options := defaultInitOptions()
	var err error
	if len(args) == 0 {
		if !isTerminal(os.Stdin) {
			fatal("interactive init requires a terminal; use --public-url for non-interactive setup")
		}
		options, err = promptInitOptions(os.Stdin, os.Stderr, options)
		if errors.Is(err, errInitCancelled) {
			fmt.Fprintln(os.Stdout, "Initialization cancelled.")
			return
		}
	} else {
		options, err = parseInitOptions(args)
	}
	if err != nil {
		fatal(err.Error())
	}
	_, normalizedURL, err := initialize(options)
	if err != nil {
		fatal(err.Error())
	}
	absolute, _ := filepath.Abs(options.directory)
	fmt.Printf(
		"Relay deployment initialized in %s\nPublic URL: %s\nAdministrator token: saved only in %s\n\nNext:\n  cd %s\n  docker compose up -d\n  coworker-relay health\n  coworker-relay instance create --name home-coworker\n",
		absolute,
		normalizedURL,
		filepath.Join(absolute, ".env"),
		absolute,
	)
}

func defaultInitOptions() initOptions {
	return initOptions{
		directory:    "coworker-relay-deploy",
		externalPort: 8443,
		image:        "ghcr.io/virtualbeingsresearch/coworker-relay:" + releaseTag(),
	}
}

func parseInitOptions(args []string) (initOptions, error) {
	options := defaultInitOptions()
	flags := flag.NewFlagSet("init", flag.ContinueOnError)
	flags.SetOutput(io.Discard)
	flags.StringVar(&options.directory, "dir", "coworker-relay-deploy", "deployment directory")
	flags.StringVar(&options.publicURL, "public-url", "", "public HTTPS URL")
	flags.IntVar(&options.externalPort, "external-port", 8443, "published HTTPS port")
	flags.StringVar(&options.acmeDomain, "acme-domain", "", "ACME certificate domain")
	flags.StringVar(&options.tlsCert, "tls-cert", "", "host path to PEM certificate")
	flags.StringVar(&options.tlsKey, "tls-key", "", "host path to PEM private key")
	flags.StringVar(
		&options.image,
		"image",
		options.image,
		"container image",
	)
	flags.BoolVar(&options.force, "force", false, "replace generated files")
	if err := flags.Parse(args); err != nil {
		return initOptions{}, err
	}
	if flags.NArg() != 0 {
		return initOptions{}, fmt.Errorf("unexpected init argument %q", flags.Arg(0))
	}
	return options, nil
}

func promptInitOptions(
	input io.Reader,
	output io.Writer,
	options initOptions,
) (initOptions, error) {
	fmt.Fprintln(output, "Coworker Relay setup")
	fmt.Fprintln(output, "Press Enter to accept a displayed default.")
	reader, err := newInitLineReader(input, output)
	if err != nil {
		return initOptions{}, err
	}

	options.directory, err = prompt(reader, output, "Deployment directory", options.directory)
	if err != nil {
		return initOptions{}, err
	}
	for {
		rawPort, promptErr := prompt(
			reader,
			output,
			"Published HTTPS port",
			strconv.Itoa(options.externalPort),
		)
		if promptErr != nil {
			return initOptions{}, promptErr
		}
		port, parseErr := strconv.Atoi(rawPort)
		if parseErr == nil && port >= 1 && port <= 65535 {
			options.externalPort = port
			break
		}
		fmt.Fprintln(output, "Enter a port between 1 and 65535.")
	}
	originExample := "https://relay.example.com"
	if options.externalPort != 443 {
		originExample = net.JoinHostPort("relay.example.com", strconv.Itoa(options.externalPort))
		originExample = "https://" + originExample
	}
	fmt.Fprintln(
		output,
		"Public HTTPS origin is the externally reachable Relay address (no path).",
	)
	fmt.Fprintf(output, "Example: %s\n", originExample)
	for {
		options.publicURL, err = prompt(reader, output, "Public HTTPS origin", "")
		if err != nil {
			return initOptions{}, err
		}
		if options.publicURL == "" {
			fmt.Fprintln(output, "A public HTTPS origin is required.")
			continue
		}
		if _, normalized, normalizeErr := normalizePublicURL(
			options.publicURL,
			options.externalPort,
		); normalizeErr == nil {
			options.publicURL = normalized
			break
		} else {
			fmt.Fprintln(output, normalizeErr)
		}
	}
	parsed, _, _ := normalizePublicURL(options.publicURL, options.externalPort)
	hostname := parsed.Hostname()
	ip := net.ParseIP(hostname)
	if ip != nil && !netutil.IsPublicIP(ip) {
		fmt.Fprintln(
			output,
			"Private or non-routable IP detected; use a certificate from your private CA.",
		)
		options.tlsCert, err = requiredPrompt(reader, output, "TLS certificate path")
		if err != nil {
			return initOptions{}, err
		}
		options.tlsKey, err = requiredPrompt(reader, output, "TLS private key path")
		if err != nil {
			return initOptions{}, err
		}
	} else {
		acmeDescription := "  1) Automatic ACME certificate (requires Internet-facing TCP port 80 for validation)\n" +
			"  2) Existing PEM certificate"
		if ip != nil {
			acmeDescription = "  1) Automatic public-IP certificate (requires Internet-facing TCP port 80 for validation)\n" +
				"  2) Existing PEM certificate"
		}
		mode, modeErr := promptChoice(
			reader,
			output,
			"TLS mode",
			"acme",
			map[string]string{
				"1": "acme", "acme": "acme",
				"2": "pem", "pem": "pem",
			},
			acmeDescription,
		)
		if modeErr != nil {
			return initOptions{}, modeErr
		}
		if mode == "acme" {
			options.acmeDomain = hostname
			if ip == nil {
				options.acmeDomain, err = prompt(
					reader,
					output,
					"ACME certificate domain",
					hostname,
				)
				if err != nil {
					return initOptions{}, err
				}
			}
		} else {
			options.tlsCert, err = requiredPrompt(reader, output, "TLS certificate path")
			if err != nil {
				return initOptions{}, err
			}
			options.tlsKey, err = requiredPrompt(reader, output, "TLS private key path")
			if err != nil {
				return initOptions{}, err
			}
		}
	}
	options.image, err = prompt(reader, output, "Container image", options.image)
	if err != nil {
		return initOptions{}, err
	}
	existing, err := generatedDeploymentExists(options.directory)
	if err != nil {
		return initOptions{}, err
	}
	if existing {
		replace, confirmErr := promptConfirm(
			reader,
			output,
			"Generated deployment files already exist. Replace them?",
			false,
		)
		if confirmErr != nil {
			return initOptions{}, confirmErr
		}
		if !replace {
			return initOptions{}, errInitCancelled
		}
		options.force = true
	}
	modeSummary := "ACME for " + options.acmeDomain
	if options.tlsCert != "" {
		modeSummary = "PEM certificate " + options.tlsCert
	}
	fmt.Fprintf(
		output,
		"\nDeployment summary:\n  Directory: %s\n  Public URL: %s\n  TLS: %s\n  Image: %s\n",
		options.directory,
		options.publicURL,
		modeSummary,
		options.image,
	)
	confirmed, err := promptConfirm(reader, output, "Create deployment files?", true)
	if err != nil {
		return initOptions{}, err
	}
	if !confirmed {
		return initOptions{}, errInitCancelled
	}
	return options, nil
}

type initLineReader interface {
	ReadLine(prompt string) (string, error)
}

type bufferedInitLineReader struct {
	reader *bufio.Reader
	output io.Writer
}

func (r *bufferedInitLineReader) ReadLine(prompt string) (string, error) {
	fmt.Fprint(r.output, prompt)
	value, err := r.reader.ReadString('\n')
	if err != nil && !(errors.Is(err, io.EOF) && value != "") {
		return "", errors.New("interactive input ended")
	}
	return value, nil
}

type terminalInitLineReader struct {
	terminal *term.Terminal
	prepare  func() (func(), error)
}

func (r *terminalInitLineReader) ReadLine(prompt string) (string, error) {
	restore := func() {}
	if r.prepare != nil {
		var err error
		restore, err = r.prepare()
		if err != nil {
			return "", err
		}
	}
	defer restore()
	r.terminal.SetPrompt(prompt)
	value, err := r.terminal.ReadLine()
	if err != nil {
		return "", errors.New("interactive input ended")
	}
	return value, nil
}

type splitReadWriter struct {
	io.Reader
	io.Writer
}

func newInitLineReader(
	input io.Reader,
	output io.Writer,
) (initLineReader, error) {
	file, terminalInput := input.(*os.File)
	if !terminalInput || !isTerminal(file) {
		return &bufferedInitLineReader{
			reader: bufio.NewReader(input),
			output: output,
		}, nil
	}
	terminal := term.NewTerminal(splitReadWriter{Reader: input, Writer: output}, "")
	prepare := func() (func(), error) {
		state, err := term.MakeRaw(int(file.Fd()))
		if err != nil {
			return nil, fmt.Errorf("enable terminal line editing: %w", err)
		}
		return func() {
			_ = term.Restore(int(file.Fd()), state)
		}, nil
	}
	return &terminalInitLineReader{
		terminal: terminal,
		prepare:  prepare,
	}, nil
}

func prompt(
	reader initLineReader,
	output io.Writer,
	label string,
	defaultValue string,
) (string, error) {
	promptText := label + ": "
	if defaultValue == "" {
		promptText = label + ": "
	} else {
		promptText = fmt.Sprintf("%s [%s]: ", label, defaultValue)
	}
	value, err := reader.ReadLine(promptText)
	if err != nil {
		return "", err
	}
	value = strings.TrimSpace(value)
	if value == "" {
		return defaultValue, nil
	}
	return value, nil
}

func requiredPrompt(
	reader initLineReader,
	output io.Writer,
	label string,
) (string, error) {
	for {
		value, err := prompt(reader, output, label, "")
		if err != nil {
			return "", err
		}
		if value != "" {
			return value, nil
		}
		fmt.Fprintln(output, "A value is required.")
	}
}

func promptChoice(
	reader initLineReader,
	output io.Writer,
	label string,
	defaultValue string,
	choices map[string]string,
	description string,
) (string, error) {
	for {
		fmt.Fprintln(output, description)
		value, err := prompt(reader, output, label, defaultValue)
		if err != nil {
			return "", err
		}
		if choice, ok := choices[strings.ToLower(value)]; ok {
			return choice, nil
		}
		fmt.Fprintln(output, "Choose one of the listed options.")
	}
}

func promptConfirm(
	reader initLineReader,
	output io.Writer,
	label string,
	defaultValue bool,
) (bool, error) {
	defaultLabel := "y/N"
	if defaultValue {
		defaultLabel = "Y/n"
	}
	for {
		value, readErr := reader.ReadLine(fmt.Sprintf("%s [%s]: ", label, defaultLabel))
		if readErr != nil {
			return false, readErr
		}
		value = strings.ToLower(strings.TrimSpace(value))
		if value == "" {
			return defaultValue, nil
		}
		switch value {
		case "y", "yes":
			return true, nil
		case "n", "no":
			return false, nil
		default:
			fmt.Fprintln(output, "Enter yes or no.")
		}
	}
}

func generatedDeploymentExists(directory string) (bool, error) {
	absolute, err := filepath.Abs(directory)
	if err != nil {
		return false, err
	}
	for _, name := range []string{".env", "compose.yaml", ".gitignore"} {
		_, statErr := os.Stat(filepath.Join(absolute, name))
		if statErr == nil {
			return true, nil
		}
		if !errors.Is(statErr, os.ErrNotExist) {
			return false, statErr
		}
	}
	return false, nil
}

func isTerminal(file *os.File) bool {
	return term.IsTerminal(int(file.Fd()))
}

func initialize(options initOptions) (string, string, error) {
	if options.externalPort < 1 || options.externalPort > 65535 {
		return "", "", errors.New("--external-port must be between 1 and 65535")
	}
	parsed, normalizedURL, err := normalizePublicURL(options.publicURL, options.externalPort)
	if err != nil {
		return "", "", err
	}
	if options.image == "" || strings.ContainsAny(options.image, "\r\n") {
		return "", "", errors.New("--image must not be empty or contain newlines")
	}
	pemMode := options.tlsCert != "" || options.tlsKey != ""
	if pemMode && (options.tlsCert == "" || options.tlsKey == "") {
		return "", "", errors.New("--tls-cert and --tls-key must be provided together")
	}
	if pemMode && options.acmeDomain != "" {
		return "", "", errors.New("choose either ACME or PEM certificate mode")
	}
	if !pemMode {
		if options.acmeDomain == "" {
			options.acmeDomain = parsed.Hostname()
		}
		options.acmeDomain = strings.TrimSpace(options.acmeDomain)
		if options.acmeDomain == "" || strings.ContainsAny(options.acmeDomain, "\r\n") {
			return "", "", errors.New("--acme-domain must be a hostname or public IP address")
		}
		if !strings.EqualFold(options.acmeDomain, parsed.Hostname()) {
			return "", "", errors.New("--acme-domain must match the public URL hostname")
		}
		if ip := net.ParseIP(options.acmeDomain); ip != nil && !netutil.IsPublicIP(ip) {
			return "", "", errors.New(
				"a private or non-routable IP address requires --tls-cert and --tls-key",
			)
		}
	}
	directory, err := filepath.Abs(options.directory)
	if err != nil {
		return "", "", err
	}
	envPath := filepath.Join(directory, ".env")
	composePath := filepath.Join(directory, "compose.yaml")
	gitignorePath := filepath.Join(directory, ".gitignore")
	if !options.force {
		for _, path := range []string{envPath, composePath, gitignorePath} {
			if _, err := os.Stat(path); err == nil {
				return "", "", fmt.Errorf("%s already exists; use --force to replace generated files", path)
			} else if !errors.Is(err, os.ErrNotExist) {
				return "", "", err
			}
		}
	}
	if err := os.MkdirAll(directory, 0o700); err != nil {
		return "", "", err
	}
	adminToken, err := randomAdminToken()
	if err != nil {
		return "", "", err
	}
	env := strings.Join([]string{
		"RELAY_PUBLIC_URL=" + normalizedURL,
		"RELAY_URL=" + normalizedURL,
		"RELAY_ADMIN_TOKEN=" + adminToken,
		"RELAY_LISTEN=:8443",
		"RELAY_DATABASE=/var/lib/coworker-relay/relay.db",
		"RELAY_CACHE_DIR=/var/lib/coworker-relay/cache",
		"RELAY_CACHE_MAX_BYTES=4294967296",
		"RELAY_REQUESTS_PER_MINUTE=600",
		"RELAY_ANONYMOUS_PER_MINUTE=60",
		"RELAY_BAN_FAILURE_LIMIT=5",
		"RELAY_BAN_FAILURE_WINDOW=10m",
		"RELAY_BAN_DURATION=1h",
		"RELAY_MAX_REQUEST_BODY_BYTES=33554432",
		"RELAY_MAX_TUNNEL_FRAME_BYTES=50331648",
		"RELAY_EXTERNAL_PORT=" + strconv.Itoa(options.externalPort),
		"RELAY_IMAGE=" + options.image,
	}, "\n") + "\n"
	composeVolumes := "      - relay-data:/var/lib/coworker-relay\n"
	ports := "      - \"${RELAY_EXTERNAL_PORT}:8443\"\n"
	if pemMode {
		cert, certErr := filepath.Abs(options.tlsCert)
		if certErr != nil {
			return "", "", certErr
		}
		key, keyErr := filepath.Abs(options.tlsKey)
		if keyErr != nil {
			return "", "", keyErr
		}
		env += "RELAY_TLS_CERT=/run/tls/fullchain.pem\nRELAY_TLS_KEY=/run/tls/privkey.pem\n"
		composeVolumes += "      - " + strconv.Quote(cert+":/run/tls/fullchain.pem:ro") + "\n"
		composeVolumes += "      - " + strconv.Quote(key+":/run/tls/privkey.pem:ro") + "\n"
	} else {
		if net.ParseIP(options.acmeDomain) != nil {
			env += "RELAY_ACME_IP=" + options.acmeDomain + "\n"
		} else {
			env += "RELAY_ACME_DOMAIN=" + options.acmeDomain + "\n"
		}
		env += "RELAY_ACME_HTTP_LISTEN=:8080\n"
		env += "RELAY_ACME_CACHE=/var/lib/coworker-relay/acme\n"
		ports += "      - \"80:8080\"\n"
	}
	compose := "services:\n" +
		"  relay:\n" +
		"    image: ${RELAY_IMAGE}\n" +
		"    restart: unless-stopped\n" +
		"    stop_grace_period: 35s\n" +
		"    env_file:\n" +
		"      - .env\n" +
		"    ports:\n" + ports +
		"    volumes:\n" + composeVolumes +
		"    read_only: true\n" +
		"    pids_limit: 256\n" +
		"    ulimits:\n" +
		"      nofile:\n" +
		"        soft: 65536\n" +
		"        hard: 65536\n" +
		"    tmpfs:\n" +
		"      - /tmp\n" +
		"    healthcheck:\n" +
		"      test: [\"CMD\", \"coworker-relay\", \"health\"]\n" +
		"      interval: 30s\n" +
		"      timeout: 10s\n" +
		"      retries: 3\n" +
		"      start_period: 15s\n" +
		"    logging:\n" +
		"      driver: json-file\n" +
		"      options:\n" +
		"        max-size: \"10m\"\n" +
		"        max-file: \"5\"\n\n" +
		"volumes:\n" +
		"  relay-data:\n"
	flag := os.O_WRONLY | os.O_CREATE
	if options.force {
		flag |= os.O_TRUNC
	} else {
		flag |= os.O_EXCL
	}
	if err := writeFile(envPath, []byte(env), 0o600, flag); err != nil {
		return "", "", err
	}
	if err := writeFile(composePath, []byte(compose), 0o644, flag); err != nil {
		return "", "", err
	}
	if err := writeFile(gitignorePath, []byte(".env\n"), 0o644, flag); err != nil {
		return "", "", err
	}
	return adminToken, normalizedURL, nil
}

func releaseTag() string {
	if buildinfo.Version == "" || buildinfo.Version == "dev" {
		return "latest"
	}
	return buildinfo.Version
}

func normalizePublicURL(raw string, externalPort int) (*url.URL, string, error) {
	parsed, err := url.Parse(strings.TrimSpace(strings.TrimRight(raw, "/")))
	if err != nil ||
		parsed.Scheme != "https" ||
		parsed.Hostname() == "" ||
		parsed.User != nil ||
		parsed.Path != "" ||
		parsed.RawQuery != "" ||
		parsed.Fragment != "" {
		return nil, "", errors.New("--public-url must be an HTTPS origin")
	}
	if parsed.Port() == "" && externalPort != 443 {
		parsed.Host = net.JoinHostPort(parsed.Hostname(), strconv.Itoa(externalPort))
	} else if parsed.Port() != "" && parsed.Port() != strconv.Itoa(externalPort) {
		return nil, "", errors.New("--public-url port must match --external-port")
	}
	return parsed, parsed.String(), nil
}

func randomAdminToken() (string, error) {
	raw := make([]byte, 32)
	if _, err := rand.Read(raw); err != nil {
		return "", err
	}
	return base64.RawURLEncoding.EncodeToString(raw), nil
}

func writeFile(path string, body []byte, mode os.FileMode, flag int) error {
	file, err := os.OpenFile(path, flag, mode)
	if err != nil {
		return err
	}
	if err := file.Chmod(mode); err != nil {
		_ = file.Close()
		return err
	}
	if _, err := file.Write(body); err != nil {
		_ = file.Close()
		return err
	}
	return file.Close()
}

func loadLocalEnvironment() error {
	path := os.Getenv("RELAY_CONFIG")
	explicit := path != ""
	if path == "" {
		path = ".env"
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		if errors.Is(err, os.ErrNotExist) && !explicit {
			return nil
		}
		return fmt.Errorf("read Relay configuration %s: %w", path, err)
	}
	for lineNumber, line := range strings.Split(string(raw), "\n") {
		line = strings.TrimSpace(line)
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		name, value, ok := strings.Cut(line, "=")
		name = strings.TrimSpace(name)
		if !ok {
			return fmt.Errorf(
				"invalid Relay configuration %s:%d",
				path,
				lineNumber+1,
			)
		}
		if strings.HasPrefix(name, "RELAY_") {
			if _, exists := os.LookupEnv(name); exists {
				continue
			}
			if err := os.Setenv(name, value); err != nil {
				return fmt.Errorf(
					"invalid Relay configuration variable %s: %w",
					name,
					err,
				)
			}
		}
	}
	return nil
}

func (c client) cache(args []string) {
	if len(args) != 1 {
		usage()
	}
	switch args[0] {
	case "inspect":
		c.print("GET", "/_relay/v1/admin/cache", nil, true)
	case "purge":
		c.print("DELETE", "/_relay/v1/admin/cache", nil, true)
	default:
		usage()
	}
}

func (c client) instance(args []string) {
	if len(args) == 0 {
		usage()
	}
	switch args[0] {
	case "create":
		flags := flag.NewFlagSet("instance create", flag.ExitOnError)
		name := flags.String("name", "", "display name")
		_ = flags.Parse(args[1:])
		c.print("POST", "/_relay/v1/admin/instances", map[string]string{"name": *name}, true)
	case "list":
		c.print("GET", "/_relay/v1/admin/instances", nil, true)
	case "revoke":
		if len(args) != 2 {
			fatal("usage: coworker-relay instance revoke <instance_id>")
		}
		c.print("DELETE", "/_relay/v1/admin/instances/"+url.PathEscape(args[1]), nil, true)
	case "rotate-credential":
		if len(args) != 2 {
			fatal("usage: coworker-relay instance rotate-credential <instance_id>")
		}
		path := "/_relay/v1/admin/instances/" + url.PathEscape(args[1]) + "/rotate-credential"
		c.print("POST", path, nil, true)
	case "update-auth":
		if len(args) != 3 || (args[2] != "optional" && args[2] != "required") {
			fatal("usage: coworker-relay instance update-auth <instance_id> <optional|required>")
		}
		path := "/_relay/v1/admin/instances/" + url.PathEscape(args[1]) + "/update-auth"
		c.print("PATCH", path, map[string]string{"mode": args[2]}, true)
	case "update-stats":
		if len(args) != 2 {
			fatal("usage: coworker-relay instance update-stats <instance_id>")
		}
		path := "/_relay/v1/admin/instances/" + url.PathEscape(args[1]) + "/update-stats"
		c.print("GET", path, nil, true)
	default:
		usage()
	}
}

func (c client) backup(args []string) {
	flags := flag.NewFlagSet("backup", flag.ExitOnError)
	output := flags.String("output", "", "backup output path")
	_ = flags.Parse(args)
	if *output == "" {
		*output = "coworker-relay-" + time.Now().UTC().Format("20060102T150405Z") + ".db"
	}
	request, err := http.NewRequest("GET", c.baseURL+"/_relay/v1/admin/backup", nil)
	if err != nil {
		fatal(err.Error())
	}
	if c.token == "" {
		fatal("RELAY_ADMIN_TOKEN is required")
	}
	request.Header.Set("Authorization", "Bearer "+c.token)
	response, err := c.http.Do(request)
	if err != nil {
		fatal(err.Error())
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		raw, _ := io.ReadAll(io.LimitReader(response.Body, 1<<20))
		fatal(fmt.Sprintf("%s: %s", response.Status, strings.TrimSpace(string(raw))))
	}
	file, err := os.OpenFile(*output, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0o600)
	if err != nil {
		fatal(err.Error())
	}
	_, copyErr := io.Copy(file, response.Body)
	closeErr := file.Close()
	if copyErr != nil || closeErr != nil {
		_ = os.Remove(*output)
		if copyErr != nil {
			fatal(copyErr.Error())
		}
		fatal(closeErr.Error())
	}
	fmt.Println(*output)
}

func restoreDatabase(args []string) {
	flags := flag.NewFlagSet("restore", flag.ExitOnError)
	source := flags.String("from", "", "backup database path")
	destination := flags.String("database", "", "stopped Relay database path")
	force := flags.Bool("force", false, "replace an existing database after preserving it")
	_ = flags.Parse(args)
	if *source == "" || *destination == "" {
		fatal("--from and --database are required")
	}
	sourcePath, sourceErr := filepath.Abs(*source)
	destinationPath, destinationErr := filepath.Abs(*destination)
	if sourceErr != nil || destinationErr != nil {
		fatal("unable to resolve backup or database path")
	}
	if sourcePath == destinationPath {
		fatal("--from and --database must be different paths")
	}
	*source, *destination = sourcePath, destinationPath
	if err := store.Validate(*source); err != nil {
		fatal("backup validation failed: " + err.Error())
	}
	if _, err := os.Stat(*destination); err == nil {
		if !*force {
			fatal("destination exists; stop Relay and pass --force to preserve and replace it")
		}
		preserved := *destination + ".before-restore-" + time.Now().UTC().Format("20060102T150405Z")
		if err := os.Rename(*destination, preserved); err != nil {
			fatal(err.Error())
		}
		fmt.Fprintln(os.Stderr, "preserved previous database:", preserved)
	} else if !errors.Is(err, os.ErrNotExist) {
		fatal(err.Error())
	}
	input, err := os.Open(*source)
	if err != nil {
		fatal(err.Error())
	}
	defer input.Close()
	output, err := os.OpenFile(*destination, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0o600)
	if err != nil {
		fatal(err.Error())
	}
	_, copyErr := io.Copy(output, input)
	closeErr := output.Close()
	if copyErr != nil || closeErr != nil {
		_ = os.Remove(*destination)
		if copyErr != nil {
			fatal(copyErr.Error())
		}
		fatal(closeErr.Error())
	}
	fmt.Println(*destination)
}

func (c client) bans(args []string) {
	if len(args) == 0 {
		usage()
	}
	switch args[0] {
	case "list":
		flags := flag.NewFlagSet("bans list", flag.ExitOnError)
		instance := flags.String("instance", "", "instance ID")
		_ = flags.Parse(args[1:])
		path := "/_relay/v1/admin/bans"
		if *instance != "" {
			path += "?instance=" + url.QueryEscape(*instance)
		}
		c.print("GET", path, nil, true)
	case "remove":
		flags := flag.NewFlagSet("bans remove", flag.ExitOnError)
		instance := flags.String("instance", "", "instance ID")
		ip := flags.String("ip", "", "source IP")
		reason := flags.String("reason", "", "audit reason")
		_ = flags.Parse(args[1:])
		if *instance == "" || *ip == "" || strings.TrimSpace(*reason) == "" {
			fatal("--instance, --ip and --reason are required")
		}
		query := url.Values{"instance": {*instance}, "ip": {*ip}, "reason": {*reason}}
		c.print("DELETE", "/_relay/v1/admin/bans?"+query.Encode(), nil, true)
	default:
		usage()
	}
}

func (c client) print(method, path string, body any, admin bool) {
	var reader io.Reader
	if body != nil {
		raw, err := json.Marshal(body)
		if err != nil {
			fatal(err.Error())
		}
		reader = bytes.NewReader(raw)
	}
	request, err := http.NewRequest(method, c.baseURL+path, reader)
	if err != nil {
		fatal(err.Error())
	}
	if body != nil {
		request.Header.Set("Content-Type", "application/json")
	}
	if admin {
		if c.token == "" {
			fatal("RELAY_ADMIN_TOKEN is required")
		}
		request.Header.Set("Authorization", "Bearer "+c.token)
	}
	response, err := c.http.Do(request)
	if err != nil {
		fatal(err.Error())
	}
	defer response.Body.Close()
	raw, _ := io.ReadAll(io.LimitReader(response.Body, 1<<20))
	if response.StatusCode < 200 || response.StatusCode >= 300 {
		fatal(fmt.Sprintf("%s: %s", response.Status, strings.TrimSpace(string(raw))))
	}
	if len(bytes.TrimSpace(raw)) == 0 {
		fmt.Println("ok")
		return
	}
	var pretty bytes.Buffer
	if json.Indent(&pretty, raw, "", "  ") == nil {
		fmt.Println(pretty.String())
	} else {
		fmt.Println(string(raw))
	}
}

func usage() {
	fmt.Fprintln(os.Stderr, `usage:
  coworker-relay serve
  coworker-relay init --public-url <https-origin> [--dir <directory>]
                [--external-port 8443]
                [--acme-domain <domain> | --tls-cert <path> --tls-key <path>]
  coworker-relay health
  coworker-relay version
  coworker-relay backup [--output <path>]
  coworker-relay restore --from <backup.db> --database <stopped-relay.db> [--force]
  coworker-relay gc
  coworker-relay metrics
  coworker-relay instance create --name <name>
  coworker-relay instance list
  coworker-relay instance revoke <instance_id>
  coworker-relay instance rotate-credential <instance_id>
  coworker-relay instance update-auth <instance_id> <optional|required>
  coworker-relay instance update-stats <instance_id>
  coworker-relay bans list [--instance <instance_id>]
  coworker-relay bans remove --instance <instance_id> --ip <ip> --reason <reason>
  coworker-relay cache inspect
  coworker-relay cache purge

configuration:
  RELAY_URL=https://relay.example.com:8443
  RELAY_ADMIN_TOKEN=<shared administrator token>
  RELAY_CA_CERT=/path/to/private-ca.pem
  RELAY_CONFIG=.env  # optional; defaults to ./.env`)
	os.Exit(2)
}

func fatal(message string) {
	fmt.Fprintln(os.Stderr, "coworker-relay:", message)
	os.Exit(1)
}
