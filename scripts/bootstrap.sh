#!/usr/bin/env bash
#
# Развёртывание Докуматики на чистом сервере ОДНОЙ командой.
#
#   bash scripts/bootstrap.sh dokumatika.ru [почта-для-letsencrypt] [-y]
#
# ЗАЧЕМ ЭТОТ СКРИПТ. Ручная инструкция (deployment/ROLLOUT.md) — это 13 шагов и
# больше сотни команд. Каждая может быть выполнена не в том порядке, с опечаткой
# в пути или пропущена, а узкое место проекта — не код, а время владельца.
# Здесь оставлено ровно то, что человек обязан сделать сам (купить домен,
# направить A-запись, пройти модерацию Robokassa, вписать свои реквизиты),
# всё остальное выполняется без него.
#
# ИДЕМПОТЕНТНОСТЬ. Скрипт рассчитан на повторный запуск и служит же обновлением
# кода. Ничего готового не затирает: .env сохраняется, недостающие ключи
# дописываются, секреты не перегенерируются, сертификат не перевыпускается.
#
# ЧЕГО СКРИПТ НЕ ДЕЛАЕТ. Не покупает домен, не правит DNS, не проходит модерацию
# и не придумывает за владельца его ИНН. Если A-запись ещё не смотрит на этот
# сервер, скрипт доведёт настройку до конца, честно скажет об этом и предложит
# повторить запуск — TLS выпустится со второго раза.

set -euo pipefail

# --------------------------------------------------------------- раскладка
APP_USER="dokumatika"
CODE_DIR="/opt/dokumatika"
DATA_DIR="/var/lib/dokumatika"
BACKUP_DIR="/var/backups/dokumatika"
ETC_DIR="/etc/dokumatika"
ENV_FILE="${ETC_DIR}/.env"
ACME_ROOT="/var/www/certbot"
SWAP_FILE="/swapfile"
APP_PORT="8081"
MIN_PY="3.10"

NGINX_SITE="/etc/nginx/sites-available/dokumatika.conf"
NGINX_ACME="/etc/nginx/sites-available/dokumatika-acme.conf"
NGINX_SNIPPET="/etc/nginx/snippets/dokumatika-security-headers.conf"

SCRIPT_PATH="$(readlink -f "${BASH_SOURCE[0]}")"
SOURCE_DIR="$(dirname "$(dirname "$SCRIPT_PATH")")"

DOMAIN=""
LE_EMAIL=""
LE_EMAIL_GIVEN=0
ASSUME_YES=0
TLS_READY=0
WWW_IN_CERT=0
DOMAIN_IPS=""
LOCAL_IPS=""

# ------------------------------------------------------------------- вывод
if [ -t 1 ]; then BOLD="$(printf '\033[1m')"; PLAIN="$(printf '\033[0m')"; else BOLD=""; PLAIN=""; fi

step() { printf '\n%s==> %s%s\n' "$BOLD" "$*" "$PLAIN"; }
info() { printf '    %s\n' "$*"; }
ok()   { printf '    [ok] %s\n' "$*"; }
warn() { printf '    [!]  %s\n' "$*"; }
die()  { printf '\n[ОШИБКА] %s\n\n' "$*" >&2; exit 1; }

usage() {
    cat <<'EOF'
Развёртывание Докуматики на чистом Debian 12 / Ubuntu 22.04+.

    bash scripts/bootstrap.sh <домен> [почта] [-y]

    <домен>  канонический домен без www и без схемы, например dokumatika.ru
    [почта]  адрес для уведомлений Let's Encrypt об истечении сертификата.
             По умолчанию admin@<домен>; письма туда приходят раз в год.
    -y       не спрашивать подтверждения (для повторных запусков и обновлений)

Запускать от root. Повторный запуск безопасен и служит обновлением кода.
EOF
}

# --------------------------------------------------------------- аргументы
while [ $# -gt 0 ]; do
    case "$1" in
        -y|--yes) ASSUME_YES=1 ;;
        -h|--help) usage; exit 0 ;;
        -*) usage; die "Неизвестный флаг: $1" ;;
        *)
            if [ -z "$DOMAIN" ]; then
                DOMAIN="$1"
            elif [ -z "$LE_EMAIL" ]; then
                LE_EMAIL="$1"
                LE_EMAIL_GIVEN=1
            else
                usage; die "Лишний аргумент: $1"
            fi
            ;;
    esac
    shift
done

if [ -z "$DOMAIN" ]; then
    usage
    die "Не передан домен. Пример: bash scripts/bootstrap.sh dokumatika.ru"
fi

