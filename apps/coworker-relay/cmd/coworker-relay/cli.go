package main

import (
	"bufio"
	"bytes"
	"crypto/rand"
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
	switch args[0] {
	case "init":
		initDeployment(args[1:])
		return
	case "version":
		if len(args) != 1 {
			usage()
		}
		fmt.Printf(
			"coworker-relay %s (%s, %s)\n",
			buildinfo.Version, buildinfo.Commit, buildinfo.Date,
		)
		return
	case "restore":
		restoreDatabase(args[1:])
		return
	case "health", "instance", "bans", "backup", "gc", "metrics":
	default:
		usage()
	}
	if err := loadLocalEnvironment(); err != nil {
		fatal(err.Error())
	}
	c := client{
		baseURL: strings.TrimRight(
			firstEnvironment("RELAY_ADMIN_URL", "RELAY_URL"), "/",
		),
		token: os.Getenv("RELAY_ADMIN_TOKEN"),
		http:  &http.Client{Timeout: 20 * time.Second},
	}
	if c.baseURL == "" {
		c.baseURL = "http://127.0.0.1:8444"
	}
	switch args[0] {
	case "health":
		c.print("GET", "/healthz", nil, false)
	case "instance":
		c.instance(args[1:])
	case "bans":
		c.bans(args[1:])
	case "backup":
		c.backup(args[1:])
	case "gc":
		c.print("POST", "/_relay/v1/admin/gc", nil, true)
	case "metrics":
		c.print("GET", "/_relay/v1/admin/metrics", nil, true)
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

type initOptions struct {
	directory    string
	publicURL    string
	externalPort int
	adminPort    int
	deployment   string
	image        string
	force        bool
}

var errInitCancelled = errors.New("initialization cancelled")

const (
	deploymentContainer = "container"
	deploymentNative    = "native"
)

func defaultInitOptions() initOptions {
	return initOptions{
		directory: ".", externalPort: 8443, adminPort: 8444,
		deployment: deploymentContainer,
		image:      "ghcr.io/virtualbeingsresearch/coworker-relay:" + releaseTag(),
	}
}

func initDeployment(args []string) {
	options := defaultInitOptions()
	var err error
	if len(args) == 0 {
		if !isTerminal(os.Stdin) {
			fatal("interactive init requires a terminal; use --public-url for scripts")
		}
		options, err = promptInitOptions(os.Stdin, os.Stderr, options)
		if errors.Is(err, errInitCancelled) {
			fmt.Println("Initialization cancelled.")
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
	nextCommand := "docker compose up -d"
	credentialPath := filepath.Join(absolute, ".env")
	if options.deployment == deploymentNative {
		nextCommand = "coworker-relay serve"
	}
	fmt.Printf(
		"Relay deployment initialized in %s\n"+
			"Deployment: %s\n"+
			"Public URL: %s\n"+
			"Local administration: http://127.0.0.1:%d\n"+
			"Administrator token: saved only in %s\n\n"+
			"Next:\n  cd %s\n  %s\n"+
			"  coworker-relay health\n"+
			"  coworker-relay instance create --name home-coworker\n",
		absolute, options.deployment, normalizedURL, options.adminPort,
		credentialPath, absolute, nextCommand,
	)
}

func parseInitOptions(args []string) (initOptions, error) {
	options := defaultInitOptions()
	flags := flag.NewFlagSet("init", flag.ContinueOnError)
	flags.SetOutput(io.Discard)
	flags.StringVar(&options.directory, "dir", options.directory, "deployment directory")
	flags.StringVar(
		&options.publicURL, "public-url", "",
		"public HTTP(S) origin, for example http://203.0.113.10:8443",
	)
	flags.IntVar(&options.externalPort, "external-port", 8443, "published WebSocket port")
	flags.IntVar(&options.adminPort, "admin-port", 8444, "loopback administration port")
	flags.StringVar(
		&options.deployment, "deployment", options.deployment,
		"deployment type: container or native",
	)
	flags.StringVar(&options.image, "image", options.image, "container image")
	flags.BoolVar(&options.force, "force", false, "replace generated files")
	if err := flags.Parse(args); err != nil {
		return initOptions{}, err
	}
	if flags.NArg() != 0 {
		return initOptions{}, fmt.Errorf("unexpected init argument %q", flags.Arg(0))
	}
	if options.publicURL == "" {
		return initOptions{}, errors.New("--public-url is required for non-interactive init")
	}
	if err := validateDeployment(options.deployment); err != nil {
		return initOptions{}, err
	}
	return options, nil
}

func promptInitOptions(
	input io.Reader,
	output io.Writer,
	options initOptions,
) (initOptions, error) {
	fmt.Fprintln(output, "Coworker Relay setup")
	fmt.Fprintln(output, "The public endpoint carries opaque E2EE traffic; TLS is optional.")
	fmt.Fprintln(output, "Press Enter to accept a displayed default.")
	reader, err := newInitLineReader(input, output)
	if err != nil {
		return initOptions{}, err
	}
	options.directory, err = prompt(reader, output, "Deployment directory", options.directory)
	if err != nil {
		return initOptions{}, err
	}
	options.publicURL, err = requiredPrompt(
		reader, output,
		"Public origin (example: http://203.0.113.10:8443)",
	)
	if err != nil {
		return initOptions{}, err
	}
	external, err := prompt(
		reader, output, "Published WebSocket port", strconv.Itoa(options.externalPort),
	)
	if err != nil {
		return initOptions{}, err
	}
	options.externalPort, err = strconv.Atoi(external)
	if err != nil {
		return initOptions{}, errors.New("published port must be a number")
	}
	admin, err := prompt(
		reader, output, "Local administration port", strconv.Itoa(options.adminPort),
	)
	if err != nil {
		return initOptions{}, err
	}
	options.adminPort, err = strconv.Atoi(admin)
	if err != nil {
		return initOptions{}, errors.New("administration port must be a number")
	}
	useContainer, err := promptConfirm(reader, output, "Run Relay with Docker Compose?", true)
	if err != nil {
		return initOptions{}, err
	}
	if useContainer {
		options.deployment = deploymentContainer
		options.image, err = prompt(reader, output, "Container image", options.image)
		if err != nil {
			return initOptions{}, err
		}
	} else {
		options.deployment = deploymentNative
	}
	fmt.Fprintf(
		output,
		"\nDeployment summary:\n  Directory: %s\n  Public URL: %s\n"+
			"  Public port: %d\n  Local admin port: %d\n  Deployment: %s\n",
		options.directory, options.publicURL, options.externalPort,
		options.adminPort, options.deployment,
	)
	if options.deployment == deploymentContainer {
		fmt.Fprintf(output, "  Image: %s\n", options.image)
	}
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

func newInitLineReader(input io.Reader, output io.Writer) (initLineReader, error) {
	file, terminalInput := input.(*os.File)
	if !terminalInput || !isTerminal(file) {
		return &bufferedInitLineReader{reader: bufio.NewReader(input), output: output}, nil
	}
	terminal := term.NewTerminal(splitReadWriter{Reader: input, Writer: output}, "")
	prepare := func() (func(), error) {
		state, err := term.MakeRaw(int(file.Fd()))
		if err != nil {
			return nil, fmt.Errorf("enable terminal line editing: %w", err)
		}
		return func() { _ = term.Restore(int(file.Fd()), state) }, nil
	}
	return &terminalInitLineReader{terminal: terminal, prepare: prepare}, nil
}

func prompt(
	reader initLineReader,
	_ io.Writer,
	label string,
	defaultValue string,
) (string, error) {
	promptText := label + ": "
	if defaultValue != "" {
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

func requiredPrompt(reader initLineReader, output io.Writer, label string) (string, error) {
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
		value, err := reader.ReadLine(fmt.Sprintf("%s [%s]: ", label, defaultLabel))
		if err != nil {
			return false, err
		}
		switch strings.ToLower(strings.TrimSpace(value)) {
		case "":
			return defaultValue, nil
		case "y", "yes":
			return true, nil
		case "n", "no":
			return false, nil
		default:
			fmt.Fprintln(output, "Enter yes or no.")
		}
	}
}

func isTerminal(file *os.File) bool { return term.IsTerminal(int(file.Fd())) }

func initialize(options initOptions) (string, string, error) {
	if err := validateDeployment(options.deployment); err != nil {
		return "", "", err
	}
	if options.externalPort < 1 || options.externalPort > 65535 ||
		options.adminPort < 1 || options.adminPort > 65535 {
		return "", "", errors.New("ports must be between 1 and 65535")
	}
	if options.externalPort == options.adminPort {
		return "", "", errors.New("public and administration ports must differ")
	}
	_, normalizedURL, err := normalizePublicURL(options.publicURL, options.externalPort)
	if err != nil {
		return "", "", err
	}
	if options.deployment == deploymentContainer &&
		(options.image == "" || strings.ContainsAny(options.image, "\r\n")) {
		return "", "", errors.New("--image must not be empty or contain newlines")
	}
	if strings.TrimSpace(options.directory) == "" ||
		strings.ContainsAny(options.directory, "\r\n") {
		return "", "", errors.New("--dir must not be empty or contain newlines")
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
				return "", "", fmt.Errorf(
					"%s already exists; use --force to replace generated files", path,
				)
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
	listen := ":" + strconv.Itoa(options.externalPort)
	adminListen := "127.0.0.1:" + strconv.Itoa(options.adminPort)
	databasePath := filepath.Join(directory, "data", "relay.db")
	signingKeyPath := filepath.Join(directory, "data", "relay-signing.key")
	if options.deployment == deploymentContainer {
		listen = ":8443"
		adminListen = ":8444"
		databasePath = "/var/lib/coworker-relay/relay.db"
		signingKeyPath = "/var/lib/coworker-relay/relay-signing.key"
	}
	envLines := []string{
		"RELAY_PUBLIC_URL=" + normalizedURL,
		"RELAY_ADMIN_URL=http://127.0.0.1:" + strconv.Itoa(options.adminPort),
		"RELAY_ADMIN_TOKEN=" + adminToken,
		"RELAY_LISTEN=" + listen,
		"RELAY_ADMIN_LISTEN=" + adminListen,
		"RELAY_DATABASE=" + databasePath,
		"RELAY_SIGNING_KEY=" + signingKeyPath,
		"RELAY_CONNECTIONS_PER_MINUTE=60",
		"RELAY_FRAMES_PER_MINUTE=2400",
		"RELAY_AUTH_CONCURRENCY=32",
		"RELAY_BAN_FAILURE_LIMIT=5",
		"RELAY_BAN_FAILURE_WINDOW=10m",
		"RELAY_BAN_DURATION=1h",
		"RELAY_MAX_FRAME_BYTES=262144",
	}
	if options.deployment == deploymentContainer {
		envLines = append(
			envLines,
			"RELAY_EXTERNAL_PORT="+strconv.Itoa(options.externalPort),
			"RELAY_ADMIN_PORT="+strconv.Itoa(options.adminPort),
			"RELAY_IMAGE="+options.image,
		)
	}
	env := strings.Join(envLines, "\n") + "\n"
	compose := `services:
  relay:
    image: ${RELAY_IMAGE}
    restart: unless-stopped
    stop_grace_period: 35s
    env_file:
      - .env
    environment:
      RELAY_ADMIN_URL: http://127.0.0.1:8444
    ports:
      - "${RELAY_EXTERNAL_PORT}:8443"
      - "127.0.0.1:${RELAY_ADMIN_PORT}:8444"
    volumes:
      - relay-data:/var/lib/coworker-relay
    read_only: true
    pids_limit: 256
    ulimits:
      nofile:
        soft: 65536
        hard: 65536
    tmpfs:
      - /tmp
    healthcheck:
      test: ["CMD", "coworker-relay", "health"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 15s
    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "5"

volumes:
  relay-data:
`
	flagValue := os.O_WRONLY | os.O_CREATE
	if options.force {
		flagValue |= os.O_TRUNC
	} else {
		flagValue |= os.O_EXCL
	}
	if err := writeFile(envPath, []byte(env), 0o600, flagValue); err != nil {
		return "", "", err
	}
	if options.deployment == deploymentContainer {
		if err := writeFile(composePath, []byte(compose), 0o644, flagValue); err != nil {
			return "", "", err
		}
	} else if options.force {
		if err := os.Remove(composePath); err != nil && !errors.Is(err, os.ErrNotExist) {
			return "", "", err
		}
	}
	gitignore := ".env\n"
	if options.deployment == deploymentNative {
		gitignore += "data/\n"
	}
	if err := writeFile(gitignorePath, []byte(gitignore), 0o644, flagValue); err != nil {
		return "", "", err
	}
	return adminToken, normalizedURL, nil
}

func validateDeployment(value string) error {
	if value != deploymentContainer && value != deploymentNative {
		return errors.New("--deployment must be container or native")
	}
	return nil
}

func releaseTag() string {
	if buildinfo.Version == "" || buildinfo.Version == "dev" {
		return "latest"
	}
	return buildinfo.Version
}

func normalizePublicURL(raw string, externalPort int) (*url.URL, string, error) {
	parsed, err := url.Parse(strings.TrimRight(strings.TrimSpace(raw), "/"))
	if err != nil || (parsed.Scheme != "http" && parsed.Scheme != "https") ||
		parsed.Hostname() == "" || parsed.User != nil || parsed.Path != "" ||
		parsed.RawQuery != "" || parsed.Fragment != "" {
		return nil, "", errors.New("--public-url must be an HTTP(S) origin")
	}
	if parsed.Port() == "" {
		parsed.Host = net.JoinHostPort(parsed.Hostname(), strconv.Itoa(externalPort))
	} else if parsed.Port() != strconv.Itoa(externalPort) {
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

func writeFile(path string, body []byte, mode os.FileMode, flagValue int) error {
	file, err := os.OpenFile(path, flagValue, mode)
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
			return fmt.Errorf("invalid Relay configuration %s:%d", path, lineNumber+1)
		}
		if strings.HasPrefix(name, "RELAY_") {
			if _, exists := os.LookupEnv(name); !exists {
				if err := os.Setenv(name, value); err != nil {
					return err
				}
			}
		}
	}
	return nil
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
		c.print(
			"DELETE", "/_relay/v1/admin/instances/"+url.PathEscape(args[1]), nil, true,
		)
	default:
		usage()
	}
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
	force := flags.Bool("force", false, "preserve and replace an existing database")
	_ = flags.Parse(args)
	if *source == "" || *destination == "" {
		fatal("--from and --database are required")
	}
	sourcePath, sourceErr := filepath.Abs(*source)
	destinationPath, destinationErr := filepath.Abs(*destination)
	if sourceErr != nil || destinationErr != nil || sourcePath == destinationPath {
		fatal("backup and database must resolve to different paths")
	}
	if err := store.Validate(sourcePath); err != nil {
		fatal("backup validation failed: " + err.Error())
	}
	if _, err := os.Stat(destinationPath); err == nil {
		if !*force {
			fatal("destination exists; pass --force to preserve and replace it")
		}
		preserved := destinationPath + ".before-restore-" +
			time.Now().UTC().Format("20060102T150405Z")
		if err := os.Rename(destinationPath, preserved); err != nil {
			fatal(err.Error())
		}
		fmt.Fprintln(os.Stderr, "preserved previous database:", preserved)
	}
	input, err := os.Open(sourcePath)
	if err != nil {
		fatal(err.Error())
	}
	defer input.Close()
	output, err := os.OpenFile(destinationPath, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0o600)
	if err != nil {
		fatal(err.Error())
	}
	_, copyErr := io.Copy(output, input)
	closeErr := output.Close()
	if copyErr != nil || closeErr != nil {
		_ = os.Remove(destinationPath)
		if copyErr != nil {
			fatal(copyErr.Error())
		}
		fatal(closeErr.Error())
	}
	fmt.Println(destinationPath)
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
	var pretty bytes.Buffer
	if json.Indent(&pretty, raw, "", "  ") == nil {
		fmt.Println(pretty.String())
	} else {
		fmt.Println(strings.TrimSpace(string(raw)))
	}
}

func usage() {
	fmt.Fprintln(os.Stderr, `usage:
  coworker-relay serve
  coworker-relay init [--public-url <http-or-https-origin>] [--deployment container|native] [options]
  coworker-relay health
  coworker-relay version
  coworker-relay instance create [--name <name>]
  coworker-relay instance list
  coworker-relay instance revoke <instance_id>
  coworker-relay bans list [--instance <instance_id>]
  coworker-relay bans remove --instance <instance_id> --ip <ip> --reason <reason>
  coworker-relay backup [--output <path>]
  coworker-relay restore --from <backup.db> --database <stopped-relay.db> [--force]
  coworker-relay gc
  coworker-relay metrics`)
	os.Exit(2)
}

func fatal(message string) {
	fmt.Fprintln(os.Stderr, "coworker-relay:", message)
	os.Exit(1)
}
