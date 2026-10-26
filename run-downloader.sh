#!/bin/sh
# Generic NAS entry point. Private values are loaded only from the local .env.
set -eu
project_dir=$(CDPATH= cd "$(dirname "$0")" && pwd)
if [ ! -f "$project_dir/.env" ]; then
    echo 'Missing local .env; copy .env.example and configure your NAS paths and UID/GID first.' >&2
    exit 1
fi
docker_bin=''
for candidate in /var/packages/ContainerManager/target/usr/bin/docker /usr/local/bin/docker /usr/bin/docker /bin/docker; do
    if [ -x "$candidate" ]; then
        docker_bin="$candidate"
        break
    fi
done
if [ -z "$docker_bin" ]; then
    docker_bin=$(command -v docker) || {
        echo 'Docker executable not found.' >&2
        exit 1
    }
fi
exec "$docker_bin" compose --env-file "$project_dir/.env" --project-directory "$project_dir" -p downloader -f "$project_dir/compose.yaml" up --no-build --no-color --no-log-prefix --abort-on-container-exit --exit-code-from downloader
