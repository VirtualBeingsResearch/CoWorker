use std::{
    ffi::OsStr,
    io,
    path::{Path, PathBuf},
};
use tokio::process::Command;

#[cfg(windows)]
use std::ffi::OsString;

#[cfg(windows)]
const CREATE_NO_WINDOW: u32 = 0x08000000;

#[derive(Debug, Clone)]
pub struct ResolvedCommand {
    executable: PathBuf,
    display_path: PathBuf,
    path_entries: Vec<PathBuf>,
}

impl ResolvedCommand {
    pub fn command(&self) -> Command {
        let mut command = Command::new(self.executable());
        if !self.path_entries.is_empty() {
            let inherited = std::env::var_os("PATH")
                .map(|value| std::env::split_paths(&value).collect::<Vec<_>>())
                .unwrap_or_default();
            if let Ok(path) = std::env::join_paths(self.path_entries.iter().chain(inherited.iter()))
            {
                command.env("PATH", path);
            }
        }
        suppress_console_window(&mut command);
        command
    }

    pub fn executable(&self) -> &Path {
        &self.executable
    }

    pub fn display_path(&self) -> &Path {
        &self.display_path
    }

    fn direct(path: PathBuf) -> Self {
        let path_entries = path
            .parent()
            .filter(|parent| !parent.as_os_str().is_empty())
            .map(|parent| vec![parent.to_path_buf()])
            .unwrap_or_default();
        Self {
            executable: path.clone(),
            display_path: path,
            path_entries,
        }
    }

    #[cfg(not(windows))]
    fn with_path_entries(mut self, entries: impl IntoIterator<Item = PathBuf>) -> Self {
        for entry in entries {
            if !self.path_entries.contains(&entry) {
                self.path_entries.push(entry);
            }
        }
        self
    }
}

#[cfg(windows)]
fn suppress_console_window(command: &mut Command) {
    command.creation_flags(CREATE_NO_WINDOW);
}

#[cfg(not(windows))]
fn suppress_console_window(_command: &mut Command) {}

pub fn resolve_command(command: &str) -> io::Result<ResolvedCommand> {
    let command = command.trim();
    if command.is_empty() {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "command is empty",
        ));
    }

    #[cfg(windows)]
    {
        resolve_windows_command(command)
    }

    #[cfg(not(windows))]
    {
        Ok(resolve_unix_command(command))
    }
}

#[cfg(not(windows))]
fn resolve_unix_command(command: &str) -> ResolvedCommand {
    let path = Path::new(command);
    if is_path_like(command, path) {
        return ResolvedCommand::direct(PathBuf::from(command)).with_path_entries(
            default_unix_runtime_bin_paths(std::env::var_os("HOME").as_deref()),
        );
    }

    let resolved = resolve_unix_name(
        command,
        std::env::var_os("PATH").as_deref(),
        std::env::var_os("HOME").as_deref(),
    )
    .unwrap_or_else(|| PathBuf::from(command));
    ResolvedCommand::direct(resolved).with_path_entries(default_unix_runtime_bin_paths(
        std::env::var_os("HOME").as_deref(),
    ))
}

#[cfg(not(windows))]
fn resolve_unix_name(
    command: &str,
    path_value: Option<&OsStr>,
    home_value: Option<&OsStr>,
) -> Option<PathBuf> {
    std::env::split_paths(path_value.unwrap_or_else(|| OsStr::new("")))
        .map(|dir| dir.join(command))
        .find(|candidate| candidate.is_file())
        .or_else(|| {
            default_unix_codex_paths(command, home_value)
                .into_iter()
                .find(|candidate| candidate.is_file())
        })
}

