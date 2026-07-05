#!/usr/bin/env bash
set -euo pipefail

# 无论从哪个目录执行脚本，都先定位到本 Git 仓库根目录。
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# 默认安装到 ~/.hermes；设置 HERMES_HOME 可安装到 weixin2 等指定 Profile。
hermes_home="${HERMES_HOME:-$HOME/.hermes}"
target="$hermes_home/plugins/model-telemetry"

# 只复制运行插件所需的三个文件，不会覆盖 Hermes 的 config.yaml 或凭据。
install -d -m 755 "$target"
install -m 644 "$repo_root/plugins/model-telemetry/__init__.py" "$target/__init__.py"
install -m 644 "$repo_root/plugins/model-telemetry/plugin.yaml" "$target/plugin.yaml"
install -m 644 "$repo_root/plugins/model-telemetry/test_plugin.py" "$target/test_plugin.py"

printf 'Installed model-telemetry to %s\n' "$target"
