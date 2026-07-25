#!/bin/sh
set -eu

workspace_path="${COWORKER_WORKSPACE_PATH:-/workspace/CoWorker}"
state_path="${COWORKER_STATE_PATH:-/var/lib/coworker}"
bundled_repository="${COWORKER_REPOSITORY_BUNDLE:-/opt/coworker/repository.bundle}"
repository_url="${COWORKER_REPOSITORY_URL-}"
repository_ref="${COWORKER_REPOSITORY_REF-}"
repository_branch=""
workspace_parent="$(dirname "$workspace_path")"

mkdir -p "$workspace_parent" "$state_path"

if [ ! -e "$workspace_path/.git" ]; then
    if [ -e "$workspace_path" ]; then
        [ -d "$workspace_path" ] || {
            echo "Workspace path is not a directory: $workspace_path" >&2
            exit 1
        }
        [ -z "$(ls -A "$workspace_path")" ] || {
            echo "Refusing to initialize a non-empty workspace without .git: $workspace_path" >&2
            exit 1
        }
        rmdir "$workspace_path"
    fi

    temporary_workspace="$(mktemp -d "$workspace_parent/.coworker-workspace.XXXXXX")"
    trap 'rm -rf "$temporary_workspace"' EXIT HUP INT TERM

    if [ -n "${COWORKER_REPOSITORY_BUNDLE-}" ]; then
        echo "Creating Git workspace from mounted repository bundle"
        git clone "$bundled_repository" "$temporary_workspace/repository"
    elif [ -z "$repository_url" ]; then
        echo "Creating Git workspace from embedded repository bundle"
        git clone "$bundled_repository" "$temporary_workspace/repository"
        if [ -z "$repository_ref" ]; then
            repository_ref="$(cat /opt/coworker/repository.revision)"
            repository_branch="$(cat /opt/coworker/repository.branch)"
        fi
        if [ -n "${COWORKER_BUNDLED_REPOSITORY_URL-}" ]; then
            git -C "$temporary_workspace/repository" remote set-url \
                origin "$COWORKER_BUNDLED_REPOSITORY_URL"
        fi
    else
        if [ "${COWORKER_REPOSITORY_OFFLINE:-0}" = "1" ]; then
            echo "Strict offline image refuses runtime repository network access" >&2
            exit 1
        fi
        case "$repository_url" in
            http://*:*@*|https://*:*@*)
                echo "Repository URLs must not contain credentials" >&2
                exit 1
                ;;
        esac
        echo "Cloning configured Git repository"
        git clone "$repository_url" "$temporary_workspace/repository"
    fi

    if [ -n "$repository_branch" ]; then
        git -C "$temporary_workspace/repository" checkout \
            -B "$repository_branch" "$repository_ref"
    elif [ -n "$repository_ref" ]; then
        git -C "$temporary_workspace/repository" checkout "$repository_ref"
    fi
    mv "$temporary_workspace/repository" "$workspace_path"
    trap - EXIT HUP INT TERM
    rmdir "$temporary_workspace"
else
    echo "Using existing Git workspace at $workspace_path"
fi

data_path="$workspace_path/data"
if [ -L "$data_path" ]; then
    [ "$(readlink -f "$data_path")" = "$(readlink -f "$state_path")" ] || {
        echo "Workspace data link does not point to configured state: $data_path" >&2
        exit 1
    }
elif [ -e "$data_path" ]; then
    [ -d "$data_path" ] && [ -z "$(ls -A "$data_path")" ] || {
        echo "Refusing to replace a non-empty workspace data path: $data_path" >&2
        exit 1
    }
    rmdir "$data_path"
    ln -s "$state_path" "$data_path"
else
    ln -s "$state_path" "$data_path"
fi

cd "$workspace_path"
exec "$@"