DOMAIN="$(printf '%s' "$DOMAIN" | tr 'A-Z' 'a-z')"
case "$DOMAIN" in
    http*|*/*) die "Домен указывается без схемы и без слэша: dokumatika.ru, а не «$DOMAIN»." ;;
    www.*) die "Каноническим должен быть домен без www. Передайте ${DOMAIN#www.} — www настроится сам." ;;
esac
DOMAIN_RE='^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$'
if ! printf '%s' "$DOMAIN" | grep -qE "$DOMAIN_RE"; then
    die "«$DOMAIN» не похоже на доменное имя."
fi
[ -n "$LE_EMAIL" ] || LE_EMAIL="admin@${DOMAIN}"

# ------------------------------------------------------------ утилиты .env
# RU: Значение может содержать пробелы и спецсимволы sed, поэтому правку делает
# python3 (он всё равно обязателен для самого приложения), а не sed.
env_put() {
    python3 - "$ENV_FILE" "$1" "$2" <<'PY'
import pathlib
import sys

path, key, value = pathlib.Path(sys.argv[1]), sys.argv[2], sys.argv[3]
prefix = key + "="
lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
result, replaced = [], False
for line in lines:
    stripped = line.lstrip()
    if not stripped.startswith("#") and stripped.startswith(prefix):
        if not replaced:
            result.append(prefix + value)
            replaced = True
        continue
    result.append(line)
if not replaced:
    result.append(prefix + value)
path.write_text("\n".join(result) + "\n", encoding="utf-8")
PY
}

env_get() {
    [ -f "$ENV_FILE" ] || return 0
    awk -v key="$1" 'index($0, key "=") == 1 { sub(/^[^=]*=/, ""); print; exit }' "$ENV_FILE"
}

# env_default КЛЮЧ ЗНАЧЕНИЕ — записать, только если ключа нет или он пуст.
env_default() {
    local current
    current="$(env_get "$1")"
    [ -n "$current" ] || env_put "$1" "$2"
}

# gen_secret hex|urlsafe — криптостойкая строка для токена админки и IndexNow.
gen_secret() {
    if command -v python3 >/dev/null 2>&1; then
        if [ "$1" = "hex" ]; then
            python3 -c 'import secrets; print(secrets.token_hex(16))'
        else
            python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
        fi
    elif [ "$1" = "hex" ]; then
        openssl rand -hex 16
    else
        openssl rand -base64 48 | tr -d '=+/' | cut -c1-40
    fi
}

# version_ge A B — истина, если версия A не младше B.
version_ge() {
    if [ "$1" = "$2" ]; then
        return 0
    fi
    # RU: sed -n 1p, а не head: head закрывает конвейер досрочно, sort получает
    # SIGPIPE, и при pipefail это выглядит как ошибка сравнения версий.
    [ "$(printf '%s\n%s\n' "$1" "$2" | sort -V | sed -n 1p)" = "$2" ]
}

# resolve_a ИМЯ — список IPv4 через пробел; пусто, если имя не разрешается.
resolve_a() {
    getent ahostsv4 "$1" 2>/dev/null | awk '{print $1}' | sort -u | tr '\n' ' ' || true
}

find_first() {
    find /usr/lib/python3 /usr/share /usr/local/lib -name "$1" -type f 2>/dev/null | sed -n 1p || true
}

# ============================================================= 0. проверки
step "Проверяю, куда попал"

[ "$(id -u)" -eq 0 ] || die "Скрипт ставит пакеты и юниты systemd — запускайте от root (sudo -i)."

[ -r /etc/os-release ] || die "Нет /etc/os-release: не могу опознать систему."
# shellcheck disable=SC1091
. /etc/os-release
OS_ID="${ID:-unknown}"
OS_VER="${VERSION_ID:-0}"
case "$OS_ID" in
    debian)
        version_ge "$OS_VER" "12" || die "Нужен Debian 12 или новее, а здесь Debian ${OS_VER}."
        ;;
    ubuntu)
        version_ge "$OS_VER" "22.04" || die "Нужна Ubuntu 22.04 LTS или новее, а здесь Ubuntu ${OS_VER}."
        ;;
    *)
        case "${ID_LIKE:-}" in
            *debian*) warn "Система ${PRETTY_NAME:-$OS_ID} не Debian и не Ubuntu, но родственная — пробую." ;;
            *) die "Скрипт рассчитан на Debian 12 / Ubuntu 22.04+, а здесь ${PRETTY_NAME:-$OS_ID}." ;;
        esac
        ;;
esac
ok "Система: ${PRETTY_NAME:-$OS_ID $OS_VER}"

[ -f "${SOURCE_DIR}/src/app/server.py" ] \
    || die "Рядом со скриптом нет кода проекта: жду ${SOURCE_DIR}/src/app/server.py"
[ -d "${SOURCE_DIR}/deployment/systemd" ] \
    || die "Нет каталога ${SOURCE_DIR}/deployment/systemd — код выложен не целиком."
ok "Исходники: ${SOURCE_DIR}"

if [ -f "${SOURCE_DIR}/.env" ]; then
    warn "В исходниках есть .env — на сервер он НЕ копируется, секреты живут в ${ENV_FILE}"
fi

# ------------------------------------------------------------ подтверждение
cat <<EOF

Разворачиваю Докуматику:

    домен          ${DOMAIN} (и www.${DOMAIN})
    код            ${CODE_DIR}
    база           ${DATA_DIR}
    настройки      ${ENV_FILE}
    копии базы     ${BACKUP_DIR}
    пользователь   ${APP_USER} — системный, без shell и без пароля
    порт           127.0.0.1:${APP_PORT}, наружу смотрит nginx
    почта для TLS  ${LE_EMAIL}

Будут установлены пакеты (nginx, certbot, sqlite3), создан swap 2 ГБ,
включён файрвол ufw (ssh останется открытым), поставлены юниты systemd,
включён ежедневный бэкап и выпущен сертификат Let's Encrypt.

Повторный запуск безопасен: настройки и секреты сохраняются.

EOF

if [ "$ASSUME_YES" -eq 0 ]; then
    [ -t 0 ] || die "Нет терминала для подтверждения. Если уверены — добавьте флаг -y."
    printf 'Продолжаем? [y/N]: '
    read -r ANSWER
    case "$ANSWER" in
        [yYдД]*) ;;
        *) printf '\nОтменено, ничего не менял.\n'; exit 1 ;;
    esac
fi

# ============================================================== 1. пакеты
step "Ставлю пакеты"

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq || die "apt-get update не прошёл — проверьте сеть и /etc/apt/sources.list"
info "обновляю уже установленное, это может занять несколько минут"
apt-get -y -qq -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold upgrade >/dev/null \
    || warn "apt-get upgrade завершился с ошибкой — продолжаю, но разберитесь позже"
apt-get install -y -qq nginx python3 sqlite3 certbot python3-certbot-nginx \
                       rsync curl ca-certificates ufw cron openssl >/dev/null \
    || die "Не удалось установить пакеты. Повторите: apt-get install nginx python3 certbot"
ok "пакеты на месте"

PY_VER="$(python3 -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])')"
if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'; then
    die "Python ${PY_VER}, а проекту нужен ${MIN_PY}+.
Так бывает на Debian 11 и Ubuntu 20.04. Обновите систему: на Debian 12 и
Ubuntu 22.04+ подходящий Python идёт в базовой поставке, ставить ничего не надо."
fi
ok "Python ${PY_VER}"

# =============================================================== 2. файрвол
step "Закрываю сервер файрволом"

# RU: SSH разрешаем ПЕРВЫМ и по фактическому порту подключения — иначе
# `ufw enable` по ssh отрезает администратора от собственного сервера.
ufw allow OpenSSH >/dev/null 2>&1 || ufw allow 22/tcp >/dev/null
SSH_PORT="$(printf '%s' "${SSH_CONNECTION:-}" | awk '{print $4}')"
if [ -n "$SSH_PORT" ] && [ "$SSH_PORT" != "22" ]; then
    ufw allow "${SSH_PORT}/tcp" >/dev/null
    ok "ssh на нестандартном порту ${SSH_PORT} тоже разрешён"
fi
ufw allow 80/tcp >/dev/null
ufw allow 443/tcp >/dev/null
ufw --force enable >/dev/null
ok "ufw включён: открыты ssh, 80, 443"

# ================================================================ 3. память
step "Настраиваю память и своп"

SWAP_LINES="$(swapon --show --noheadings 2>/dev/null | wc -l || true)"
if [ "${SWAP_LINES:-0}" -gt 0 ]; then
    ok "своп уже есть — не трогаю"
elif [ -e "$SWAP_FILE" ]; then
    warn "файл ${SWAP_FILE} есть, но не подключён — включаю"
    swapon "$SWAP_FILE" 2>/dev/null || warn "включить ${SWAP_FILE} не вышло, проверьте вручную"
else
    # RU: На 4 ГБ без свопа любой всплеск (apt upgrade рядом с работающими
    # сайтами) заканчивается тем, что OOM killer убивает не виновника, а самый
    # жирный процесс — обычно чужой сайт. Своп здесь страховка, а не память.
    info "создаю 2 ГБ свопа в ${SWAP_FILE}"
    if ! fallocate -l 2G "$SWAP_FILE" 2>/dev/null; then
        dd if=/dev/zero of="$SWAP_FILE" bs=1M count=2048 status=none
    fi
    chmod 600 "$SWAP_FILE"
    if mkswap "$SWAP_FILE" >/dev/null 2>&1 && swapon "$SWAP_FILE" 2>/dev/null; then
        grep -qF "$SWAP_FILE" /etc/fstab || printf '%s none swap sw 0 0\n' "$SWAP_FILE" >> /etc/fstab
        ok "своп 2 ГБ подключён и прописан в /etc/fstab"
    else
        rm -f "$SWAP_FILE"
        # RU: В контейнерах (LXC, OpenVZ) собственный своп завести нельзя. Это не
        # повод прерывать выкатку: сайту хватит и без него, страдает только запас.
        warn "ядро не дало включить своп (обычно это контейнер) — продолжаю без него"
    fi
fi

cat > /etc/sysctl.d/60-webserver.conf <<'EOF'
# Своп — аварийный запас, а не рабочий режим: вытесняем только под настоящим
# давлением. 0 отключил бы механизм совсем, 60 (по умолчанию) начал бы
# выпихивать в диск страницы простаивающих сайтов.
vm.swappiness = 10
# Дольше держим кэш каталогов и inode: сайты читают одни и те же файлы статики.
vm.vfs_cache_pressure = 50
# Очередь ожидающих соединений — дефолтных 128 мало при всплеске трафика.
net.core.somaxconn = 1024
EOF
sysctl --system >/dev/null
ok "vm.swappiness=10, vm.vfs_cache_pressure=50, net.core.somaxconn=1024"

# =============================================================== 4. журналы
step "Ограничиваю журналы"

# RU: По умолчанию journald занимает до 10% диска — на маленьком VPS это десятки
# гигабайт, и однажды на них кончится место под базу.
install -d -m 0755 /etc/systemd/journald.conf.d
cat > /etc/systemd/journald.conf.d/limits.conf <<'EOF'
[Journal]
Storage=persistent
Compress=yes
SystemMaxUse=300M
SystemMaxFileSize=50M
RuntimeMaxUse=64M
MaxRetentionSec=1month
EOF
systemctl restart systemd-journald
ok "journald: не больше 300 МБ, хранение месяц"

# ================================================= 5. пользователь и каталоги
step "Создаю пользователя и каталоги"

if id -u "$APP_USER" >/dev/null 2>&1; then
    ok "пользователь ${APP_USER} уже есть"
else
    useradd --system --no-create-home --shell /usr/sbin/nologin --home-dir "$DATA_DIR" "$APP_USER"
    ok "создан системный пользователь ${APP_USER} (без shell и без пароля)"
fi

# RU: Код принадлежит root, а сервис работает от dokumatika и не имеет права
# записи в собственный каталог: подменить файлы сайта не выйдет даже при
# удалённом выполнении кода. Каталог бэкапов создаём здесь, потому что при
# ProtectSystem=strict сервис бэкапа создать его себе не сможет.
install -d -o root        -g root        -m 0755 "$CODE_DIR"
install -d -o "$APP_USER" -g "$APP_USER" -m 0750 "$DATA_DIR"
install -d -o "$APP_USER" -g "$APP_USER" -m 0750 "$BACKUP_DIR"
install -d -o root        -g "$APP_USER" -m 0750 "$ETC_DIR"
install -d -o root        -g root        -m 0755 "$ACME_ROOT"
ok "каталоги: ${CODE_DIR}, ${DATA_DIR}, ${BACKUP_DIR}, ${ETC_DIR}"

# =================================================================== 6. код
step "Выкладываю код"

if [ "$SOURCE_DIR" = "$CODE_DIR" ]; then
    ok "код уже в ${CODE_DIR} — только выставляю права"
else
    rsync -a --delete \
          --exclude '.git' --exclude '.env' --exclude 'var' \
          --exclude '__pycache__' --exclude '*.pyc' \
          --exclude '.pytest_cache' --exclude '.ruff_cache' \
          "${SOURCE_DIR}/" "${CODE_DIR}/"
    ok "код скопирован в ${CODE_DIR}"
fi

# RU: 0755/0644 нужны и nginx — он отдаёт статику прямо с диска из src/static.
chown -R root:root "$CODE_DIR"
find "$CODE_DIR" -type d -exec chmod 0755 {} +
find "$CODE_DIR" -type f -exec chmod 0644 {} +
for executable in backup.sh bootstrap.sh smoke_prod.sh; do
    if [ -f "${CODE_DIR}/scripts/${executable}" ]; then
        chmod 0755 "${CODE_DIR}/scripts/${executable}"
    fi
done
ok "права выставлены"

# ========================================================== 7. файл окружения
step "Готовлю ${ENV_FILE}"

if [ ! -s "$ENV_FILE" ]; then
    cat > "$ENV_FILE" <<EOF
# Боевые настройки Докуматики. Формат systemd EnvironmentFile: без кавычек
# вокруг значений, без подстановок \$VAR, комментарии с #.
#
# Файл создан scripts/bootstrap.sh. Повторный запуск bootstrap его не затирает:
# дописывает недостающие ключи и обновляет домен, пути и версию статики.
# После любой правки:  systemctl restart dokumatika

# ────────────────────────────────── сайт ──────────────────────────────────
SITE_DOMAIN=${DOMAIN}
SITE_SCHEME=https
# Пусто = подставится SELLER_EMAIL.
SUPPORT_EMAIL=

# ─────────────────── реквизиты продавца — ЗАПОЛНИТЬ РУКАМИ ────────────────
# Robokassa проверяет эти данные в подвале сайта и в оферте. Пока пусто —
# preflight.py не пропускает выкатку, а оферта показывает предупреждение.
# Данные обязаны совпадать с «Мой налог» / ЕГРИП; выдумывать ничего нельзя.
SELLER_LEGAL_FORM=Самозанятый
SELLER_NAME=
SELLER_INN=
# ОГРН/ОГРНИП. У самозанятого-физлица его нет — оставьте пустым.
SELLER_OGRN=
SELLER_EMAIL=
# Для самозанятого достаточно города, домашний адрес публиковать не нужно.
SELLER_ADDRESS=

# ────────────────── Robokassa — ЗАПОЛНИТЬ ПОСЛЕ МОДЕРАЦИИ ──────────────────
# «Мои магазины» → «Технические настройки». Адреса для кабинета:
#   ResultURL  https://${DOMAIN}/robokassa/result  (метод POST)
#   SuccessURL https://${DOMAIN}/oplata/uspeh/     (метод GET)
#   FailURL    https://${DOMAIN}/oplata/otmena/    (метод GET)
ROBOKASSA_MERCHANT_LOGIN=
ROBOKASSA_PASSWORD1=
ROBOKASSA_PASSWORD2=
ROBOKASSA_TEST_PASSWORD1=
ROBOKASSA_TEST_PASSWORD2=
# 1 — тестовый контур, деньги не списываются. Забыть вернуть 0 значит раздавать
# комплект даром, поэтому в админке на этот случай висит красный баннер.
ROBOKASSA_TEST_MODE=1
# ОБЯЗАН совпадать с алгоритмом, выбранным в кабинете Robokassa. В кабинете по
# умолчанию стоит MD5, здесь — sha256, поэтому в кабинете его НУЖНО переключить.
# Расхождение даёт на платёжной странице ошибку 29 и больше ничего не сообщает.
ROBOKASSA_HASH_ALGORITHM=sha256
# Товар цифровой и передаётся сразу после оплаты; самозанятый НДС не платит.
ROBOKASSA_RECEIPT_PAYMENT_METHOD=full_payment
ROBOKASSA_RECEIPT_PAYMENT_OBJECT=service
ROBOKASSA_RECEIPT_TAX=none

# ───────────── почта (необязательно: ссылка на заказ и так на экране) ──────
SMTP_HOST=
SMTP_PORT=587
SMTP_USER=
SMTP_PASSWORD=
SMTP_SENDER=
SMTP_USE_TLS=1

# ───────────────────────────────── служебное ──────────────────────────────
PAYMENTS_ENABLED=1
APP_DEBUG=0
MAINTENANCE=0

# ────────────────────── аналитика и индексация (можно позже) ───────────────
# Счётчик Метрики требует ослабить CSP — см. deployment/nginx/security-headers.conf.
METRIKA_ID=
EOF
    ok "создан новый ${ENV_FILE}"
else
    ok "${ENV_FILE} уже есть — сохраняю его и дописываю только недостающее"
fi

chown root:"$APP_USER" "$ENV_FILE"
chmod 0640 "$ENV_FILE"

# RU: Секреты генерируем за владельца: придуманный человеком токен почти всегда
# короче и предсказуемее, а по этому токену видны почты всех покупателей.
if [ -z "$(env_get ADMIN_TOKEN)" ]; then
    env_put ADMIN_TOKEN "$(gen_secret urlsafe)"
    ok "сгенерирован ADMIN_TOKEN"
else
    ok "ADMIN_TOKEN уже задан — оставляю прежний"
fi
if [ -z "$(env_get INDEXNOW_KEY)" ]; then
    env_put INDEXNOW_KEY "$(gen_secret hex)"
    ok "сгенерирован INDEXNOW_KEY"
else
    ok "INDEXNOW_KEY уже задан — оставляю прежний"
fi

# RU: Пути обязаны быть именно в .env, а не только в юните: backup.sh,
# preflight.py и reconcile_payments.py запускаются отдельно и юнита не видят.
# Без DATABASE_PATH здесь сверка платежей пошла бы искать базу рядом с кодом,
# не нашла бы там заказов и промолчала — зависший платёж остался бы незамеченным.
env_put SITE_DOMAIN "$DOMAIN"
env_put SITE_SCHEME "https"
env_put APP_HOST "127.0.0.1"
env_put APP_PORT "$APP_PORT"
env_put DATABASE_PATH "${DATA_DIR}/dokumatika.sqlite3"
env_put STATIC_ROOT "${CODE_DIR}/src/static"
env_put BACKUP_DIR "$BACKUP_DIR"
env_default BACKUP_KEEP "14"
# RU: Статика кэшируется у посетителя на год по версионированному URL. Раз
# bootstrap — это ещё и обновление кода, версию двигаем на каждой выкатке:
# так забыть про неё физически невозможно.
env_put ASSET_VERSION "$(date -u '+%Y%m%d-%H%M')"
if [ "$LE_EMAIL_GIVEN" -eq 1 ]; then
    env_default SUPPORT_EMAIL "$LE_EMAIL"
fi
ok "домен, пути и версия статики записаны"

ADMIN_TOKEN_VALUE="$(env_get ADMIN_TOKEN)"

# ================================================================ 8. systemd
step "Ставлю юниты systemd"

for unit in apps.slice dokumatika.service dokumatika-backup.service dokumatika-backup.timer; do
    install -m 0644 "${CODE_DIR}/deployment/systemd/${unit}" "/etc/systemd/system/${unit}"
done
systemctl daemon-reload
systemctl enable dokumatika.service >/dev/null 2>&1 || true
systemctl restart dokumatika.service
systemctl enable --now dokumatika-backup.timer >/dev/null 2>&1 || true
ok "сервис и таймер ежедневного бэкапа включены"

info "жду ответа приложения на 127.0.0.1:${APP_PORT}"
APP_UP=0
for _ in $(seq 1 30); do
    if curl -fsS --max-time 2 "http://127.0.0.1:${APP_PORT}/healthz" >/dev/null 2>&1; then
        APP_UP=1
        break
    fi
    sleep 1
done
if [ "$APP_UP" -eq 1 ]; then
    ok "приложение отвечает, база создана"
else
    systemctl status dokumatika --no-pager --lines 20 || true
    die "Приложение не поднялось за 30 секунд. Подробности: journalctl -u dokumatika -n 50 --no-pager"
fi

# RU: Сверка зависших платежей — страховка на случай, когда ResultURL не дошёл
# (сервер лежал, сертификат протух, сменились адреса Robokassa). Пока Robokassa
# не настроена, скрипт честно пишет об этом в журнал: journalctl -t dokumatika-reconcile.
cat > /etc/cron.d/dokumatika-reconcile <<EOF
# Сверка зависших платежей с Robokassa. Установлено scripts/bootstrap.sh.
*/15 * * * * ${APP_USER} /usr/bin/python3 ${CODE_DIR}/scripts/reconcile_payments.py --env-file ${ENV_FILE} 2>&1 | /usr/bin/logger -t dokumatika-reconcile
EOF
chmod 0644 /etc/cron.d/dokumatika-reconcile
ok "сверка платежей поставлена в cron, раз в 15 минут"

