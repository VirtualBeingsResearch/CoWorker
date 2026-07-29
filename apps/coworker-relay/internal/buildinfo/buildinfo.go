package buildinfo

var (
	Version = "dev"
	Commit  = "unknown"
	Date    = "unknown"
)

func Values() map[string]string {
	return map[string]string{
		"version": Version,
		"commit":  Commit,
		"date":    Date,
	}
}
