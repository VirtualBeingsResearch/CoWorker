#!/bin/sh
set -eu

# /app is intentionally both the executable project root and the Agent's Git
# workspace. Mutable application state stays outside it and is linked back in
# near the end of this script.
workspace_path="${COWORKER_WORKSPACE_PATH:-/app}"
state_path="${COWORKER_STATE_PATH:-/var/lib/coworker}"

# Every release image contains a Git bundle and the exact revision/branch used
# to create it. Tests and advanced deployments may override these internal
# paths without changing the normal public configuration.
default_bundled_repository="${COWORKER_BUNDLED_REPOSITORY_PATH:-/opt/coworker/repository.bundle}"
bundled_repository="${COWORKER_REPOSITORY_BUNDLE:-$default_bundled_repository}"
repository_url="${COWORKER_REPOSITORY_URL-}"
repository_ref="${COWORKER_REPOSITORY_REF-}"
bundled_repository_url="${COWORKER_BUNDLED_REPOSITORY_URL-}"
bundled_repository_ref="${COWORKER_BUNDLED_REPOSITORY_REF-}"
repository_branch=""
workspace_parent="$(dirname "$workspace_path")"
image_workspace_marker="$workspace_path/.coworker-image-workspace"
image_revision_file="${COWORKER_IMAGE_REVISION_FILE:-/opt/coworker/repository.revision}"
image_branch_file="${COWORKER_IMAGE_BRANCH_FILE:-/opt/coworker/repository.branch}"
managed_workspace=false
replace_image_workspace=false

# The default embedded bundle supplies history for the source already copied
# from the image, so it must not discard local build-context differences.
# Explicit repository settings are different: they are a request to make that
# repository/ref authoritative, and therefore may replace the image source.
if [ -n "${COWORKER_REPOSITORY_BUNDLE-}" ] \
    || [ -n "$repository_url" ] \
    || [ -n "$repository_ref" ] \
    || [ -n "$bundled_repository_ref" ]; then
    replace_image_workspace=true
fi
case "$bundled_repository_url" in
    ""|https://github.com/VirtualBeingsResearch/CoWorker.git) ;;
    *) replace_image_workspace=true ;;
esac

mkdir -p "$workspace_parent" "$state_path"

if [ ! -e "$workspace_path/.git" ]; then
    image_workspace=false
    if [ -e "$workspace_path" ]; then
        [ -d "$workspace_path" ] || {
            echo "Workspace path is not a directory: $workspace_path" >&2
            exit 1
        }
        # Docker copies an image's VOLUME contents into a newly created named
        # volume before invoking ENTRYPOINT. The revision marker proves this
        # non-empty directory is that trusted image seed, not arbitrary user
        # data that the initializer might otherwise overwrite.
        if [ -f "$image_workspace_marker" ] \
            && [ -f "$image_revision_file" ] \
            && cmp -s "$image_workspace_marker" "$image_revision_file"; then
            image_workspace=true
        elif [ -n "$(ls -A "$workspace_path")" ]; then
            echo "Refusing to initialize a non-empty workspace without .git: $workspace_path" >&2
            exit 1
        else
            rmdir "$workspace_path"
        fi
    fi

    # Prepare Git metadata in a sibling directory. This keeps /app untouched
    # until cloning and ref selection have succeeded, so a failed initializer
    # never leaves a half-created repository behind.
    temporary_workspace="$(mktemp -d "$workspace_parent/.coworker-workspace.XXXXXX")"
    trap 'rm -rf "$temporary_workspace"' EXIT HUP INT TERM

    # Source selection precedence:
    #   1. an explicitly mounted bundle (works without network access),
    #   2. the bundle embedded in the image,
    #   3. an explicitly configured online repository.
    if [ -n "${COWORKER_REPOSITORY_BUNDLE-}" ]; then
        echo "Creating Git workspace from mounted repository bundle"
        git clone "$bundled_repository" "$temporary_workspace/repository"
    elif [ -z "$repository_url" ]; then
        echo "Creating Git workspace from embedded repository bundle"
        git clone "$bundled_repository" "$temporary_workspace/repository"
        if [ -z "$repository_ref" ]; then
            repository_ref="$(cat "$image_revision_file")"
            repository_branch="$(cat "$image_branch_file")"
        fi
        if [ -n "$bundled_repository_url" ]; then
            git -C "$temporary_workspace/repository" remote set-url \
                origin "$bundled_repository_url"
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

    if [ "$image_workspace" = true ]; then
        # Docker copies the image's /app tree into a new named volume. Attach
        # the bundle's Git metadata to that exact tree so the running source
        # and the Agent's editable workspace remain the same files. Preserve
        # the image source for the default bundle because a local image build
        # may differ from the configured upstream revision.
        mv "$temporary_workspace/repository/.git" "$workspace_path/.git"
        if [ "$replace_image_workspace" = true ]; then
            # An explicit repository choice is authoritative. reset + clean
            # makes every tracked and untracked file match the selected ref.
            git -C "$workspace_path" reset --hard
            git -C "$workspace_path" clean -dffx
        else
            # With the default bundle, keep the copied image files verbatim.
            # Removing the one-time marker also keeps a matching release tree
            # clean, which is required for safe automatic fast-forwards.
            rm -f "$image_workspace_marker"
        fi
    else
        # Empty custom workspace paths do not receive Docker copy-up content,
        # so the fully prepared clone can become the workspace atomically.
        mv "$temporary_workspace/repository" "$workspace_path"
    fi
    trap - EXIT HUP INT TERM
    rm -rf "$temporary_workspace"
    managed_workspace=true
