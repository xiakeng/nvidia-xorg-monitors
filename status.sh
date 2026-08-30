#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
config_file="${MONITORS_SETUP_CONFIG:-$project_dir/config.toml}"

exec python3 "$project_dir/monitors_setup.py" status --config "$config_file" "$@"