# ======================================================= 9. сертификат TLS
step "Настраиваю nginx и получаю сертификат"

install -d -m 0755 /etc/nginx/snippets
install -m 0644 "${CODE_DIR}/deployment/nginx/security-headers.conf" "$NGINX_SNIPPET"
ok "заголовки безопасности разложены в ${NGINX_SNIPPET}"

CERT_DIR="/etc/letsencrypt/live/${DOMAIN}"
LOCAL_IPS="$(hostname -I 2>/dev/null || true)"

if [ -s "${CERT_DIR}/fullchain.pem" ]; then
    TLS_READY=1
    # RU: Смотрим, попал ли www в уже выпущенный сертификат: от этого зависит,
    # откроется ли редирект с www без предупреждения браузера.
    if openssl x509 -in "${CERT_DIR}/fullchain.pem" -noout -text 2>/dev/null \
        | grep -q "DNS:www.${DOMAIN}"; then
        WWW_IN_CERT=1
    fi
    ok "сертификат уже выпущен, продление обслуживает таймер certbot"
else
    # RU: Боевой конфиг ссылается на сертификат, которого ещё нет, и `nginx -t`
    # на нём упадёт. Поэтому сначала поднимаем минимальный конфиг под ACME.
    cat > "$NGINX_ACME" <<EOF