else
    echo "Using existing Git workspace at $workspace_path"
fi

if [ "$managed_workspace" = true ]; then
    # Store ownership in local Git config rather than a tracked file. Existing
    # bind-mounted developer repositories never receive this flag and are
    # therefore never advanced by image-update automation.
    git -C "$workspace_path" config --local coworker.containerManaged true
fi

# A managed named volume survives image replacement. Fast-forward its clean
# bundled branch when a newer image contains a descendant revision, while
# preserving bind-mounted checkouts, dirty trees, local commits, and branches.
# Fetching from the local bundle keeps this path usable in the strict-offline
# image; merge-base checks distinguish behind, ahead, and divergent histories.
if [ -z "${COWORKER_REPOSITORY_BUNDLE-}" ] \
    && [ -z "$repository_url" ] \
    && [ "$(git -C "$workspace_path" config --bool --get coworker.containerManaged || true)" = true ] \
    && [ -s "$image_revision_file" ] \
    && [ -s "$image_branch_file" ]; then
    image_revision="$(cat "$image_revision_file")"
    image_branch="$(cat "$image_branch_file")"
    current_branch="$(git -C "$workspace_path" symbolic-ref --quiet --short HEAD || true)"
    if [ "$current_branch" = "$image_branch" ]; then
        if [ -n "$(git -C "$workspace_path" status --porcelain)" ]; then
            echo "Keeping locally modified workspace at $workspace_path"
        else
            git -C "$workspace_path" fetch "$bundled_repository" "$image_revision"
            if git -C "$workspace_path" merge-base --is-ancestor HEAD FETCH_HEAD; then
                if [ "$(git -C "$workspace_path" rev-parse HEAD)" != \
                    "$(git -C "$workspace_path" rev-parse FETCH_HEAD)" ]; then
                    echo "Fast-forwarding managed workspace to image revision $image_revision"
                    git -C "$workspace_path" merge --ff-only FETCH_HEAD
                fi
            elif ! git -C "$workspace_path" merge-base --is-ancestor FETCH_HEAD HEAD; then
                echo "Keeping divergent managed workspace at $workspace_path"
            fi
        fi
    fi
fi

# Coworker uses relative data/* paths. Point that single path at the separately
# persisted state volume so source-volume recreation or Git updates cannot
# remove identity, memory, inbox/outbox, logs, or administrator configuration.
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

mkdir -p "$workspace_path/.coworker/skills"

# uv resolves pyproject.toml and the editable project from the current working
# directory, so the default "uv run coworker" naturally executes /app's source.
cd "$workspace_path"
exec "$@"
