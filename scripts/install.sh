#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
hermes_home="${HERMES_HOME:-$HOME/.hermes}"
target="$hermes_home/plugins/model-telemetry"

install -d -m 755 "$target"
install -m 644 "$repo_root/plugins/model-telemetry/__init__.py" "$target/__init__.py"
install -m 644 "$repo_root/plugins/model-telemetry/plugin.yaml" "$target/plugin.yaml"
install -m 644 "$repo_root/plugins/model-telemetry/test_plugin.py" "$target/test_plugin.py"

printf 'Installed model-telemetry to %s\n' "$target"