# Временный конфиг только для выпуска сертификата. Ставится scripts/bootstrap.sh
# и удаляется им же сразу после certbot.
server {
    listen 80;
    listen [::]:80;
    server_name ${DOMAIN} www.${DOMAIN};
    location ^~ /.well-known/acme-challenge/ { root ${ACME_ROOT}; }
    location / { return 404; }
}
EOF
    chmod 0644 "$NGINX_ACME"
    ln -sf "$NGINX_ACME" /etc/nginx/sites-enabled/dokumatika-acme.conf
    if ! nginx -t >/dev/null 2>&1; then
        nginx -t || true
        rm -f /etc/nginx/sites-enabled/dokumatika-acme.conf
        die "nginx не принял временный конфиг (вывод выше). Соседние сайты не тронуты."
    fi
    systemctl reload nginx
    ok "домен готов отвечать на проверку Let's Encrypt"

    DOMAIN_IPS="$(resolve_a "$DOMAIN")"
    WWW_IPS="$(resolve_a "www.${DOMAIN}")"

    if [ -z "$DOMAIN_IPS" ]; then
        warn "${DOMAIN} не разрешается в IP-адрес — сертификат выпускать не из чего"
    else
        MATCHED=0
        for ip in $DOMAIN_IPS; do
            case " $LOCAL_IPS " in *" $ip "*) MATCHED=1 ;; esac
        done
        if [ "$MATCHED" -eq 1 ]; then
            ok "A-запись ${DOMAIN} указывает на этот сервер"
        else
            # RU: Не повод останавливаться: за NAT и с плавающим адресом
            # локальные IP с публичными не совпадают. Проверит сам ACME.
            warn "A-запись ${DOMAIN} → ${DOMAIN_IPS}, адреса машины: ${LOCAL_IPS:-неизвестны}"
            warn "если сервер за NAT, это нормально — всё равно пробую выпустить сертификат"
        fi

        CERT_ARGS=(-d "$DOMAIN")
        if [ -n "$WWW_IPS" ]; then
            CERT_ARGS+=(-d "www.${DOMAIN}")
        else
            warn "www.${DOMAIN} не разрешается — выпускаю сертификат только на ${DOMAIN}"
        fi

        if certbot certonly --webroot -w "$ACME_ROOT" "${CERT_ARGS[@]}" \
                --email "$LE_EMAIL" --agree-tos --no-eff-email \
                --non-interactive --keep-until-expiring; then
            TLS_READY=1
            if [ -n "$WWW_IPS" ]; then
                WWW_IN_CERT=1
            fi
            ok "сертификат выпущен"
        else
            warn "certbot не смог подтвердить владение доменом"
        fi
    fi