#[cfg(not(windows))]
fn default_unix_codex_paths(command: &str, home_value: Option<&OsStr>) -> Vec<PathBuf> {
    if command != "codex" {
        return Vec::new();
    }
    let mut candidates = Vec::new();
    if let Some(home) = home_value {
        let home = PathBuf::from(home);
        candidates.push(home.join(".local").join("bin").join("codex"));
        candidates.push(home.join(".npm-global").join("bin").join("codex"));
        candidates.extend(
            nvm_bin_paths(&home)
                .into_iter()
                .map(|bin| bin.join("codex")),
        );
        #[cfg(target_os = "macos")]
        candidates.push(
            home.join("Applications")
                .join("Codex.app")
                .join("Contents")
                .join("Resources")
                .join("codex"),
        );
        #[cfg(target_os = "linux")]
        candidates.extend([
            home.join(".local")
                .join("share")
                .join("Codex")
                .join("resources")
                .join("codex"),
            home.join(".local")
                .join("share")
                .join("codex")
                .join("resources")
                .join("codex"),
        ]);
    }
    candidates.push(PathBuf::from("/opt/homebrew/bin/codex"));
    candidates.push(PathBuf::from("/usr/local/bin/codex"));
    #[cfg(target_os = "macos")]
    candidates.push(PathBuf::from(
        "/Applications/Codex.app/Contents/Resources/codex",
    ));
    #[cfg(target_os = "linux")]
    candidates.extend([
        PathBuf::from("/opt/Codex/resources/codex"),
        PathBuf::from("/opt/codex/resources/codex"),
        PathBuf::from("/usr/lib/codex/resources/codex"),
        PathBuf::from("/usr/share/codex/codex"),
    ]);
    candidates
}

#[cfg(not(windows))]
fn default_unix_runtime_bin_paths(home_value: Option<&OsStr>) -> Vec<PathBuf> {
    let mut paths = vec![
        PathBuf::from("/opt/homebrew/bin"),
        PathBuf::from("/usr/local/bin"),
    ];
    if let Some(home) = home_value {
        let home = PathBuf::from(home);
        paths.extend([
            home.join(".local").join("bin"),
            home.join(".npm-global").join("bin"),
            home.join(".volta").join("bin"),
            home.join(".asdf").join("shims"),
            home.join(".local").join("share").join("mise").join("shims"),
        ]);
        paths.extend(nvm_bin_paths(&home));
    }
    paths.retain(|path| path.is_dir());
    paths
}

#[cfg(not(windows))]
fn nvm_bin_paths(home: &Path) -> Vec<PathBuf> {
    let nvm_versions = home.join(".nvm").join("versions").join("node");
    let Ok(entries) = std::fs::read_dir(nvm_versions) else {
        return Vec::new();
    };
    let mut versioned = entries
        .flatten()
        .map(|entry| {
            let modified = entry
                .metadata()
                .and_then(|metadata| metadata.modified())
                .unwrap_or(std::time::UNIX_EPOCH);
            (modified, entry.path().join("bin"))
        })
        .collect::<Vec<_>>();
    versioned.sort_by(|left, right| right.0.cmp(&left.0));
    versioned.into_iter().map(|(_, path)| path).collect()
}

#[cfg(windows)]
fn resolve_windows_command(command: &str) -> io::Result<ResolvedCommand> {
    let path = Path::new(command);
    let resolved = if is_path_like(command, path) {
        resolve_windows_path(path)
    } else {
        resolve_windows_name(command)
    };

    resolved.map(ResolvedCommand::direct).ok_or_else(|| {
        io::Error::new(
            io::ErrorKind::NotFound,
            format!("command {command:?} was not found on PATH"),
        )
    })
}

#[cfg(windows)]
fn resolve_windows_path(path: &Path) -> Option<PathBuf> {
    if path.is_file() {
        return Some(path.to_path_buf());
    }
    if path.extension().is_some() {
        return None;
    }
    windows_extensions()
        .into_iter()
        .map(|extension| path.with_extension(extension.trim_start_matches('.')))
        .find(|candidate| candidate.is_file())
}

#[cfg(windows)]
fn resolve_windows_name(command: &str) -> Option<PathBuf> {
    let path_value = std::env::var_os("PATH");
    resolve_windows_name_in_path(command, path_value.as_deref()).or_else(|| {
        default_windows_codex_paths(
            command,
            std::env::var_os("LOCALAPPDATA").as_deref(),
            std::env::var_os("ProgramFiles").as_deref(),
        )
        .into_iter()
        .find(|candidate| candidate.is_file())
    })
}

