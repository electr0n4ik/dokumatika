#!/usr/bin/env bash
# Включить хуки из .githooks/ для этого клона репозитория.
#
# Хуки не переносятся вместе с репозиторием: git хранит их в .git/hooks,
# а этот каталог не версионируется. Поэтому они лежат в .githooks/ и
# подключаются одной командой — её нужно выполнить один раз после клонирования.
set -euo pipefail

root="$(git rev-parse --show-toplevel)"
cd "$root"

chmod +x .githooks/* 2>/dev/null || true
git config core.hooksPath .githooks

echo "Хуки включены: $(git config core.hooksPath)"
echo
echo "Что теперь происходит при git push:"
echo "  • прямой пуш в main блокируется (обход: ALLOW_MAIN_PUSH=1 git push)"
echo "  • перед отправкой прогоняются тесты"
echo
echo "Выключить: git config --unset core.hooksPath"