fi

if [ "$TLS_READY" -eq 1 ]; then
    # RU: `certbot certonly`, в отличие от установщика nginx, свои ssl-параметры
    # не раскладывает — а конфиг сайта их подключает, и `nginx -t` упал бы.
    if [ ! -f /etc/letsencrypt/options-ssl-nginx.conf ]; then
        SRC_OPTS="$(find_first 'options-ssl-nginx.conf')"
        if [ -n "$SRC_OPTS" ]; then
            install -m 0644 "$SRC_OPTS" /etc/letsencrypt/options-ssl-nginx.conf
        else
            cat > /etc/letsencrypt/options-ssl-nginx.conf <<'EOF'
# Параметры TLS (набор Let's Encrypt). Файл создан scripts/bootstrap.sh, потому
# что `certbot certonly` свой вариант не раскладывает, а конфиг сайта его ждёт.
ssl_session_cache shared:le_nginx_SSL:10m;
ssl_session_timeout 1440m;
ssl_session_tickets off;
ssl_protocols TLSv1.2 TLSv1.3;
ssl_prefer_server_ciphers off;
ssl_ciphers "ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256:ECDHE-ECDSA-AES256-GCM-SHA384:ECDHE-RSA-AES256-GCM-SHA384:ECDHE-ECDSA-CHACHA20-POLY1305:ECDHE-RSA-CHACHA20-POLY1305:DHE-RSA-AES128-GCM-SHA256:DHE-RSA-AES256-GCM-SHA384";
EOF
        fi
        ok "параметры TLS для nginx разложены"
    fi
    if [ ! -f /etc/letsencrypt/ssl-dhparams.pem ]; then
        SRC_DH="$(find_first 'ssl-dhparams.pem')"
        if [ -n "$SRC_DH" ]; then
            install -m 0644 "$SRC_DH" /etc/letsencrypt/ssl-dhparams.pem
        else
            info "генерирую параметры Диффи-Хеллмана, до минуты"
            openssl dhparam -out /etc/letsencrypt/ssl-dhparams.pem 2048 >/dev/null 2>&1 \
                || warn "openssl dhparam не отработал — проверьте /etc/letsencrypt/ssl-dhparams.pem"
            chmod 0644 /etc/letsencrypt/ssl-dhparams.pem 2>/dev/null || true
        fi
        ok "ssl-dhparams.pem на месте"
    fi