#[cfg(windows)]
fn default_windows_codex_paths(
    command: &str,
    local_app_data: Option<&OsStr>,
    program_files: Option<&OsStr>,
) -> Vec<PathBuf> {
    if command != "codex" {
        return Vec::new();
    }
    let mut candidates = Vec::new();
    if let Some(local_app_data) = local_app_data {
        let local_app_data = PathBuf::from(local_app_data);
        let app_bins = local_app_data.join("OpenAI").join("Codex").join("bin");
        if let Ok(entries) = std::fs::read_dir(app_bins) {
            let mut versioned = entries
                .flatten()
                .map(|entry| {
                    let modified = entry
                        .metadata()
                        .and_then(|metadata| metadata.modified())
                        .unwrap_or(std::time::UNIX_EPOCH);
                    (modified, entry.path().join("codex.exe"))
                })
                .collect::<Vec<_>>();
            versioned.sort_by(|left, right| right.0.cmp(&left.0));
            candidates.extend(versioned.into_iter().map(|(_, path)| path));
        }
        candidates.push(
            local_app_data
                .join("Programs")
                .join("Codex")
                .join("resources")
                .join("codex.exe"),
        );
    }
    if let Some(program_files) = program_files {
        candidates.push(
            PathBuf::from(program_files)
                .join("Codex")
                .join("resources")
                .join("codex.exe"),
        );
    }
    candidates
}

#[cfg(windows)]
fn resolve_windows_name_in_path(command: &str, path_value: Option<&OsStr>) -> Option<PathBuf> {
    let names = windows_candidate_names(command);
    std::env::split_paths(path_value.unwrap_or_else(|| OsStr::new("")))
        .flat_map(|dir| names.iter().map(move |name| dir.join(name)))
        .find(|candidate| candidate.is_file())
}

#[cfg(windows)]
fn windows_candidate_names(command: &str) -> Vec<OsString> {
    let path = Path::new(command);
    if path.extension().is_some() {
        return vec![OsString::from(command)];
    }
    windows_extensions()
        .into_iter()
        .map(|extension| OsString::from(format!("{command}{extension}")))
        .collect()
}

#[cfg(windows)]
fn windows_extensions() -> Vec<String> {
    let mut extensions: Vec<String> = std::env::var_os("PATHEXT")
        .map(|value| {
            value
                .to_string_lossy()
                .split(';')
                .map(str::trim)
                .filter(|item| !item.is_empty())
                .map(normalize_extension)
                .collect()
        })
        .unwrap_or_else(|| {
            [".COM", ".EXE", ".BAT", ".CMD"]
                .into_iter()
                .map(String::from)
                .collect()
        });

    for extension in [".EXE", ".BAT", ".CMD"] {
        if !extensions
            .iter()
            .any(|current| current.eq_ignore_ascii_case(extension))
        {
            extensions.push(extension.into());
        }
    }
    extensions
}

#[cfg(windows)]
fn normalize_extension(extension: &str) -> String {
    if extension.starts_with('.') {
        extension.to_owned()
    } else {
        format!(".{extension}")
    }
}

#[cfg(windows)]
fn is_path_like(command: &str, path: &Path) -> bool {
    path.is_absolute() || command.contains('\\') || command.contains('/')
}

#[cfg(not(windows))]
fn is_path_like(command: &str, path: &Path) -> bool {
    path.is_absolute() || command.contains('/')
}

#[cfg(all(test, windows))]
mod tests {
    use super::*;
    use std::{
        fs,
        time::{SystemTime, UNIX_EPOCH},
    };

    #[test]
    fn resolves_bat_from_path_for_extensionless_command() {
        let dir = temp_dir("bat");
        let script = dir.join("codex.bat");
        fs::write(&script, "@echo off\r\n").unwrap();
        let path_value = std::env::join_paths([dir.as_path()]).unwrap();

        let resolved = resolve_windows_name_in_path("codex", Some(&path_value)).unwrap();

        assert_path_eq_ignore_case(&resolved, &script);
        let _ = fs::remove_dir_all(dir);
    }

