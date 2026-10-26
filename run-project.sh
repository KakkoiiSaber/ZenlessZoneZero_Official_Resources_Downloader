#!/bin/sh
# Host-side entry point: synchronize Git before starting read-only containers.
set -eu

log() {
    printf '[%s][%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$1" "$2"
}

compose_download() {
    "$docker_bin" compose --env-file "$project_dir/.env" --project-directory "$project_dir" -f "$project_dir/compose.yaml" "$@"
}

compose_wiki() {
    "$docker_bin" compose --env-file "$project_dir/.env" --project-directory "$project_dir" -f "$project_dir/compose.wiki.yaml" "$@"
}

repo_git() {
    # DSM tasks run as root; keep Git files owned by the checkout's actual owner.
    if [ "$(id -u)" = 0 ] && [ "$git_owner" != 0 ]; then
        sudo -n -u "#$git_owner" env GIT_TERMINAL_PROMPT=0 GCM_INTERACTIVE=never "$git_bin" -C "$project_dir" "$@"
    else
        GIT_TERMINAL_PROMPT=0 GCM_INTERACTIVE=never "$git_bin" -C "$project_dir" "$@"
    fi
}

sync_code() {
    code_changed=0
    dependencies_changed=0
    if [ -n "$(compose_download ps --status running -q downloader)" ]; then
        log INFO 'Downloader is active; defer code synchronization until the next task.'
        return
    fi
    if [ "$(repo_git branch --show-current)" != main ]; then
        log WARNING 'Automatic synchronization requires main; keeping this checkout unchanged.'
        return
    fi
    if [ -n "$(repo_git status --porcelain --untracked-files=normal)" ]; then
        log WARNING 'Local repository changes found; keeping them and skipping synchronization.'
        return
    fi
    if ! repo_git fetch origin main; then
        log WARNING 'GitHub is unavailable; using the existing local code.'
        return
    fi
    if ! repo_git merge-base --is-ancestor HEAD origin/main; then
        log WARNING 'Local history has diverged; refusing to overwrite it.'
        return
    fi
    if ! repo_git diff --quiet HEAD origin/main -- src web compose.yaml compose.wiki.yaml Dockerfile requirements-download.txt .dockerignore; then
        code_changed=1
    fi
    if ! repo_git diff --quiet HEAD origin/main -- Dockerfile requirements-download.txt .dockerignore; then
        dependencies_changed=1
    fi
    if ! repo_git merge --ff-only origin/main; then
        log WARNING 'Fast-forward update failed; local changes have not been discarded.'
        code_changed=0
        dependencies_changed=0
        return
    fi
    log INFO "Repository ready: $(repo_git rev-parse --short HEAD)"
}

run() {
    mode=${1:-download}
    case "$mode" in download|wiki) ;; *) log ERROR 'Expected download or wiki.'; exit 2 ;; esac
    project_dir=$(CDPATH= cd "$(dirname "$0")" && pwd)
    if [ ! -f "$project_dir/.env" ]; then
        log ERROR 'Missing local .env; configure the NAS paths and UID/GID first.'
        exit 1
    fi
    docker_bin=$(command -v docker || true)
    if [ -x /var/packages/ContainerManager/target/usr/bin/docker ]; then
        docker_bin=/var/packages/ContainerManager/target/usr/bin/docker
    fi
    git_bin=$(command -v git || true)
    if [ -z "$git_bin" ]; then
        for candidate in /usr/local/bin/git /usr/bin/git; do
            if [ -x "$candidate" ]; then git_bin=$candidate; break; fi
        done
    fi
    if [ -z "$docker_bin" ] || [ -z "$git_bin" ]; then
        log ERROR 'Git and Docker must be installed and available to the task.'
        exit 1
    fi
    git_owner=$(stat -c %u "$project_dir/.git")
    mkdir -p "$project_dir/.cache"
    lock_dir="$project_dir/.cache/runner.lock"
    boot_id=$(cat /proc/sys/kernel/random/boot_id 2>/dev/null || printf unknown)
    if ! mkdir "$lock_dir" 2>/dev/null; then
        if [ ! -f "$lock_dir/pid" ]; then sleep 1; fi
        lock_pid=$(cat "$lock_dir/pid" 2>/dev/null || true)
        lock_boot=$(cat "$lock_dir/boot" 2>/dev/null || true)
        if [ "$lock_boot" = "$boot_id" ] && [ -n "$lock_pid" ] && kill -0 "$lock_pid" 2>/dev/null; then
            log INFO 'Another project task is active; skipping this invocation.'
            exit 0
        fi
        rm -f "$lock_dir/pid" "$lock_dir/boot"
        if ! rmdir "$lock_dir" 2>/dev/null || ! mkdir "$lock_dir" 2>/dev/null; then
            log INFO 'Another task acquired the update lock; skipping this invocation.'
            exit 0
        fi
    fi
    trap 'rm -f "$lock_dir/pid" "$lock_dir/boot"; rmdir "$lock_dir" 2>/dev/null || true' 0
    trap 'exit 130' INT
    trap 'exit 143' TERM
    printf '%s\n' "$$" > "$lock_dir/pid"
    printf '%s\n' "$boot_id" > "$lock_dir/boot"
    attempt=0
    until "$docker_bin" info >/dev/null 2>&1; do
        attempt=$((attempt + 1))
        if [ "$attempt" -ge 30 ]; then log ERROR 'Docker did not become ready.'; exit 1; fi
        sleep 2
    done
    wiki_running=$(compose_wiki ps --status running -q wiki)
    sync_code
    if [ "$mode" = download ] && [ -n "$(compose_download ps --status running -q downloader)" ]; then
        log INFO 'The downloader is already running; no second download is started.'
        exit 0
    fi
    signature=$(repo_git rev-parse HEAD:Dockerfile HEAD:requirements-download.txt HEAD:.dockerignore)
    previous=$(cat "$project_dir/.cache/runtime-signature" 2>/dev/null || true)
    if [ "$dependencies_changed" = 1 ] || { [ -n "$previous" ] && [ "$signature" != "$previous" ]; } || ! "$docker_bin" image inspect downloader:runtime >/dev/null 2>&1; then
        log INFO 'Preparing the dependency runtime image.'
        compose_download build downloader
        printf '%s\n' "$signature" > "$project_dir/.cache/runtime-signature.tmp"
        mv "$project_dir/.cache/runtime-signature.tmp" "$project_dir/.cache/runtime-signature"
        code_changed=1
    elif [ -z "$previous" ]; then
        # Adopt an existing runtime without rebuilding it for an ordinary code update.
        printf '%s\n' "$signature" > "$project_dir/.cache/runtime-signature"
    fi
    if [ "$mode" = wiki ]; then
        compose_wiki up -d --no-build --force-recreate wiki
    else
        if [ "$code_changed" = 1 ] && [ -n "$wiki_running" ]; then
            log INFO 'Reloading the Wiki with the updated code.'
            compose_wiki up -d --no-build --force-recreate wiki
        fi
        compose_download up --no-build --no-color --no-log-prefix --abort-on-container-exit --exit-code-from downloader downloader
    fi
}

# Parse the full function before a Git update can replace this script on disk.
run "$@"