fi

# ================================================ 10. боевой конфиг nginx
step "Ставлю боевой конфиг nginx"

if [ "$TLS_READY" -eq 1 ]; then
    # RU: В шаблоне домен зашит как dokumatika.ru, но только там, где это
    # действительно домен: имена зон, upstream и путей начинаются с «dokumatika»
    # без «.ru», поэтому замена по точному «dokumatika.ru» их не задевает.
    sed "s/dokumatika\.ru/${DOMAIN}/g" "${CODE_DIR}/deployment/nginx/dokumatika.conf" > "$NGINX_SITE"

    # RU: Директива `http2 on;` появилась в nginx 1.25.1, а в Debian 12 идёт
    # 1.22 и в Ubuntu 22.04 — 1.18. На них конфиг из репозитория не проходит
    # `nginx -t`, поэтому переписываем на старый синтаксис автоматически.
    NGINX_VER="$(nginx -v 2>&1 | sed -n 's|.*/\([0-9][0-9.]*\).*|\1|p')"
    if [ -n "$NGINX_VER" ] && ! version_ge "$NGINX_VER" "1.25.1"; then
        sed -i -e 's/^\([[:space:]]*\)listen 443 ssl;/\1listen 443 ssl http2;/' \
               -e 's/^\([[:space:]]*\)listen \[::\]:443 ssl;/\1listen [::]:443 ssl http2;/' \
               -e '/^[[:space:]]*http2 on;[[:space:]]*$/d' "$NGINX_SITE"
        ok "nginx ${NGINX_VER}: http2 переписан на старый синтаксис listen ... http2"
    else
        ok "nginx ${NGINX_VER:-неизвестной версии}: директива http2 on оставлена как есть"
    fi

    chmod 0644 "$NGINX_SITE"
    ln -sf "$NGINX_SITE" /etc/nginx/sites-enabled/dokumatika.conf
    rm -f /etc/nginx/sites-enabled/dokumatika-acme.conf
    if ! nginx -t >/dev/null 2>&1; then
        nginx -t || true
        # RU: Битый конфиг в sites-enabled роняет ВСЕ сайты сервера, не только
        # наш, — поэтому откатываемся сразу, не дожидаясь reload.
        rm -f /etc/nginx/sites-enabled/dokumatika.conf
        ln -sf "$NGINX_ACME" /etc/nginx/sites-enabled/dokumatika-acme.conf
        systemctl reload nginx || true
        die "nginx не принял боевой конфиг (вывод выше). Вернул временный, соседние сайты живы."
    fi
    systemctl reload nginx
    ok "сайт открыт: https://${DOMAIN}/"

    # RU: Продление проверяем сейчас, а не через 80 дней, когда чинить некогда.
    if certbot renew --dry-run >/dev/null 2>&1; then
        ok "автопродление сертификата проверено (certbot renew --dry-run)"
    else
        warn "certbot renew --dry-run не прошёл — разберитесь до истечения сертификата"
    fi
