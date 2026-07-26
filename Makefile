# Команды разработки и эксплуатации Докуматики.
#
# Приложение работает на чистой стандартной библиотеке Python 3.10+: ставить
# ничего не нужно, чтобы запустить сервер. pytest / ruff / black — инструменты
# разработчика, на боевом сервере они не устанавливаются.
#
# Переменные окружения цели run/preflight/indexnow подхватывают из .env, если он
# есть (см. .env.example). Приложение само .env не читает — в проде его отдаёт
# systemd через EnvironmentFile.

# RU: Если системный python3 старее 3.10 — не правьте Makefile, передайте нужный
# интерпретатор: make run PYTHON=python3.12
PYTHON ?= python3
SRC    := src
APP    := app.server

# RU: Одинаковая преамбула для целей, которым нужны переменные из .env.
DOTENV = set -a; [ -f .env ] && . ./.env; set +a;

.DEFAULT_GOAL := help
.PHONY: help py-version run test lint fmt compile check preflight backup indexnow clean

# RU: Служебная цель без «##» — в help не показывается. Ловит самую обидную
# ошибку новичка: старый python3 в PATH и невнятный SyntaxError вместо причины.
py-version:
	@$(PYTHON) -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' || { \
		echo "Нужен Python 3.10+, а $(PYTHON) — это `$(PYTHON) -V 2>&1`."; \
		echo "Запустите так: make <цель> PYTHON=python3.12"; exit 1; }

help: ## Показать это меню
	@echo "Докуматика — доступные команды:"
	@echo
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  %-10s %s\n", $$1, $$2}'
	@echo
	@echo "Настройки — в .env (шаблон: .env.example). Стек — только stdlib."

run: py-version ## Запустить dev-сервер (адрес и порт из .env, по умолчанию 127.0.0.1:8080)
	@$(DOTENV) PYTHONPATH=$(SRC) $(PYTHON) -m $(APP)

test: py-version ## Прогнать тесты
	PYTHONPATH=$(SRC) $(PYTHON) -m pytest tests -q

lint: ## Проверить стиль и импорты (ruff)
	@command -v ruff >/dev/null || { echo "ruff не установлен: pip install ruff"; exit 1; }
	ruff check $(SRC) tests scripts

fmt: ## Отформатировать код (black)
	@command -v black >/dev/null || { echo "black не установлен: pip install black"; exit 1; }
	black $(SRC) tests scripts

compile: py-version ## Проверить синтаксис всех модулей без запуска
	$(PYTHON) -m compileall -q $(SRC) scripts

check: lint compile test ## Полная проверка перед коммитом: lint + синтаксис + тесты
	@echo "Проверки пройдены."

preflight: ## Проверка готовности к бою: реквизиты, ключи, права на var/
	@$(DOTENV) $(PYTHON) scripts/preflight.py

backup: ## Снять бэкап базы SQLite (var/backups/)
	@$(DOTENV) bash scripts/backup.sh

indexnow: ## Сообщить поисковикам об обновлённых страницах (IndexNow)
	@$(DOTENV) $(PYTHON) scripts/indexnow_ping.py

clean: ## Удалить кэши Python и промежуточные файлы
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	find . -type f -name '*.py[co]' -delete
	rm -rf .pytest_cache .ruff_cache
