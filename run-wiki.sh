#!/bin/sh
set -eu
exec /bin/sh "$(dirname "$0")/run-project.sh" wiki
