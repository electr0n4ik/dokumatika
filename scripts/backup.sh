#!/usr/bin/env bash
#
# Резервная копия базы Докуматики.
#
# ПОЧЕМУ НЕ cp. База работает в режиме WAL: часть свежих записей (последние
# заказы, отметки об оплате) физически лежит не в файле .sqlite3, а в соседнем
# .sqlite3-wal. Копия, снятая обычным cp, окажется либо устаревшей на несколько
# минут, либо, если файл скопировался в момент записи, битой — и узнаете вы об
# этом ровно тогда, когда она понадобится.
# Правильный способ ровно один: попросить об этом сам SQLite. Здесь —
# `VACUUM INTO`, который снимает согласованный снимок живой базы, попутно
# уплотняя её. Резервный путь (если нет консольного sqlite3) — API .backup().
#
# Запуск: systemd-таймером dokumatika-backup.timer или руками:
#   BACKUP_DIR=/tmp/x scripts/backup.sh
#
# Переменные окружения:
#   DATABASE_PATH  путь к базе       (по умолчанию /var/lib/dokumatika/dokumatika.sqlite3)
#   BACKUP_DIR     куда складывать   (по умолчанию /var/backups/dokumatika)
#   BACKUP_KEEP    сколько хранить   (по умолчанию 14 копий)

set -eu
# RU: Без pipefail ошибка в середине конвейера теряется, и скрипт «успешно»
# завершается с пустым результатом.
set -o pipefail
# RU: Копия базы содержит почты покупателей — читать её может только владелец.
umask 077

DB_PATH="${DATABASE_PATH:-/var/lib/dokumatika/dokumatika.sqlite3}"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/dokumatika}"
BACKUP_KEEP="${BACKUP_KEEP:-14}"

log() { printf '%s backup: %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*"; }
fail() { log "ОШИБКА: $*" >&2; exit 1; }

[ -f "$DB_PATH" ] || fail "база не найдена: $DB_PATH"
[ -d "$BACKUP_DIR" ] || mkdir -p "$BACKUP_DIR" || fail "нет каталога для копий: $BACKUP_DIR"
[ -w "$BACKUP_DIR" ] || fail "каталог для копий недоступен на запись: $BACKUP_DIR"

case "$BACKUP_KEEP" in
    ''|*[!0-9]*) fail "BACKUP_KEEP должен быть числом, получено: $BACKUP_KEEP" ;;
esac
[ "$BACKUP_KEEP" -ge 1 ] || fail "BACKUP_KEEP должен быть не меньше 1"

if command -v sqlite3 >/dev/null 2>&1; then
    HAS_CLI=1
else
    HAS_CLI=0
    log "консольный sqlite3 не найден — работаю через python3"
    command -v python3 >/dev/null 2>&1 || fail "нужен sqlite3 или python3"
fi

# db_query <файл> <sql> — вернуть результат одного запроса одной строкой.
db_query() {
    if [ "$HAS_CLI" = "1" ]; then
        sqlite3 "$1" "$2"
    else
        python3 -c '
import sqlite3, sys
conn = sqlite3.connect(sys.argv[1])
try:
    for row in conn.execute(sys.argv[2]):
        print("|".join(str(value) for value in row))
finally:
    conn.close()
' "$1" "$2"
    fi
}

STAMP="$(date -u '+%Y%m%d-%H%M%S')"
TARGET="${BACKUP_DIR}/dokumatika-${STAMP}.sqlite3"

if [ -e "$TARGET" ]; then
    fail "копия с таким именем уже есть: $TARGET"
fi
# RU: Незавершённая копия не должна остаться в каталоге и попасть в ротацию как
# полноценная — снимаем её при любом аварийном выходе.
trap 'rm -f "$TARGET"' EXIT

log "снимаю копию: $DB_PATH -> $TARGET"
if [ "$HAS_CLI" = "1" ]; then
    # RU: Путь подставляется в SQL, поэтому апострофы в имени каталога сломают
    # запрос. Для системных путей это не проблема, но каталог с ' не создавайте.
    case "$TARGET" in
        *"'"*) fail "в пути к копии есть апостроф — так нельзя: $TARGET" ;;
    esac
    sqlite3 "$DB_PATH" "VACUUM INTO '${TARGET}'"
else
    python3 -c '
