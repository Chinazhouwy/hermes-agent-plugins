#!/usr/bin/env bash
set -euo pipefail

# 无论从哪个目录执行脚本，都先定位到本 Git 仓库根目录。
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# 默认安装到 ~/.hermes；设置 HERMES_HOME 可安装到 weixin2 等指定 Profile。
hermes_home="${HERMES_HOME:-$HOME/.hermes}"
target="$hermes_home/plugins/model-telemetry"
skill_target="$hermes_home/skills/research/interview-practice"
skill_file="$skill_target/SKILL.md"
skill_was_immutable=0

restore_skill_immutable() {
  if [[ "$skill_was_immutable" -eq 1 ]]; then
    chattr +i "$skill_file"
  fi
}

if [[ -f "$skill_file" ]] && command -v lsattr >/dev/null 2>&1; then
  skill_attributes="$(lsattr "$skill_file" 2>/dev/null | awk '{print $1}')"
  if [[ "$skill_attributes" == *i* ]]; then
    chattr -i "$skill_file"
    skill_was_immutable=1
    trap restore_skill_immutable EXIT
  fi
fi

# 只复制运行插件所需文件，不会覆盖 Hermes 的 config.yaml 或凭据。
install -d -m 755 "$target"
install -m 644 "$repo_root/plugins/model-telemetry/__init__.py" "$target/__init__.py"
install -m 644 "$repo_root/plugins/model-telemetry/plugin.yaml" "$target/plugin.yaml"

install -d -m 755 "$skill_target/references"
install -m 644 "$repo_root/skills/interview-practice/SKILL.md" "$skill_file"
install -m 644 \
  "$repo_root/skills/interview-practice/references/hermes-plugin-hooks-guide.md" \
  "$skill_target/references/hermes-plugin-hooks-guide.md"
restore_skill_immutable
trap - EXIT

printf 'Installed model-telemetry to %s\n' "$target"
printf 'Installed interview-practice skill to %s\n' "$skill_target"