    #[test]
    fn resolves_codex_from_desktop_app_bin() {
        let local_app_data = temp_dir("codex-app");
        let app_bin = local_app_data
            .join("OpenAI")
            .join("Codex")
            .join("bin")
            .join("build-id");
        fs::create_dir_all(&app_bin).unwrap();
        let executable = app_bin.join("codex.exe");
        fs::write(&executable, []).unwrap();
        let resolved = default_windows_codex_paths("codex", Some(local_app_data.as_os_str()), None)
            .into_iter()
            .find(|candidate| candidate.is_file())
            .unwrap();

        assert_path_eq_ignore_case(&resolved, &executable);
        let _ = fs::remove_dir_all(local_app_data);
    }

    fn temp_dir(name: &str) -> PathBuf {
        let millis = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_millis();
        let dir = std::env::temp_dir().join(format!(
            "coworker_desktop_{name}_{}_{}",
            std::process::id(),
            millis
        ));
        fs::create_dir_all(&dir).unwrap();
        dir
    }

    fn assert_path_eq_ignore_case(left: &Path, right: &Path) {
        assert_eq!(
            left.to_string_lossy().to_ascii_lowercase(),
            right.to_string_lossy().to_ascii_lowercase()
        );
    }
}

#[cfg(all(test, not(windows)))]
mod tests {
    use super::*;
    use std::{
        fs,
        time::{SystemTime, UNIX_EPOCH},
    };

    #[test]
    fn resolves_unix_command_from_path() {
        let dir = temp_dir("path");
        let script = dir.join("codex");
        fs::write(&script, "#!/bin/sh\n").unwrap();
        let path_value = std::env::join_paths([dir.as_path()]).unwrap();

        let resolved = resolve_unix_name("codex", Some(path_value.as_os_str()), None).unwrap();

        assert_eq!(resolved, script);
        let _ = fs::remove_dir_all(dir);
    }

    #[test]
    fn resolves_codex_from_home_local_bin_when_missing_from_path() {
        let home = temp_dir("home");
        let local_bin = home.join(".local").join("bin");
        fs::create_dir_all(&local_bin).unwrap();
        let script = local_bin.join("codex");
        fs::write(&script, "#!/bin/sh\n").unwrap();

        let resolved = resolve_unix_name("codex", None, Some(home.as_os_str())).unwrap();

        assert_eq!(resolved, script);
        let _ = fs::remove_dir_all(home);
    }

    #[test]
    fn does_not_use_home_local_bin_for_other_commands() {
        let home = temp_dir("home-other");
        let local_bin = home.join(".local").join("bin");
        fs::create_dir_all(&local_bin).unwrap();
        fs::write(local_bin.join("node"), "#!/bin/sh\n").unwrap();

        let resolved = resolve_unix_name("node", None, Some(home.as_os_str()));

        assert!(resolved.is_none());
        let _ = fs::remove_dir_all(home);
    }

    #[test]
    fn resolved_script_adds_its_bin_directory_to_path() {
        let home = temp_dir("path-env");
        let bin = home.join(".npm-global").join("bin");
        fs::create_dir_all(&bin).unwrap();
        let script = bin.join("codex");
        fs::write(&script, "#!/usr/bin/env node\n").unwrap();

        let resolved = resolve_unix_name("codex", None, Some(home.as_os_str())).unwrap();
        let command = ResolvedCommand::direct(resolved).command();
        let path = command
            .as_std()
            .get_envs()
            .find_map(|(name, value)| (name == "PATH").then_some(value.unwrap()))
            .unwrap();

        assert_eq!(std::env::split_paths(path).next().unwrap(), bin);
        let _ = fs::remove_dir_all(home);
    }

    fn temp_dir(name: &str) -> PathBuf {
        let millis = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_millis();
        let dir = std::env::temp_dir().join(format!(
            "coworker_desktop_unix_{name}_{}_{}",
            std::process::id(),
            millis
        ));
        fs::create_dir_all(&dir).unwrap();
        dir
    }
}