else
    warn "боевой конфиг не ставлю: без сертификата nginx его не примет"
    warn "временный конфиг оставлен — повторите bootstrap, когда заработает DNS"
fi

# ============================================================ 11. проверки
step "Проверяю готовность конфигурации"

# RU: От пользователя сервиса, а не от root: заодно убеждаемся, что у сервиса
# действительно есть доступ к базе и к каталогу с ней.
sudo -u "$APP_USER" python3 "${CODE_DIR}/scripts/preflight.py" --env-file "$ENV_FILE" || true

# =============================================================== 12. итог
ADMIN_URL="https://${DOMAIN}/admin/"
[ "$TLS_READY" -eq 1 ] || ADMIN_URL="пока недоступна снаружи — нет сертификата"

cat <<EOF

════════════════════════════════════════════════════════════════════════
  ДОКУМАТИКА РАЗВЁРНУТА
════════════════════════════════════════════════════════════════════════

  Сайт           https://${DOMAIN}/
  Админка        ${ADMIN_URL}
  Токен админки  ${ADMIN_TOKEN_VALUE}

  Токен вводится в ФОРМУ на странице входа, а не в адресную строку:
  адресная строка попадает в логи nginx и в историю браузера, а по этому
  токену видны почты всех покупателей. Токен лежит в ${ENV_FILE}.