import sqlite3, sys
source = sqlite3.connect(sys.argv[1])
target = sqlite3.connect(sys.argv[2])
try:
    source.backup(target)
finally:
    target.close()
    source.close()
' "$DB_PATH" "$TARGET"
fi

[ -s "$TARGET" ] || fail "копия пустая: $TARGET"

# --------------------------------------------------------------- проверка
# RU: Копия без проверки — это надежда, а не бэкап. integrity_check читает всю
# базу и подтверждает, что структура цела и данные читаются.
log "проверяю целостность копии"
INTEGRITY="$(db_query "$TARGET" 'PRAGMA integrity_check;' | head -n 1)" \
    || fail "не удалось прочитать копию: $TARGET"
[ "$INTEGRITY" = "ok" ] || fail "integrity_check вернул «${INTEGRITY}» — копия негодная"

FK_ISSUES="$(db_query "$TARGET" 'PRAGMA foreign_key_check;' | wc -l)" || FK_ISSUES=0
[ "$FK_ISSUES" -eq 0 ] || log "ВНИМАНИЕ: нарушений внешних ключей: $FK_ISSUES"

# RU: Заодно убеждаемся, что в копии есть таблицы приложения, а не пустой файл
# с валидным заголовком (такой тоже пройдёт integrity_check).
ORDERS="$(db_query "$TARGET" 'SELECT COUNT(*) FROM orders;' 2>/dev/null || echo '')"
[ -n "$ORDERS" ] || fail "в копии нет таблицы orders — проверьте DATABASE_PATH"
PAID="$(db_query "$TARGET" "SELECT COUNT(*) FROM orders WHERE status = 'paid';" 2>/dev/null || echo '?')"

RAW_SIZE="$(wc -c < "$TARGET")"
gzip -9 "$TARGET"
ARCHIVE="${TARGET}.gz"
[ -f "$ARCHIVE" ] || fail "не удалось сжать копию"
chmod 600 "$ARCHIVE"
trap - EXIT

GZ_SIZE="$(wc -c < "$ARCHIVE")"
log "готово: $ARCHIVE (${GZ_SIZE} байт, до сжатия ${RAW_SIZE}); заказов ${ORDERS}, оплачено ${PAID}"

# --------------------------------------------------------------- ротация
# RU: Храним BACKUP_KEEP последних копий. Суточные копии базы на несколько
# мегабайт — 14 штук это меньше сотни мегабайт, зато две недели истории:
# хватает, чтобы заметить порчу данных и откатиться до неё.
SKIP="+$((BACKUP_KEEP + 1))"
OUTDATED="$(ls -1t "${BACKUP_DIR}"/dokumatika-*.sqlite3.gz 2>/dev/null | tail -n "$SKIP" || true)"
REMOVED=0
while IFS= read -r old; do
    [ -n "$old" ] || continue
    rm -f -- "$old"
    REMOVED=$((REMOVED + 1))
done < <(printf '%s\n' "$OUTDATED")

KEPT="$(ls -1 "${BACKUP_DIR}"/dokumatika-*.sqlite3.gz 2>/dev/null | wc -l || true)"
log "ротация: удалено ${REMOVED}, осталось ${KEPT} (лимит ${BACKUP_KEEP})"

# ВОССТАНОВЛЕНИЕ (проверьте это хотя бы раз, до того как понадобится):
#   systemctl stop dokumatika
#   gunzip -c /var/backups/dokumatika/dokumatika-ГГГГММДД-ЧЧММСС.sqlite3.gz \
#     > /var/lib/dokumatika/dokumatika.sqlite3
#   rm -f /var/lib/dokumatika/dokumatika.sqlite3-wal /var/lib/dokumatika/dokumatika.sqlite3-shm
#   chown dokumatika:dokumatika /var/lib/dokumatika/dokumatika.sqlite3
#   systemctl start dokumatika
# Старые -wal и -shm удалить обязательно: они относятся к прежней базе, и
# SQLite попытается «докатить» их поверх восстановленной.
#
# ХРАНЕНИЕ ВНЕ СЕРВЕРА. Копия рядом с базой не спасает от потери самого сервера.
# Минимум — забирать её к себе по расписанию, например с рабочей машины:
#   rsync -az --delete <сервер>:/var/backups/dokumatika/ ~/backups/dokumatika/