────────────────────────── ЧТО ОСТАЛОСЬ ЗАПОЛНИТЬ РУКАМИ ───────────────

  Файл ${ENV_FILE} (править от root),
  затем: systemctl restart dokumatika

  1) Реквизиты продавца — Robokassa проверяет их в подвале сайта:
       SELLER_NAME      ФИО полностью, как в «Мой налог»
       SELLER_INN       12 цифр
       SELLER_EMAIL     рабочая почта для покупателей
       SELLER_ADDRESS   достаточно города

  2) Пароли Robokassa — появятся после модерации магазина:
       ROBOKASSA_MERCHANT_LOGIN
       ROBOKASSA_PASSWORD1, ROBOKASSA_PASSWORD2
       ROBOKASSA_TEST_PASSWORD1, ROBOKASSA_TEST_PASSWORD2
       ROBOKASSA_HASH_ALGORITHM — ОБЯЗАН совпадать с кабинетом Robokassa
       ROBOKASSA_TEST_MODE=1 сейчас; после тестового платежа поставить 0

  3) По желанию: SMTP_* (письмо покупателю) и METRIKA_ID (счётчик).

  Всё остальное — домен, пути, ADMIN_TOKEN, INDEXNOW_KEY, версия статики,
  бэкапы, сверка платежей — уже настроено, трогать не нужно.

──────────────────────────────── СЛЕДУЮЩИЙ ШАГ ─────────────────────────

EOF

if [ "$TLS_READY" -eq 1 ]; then
    cat <<EOF
  1. Заполнить реквизиты продавца в ${ENV_FILE},
     затем: systemctl restart dokumatika
  2. Проверить сайт снаружи:
       bash ${CODE_DIR}/scripts/smoke_prod.sh ${DOMAIN}
  3. Подать заявку в Robokassa: сайт уже отвечает её требованиям
     (оферта /oferta/, контакты /kontakty/, политика /privacy/,
     возврат /vozvrat/, реквизиты в подвале появятся с пункта 1).
  4. Тестовый платёж, затем боевой на 799 ₽ самому себе.
  5. Подать уведомление в Роскомнадзор за сам сайт.

  Подробности по каждому пункту: ${CODE_DIR}/deployment/ROLLOUT.md
EOF
    if [ "$WWW_IN_CERT" -eq 0 ]; then
        cat <<EOF

  ВНИМАНИЕ: www.${DOMAIN} в сертификат не попал — нет DNS-записи.
  Заведите CNAME www → ${DOMAIN} и повторите запуск:
    bash ${SCRIPT_PATH} ${DOMAIN} -y
EOF
    fi
else
    cat <<EOF
  СЕРТИФИКАТ НЕ ВЫПУЩЕН. Это единственное, чего скрипт сделать не может.

  Причина почти всегда одна: A-запись домена ещё не указывает на этот
  сервер либо DNS не успел разойтись (до 24 часов после правки).

    ${DOMAIN} разрешается в: ${DOMAIN_IPS:-НИ ВО ЧТО}
    адреса этого сервера:    ${LOCAL_IPS:-неизвестны}

  Что сделать:
  1. У регистратора домена создать A-запись @ на IP этого сервера
     и CNAME www на ${DOMAIN}.
  2. Дождаться, пока команда getent ahostsv4 ${DOMAIN}
     покажет нужный адрес.
  3. Повторить запуск — он безопасен, настройки сохранятся:
       bash ${SCRIPT_PATH} ${DOMAIN} -y

  Всё остальное уже сделано: сервис работает, база создана,
  бэкапы включены, файрвол закрыт.
EOF
fi

printf '\n════════════════════════════════════════════════════════════════════════\n\n'

[ "$TLS_READY" -eq 1 ] || exit 3
exit 0
