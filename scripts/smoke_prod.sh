#!/usr/bin/env bash
#
# Проверка боевого сайта снаружи — после выкатки и после каждого обновления.
#
#   bash scripts/smoke_prod.sh dokumatika.ru
#
# ЗАЧЕМ. `systemctl status` зелёный и `curl 127.0.0.1/healthz` отвечает даже
# тогда, когда посетитель видит 502, просроченный сертификат или страницу без
# оформления. Отсюда правило: проверяем то же самое, что видит браузер, —
# по имени домена, по https, снаружи приложения.
#
# Скрипт ничего не меняет и не требует root. Печатает [ok] / [FAIL] и
# возвращает ненулевой код, если хоть одна проверка провалилась: его можно
# ставить в мониторинг или дёргать из cron после выкатки.
#
# ЧТО ПРОВЕРЯЕТСЯ:
#   * ключевые адреса отдают 200;
#   * работает TLS и сертификат не истекает на днях;
#   * http и www ведут на канонический https-адрес;
#   * на страницах есть заголовки безопасности;
#   * /admin/ не открывается без токена;
#   * база жива (это и означает 200 на /healthz — при сбое базы там 503).

set -euo pipefail

DOMAIN=""
TIMEOUT=15
# RU: Порог предупреждения о сертификате. Let's Encrypt даёт 90 дней и продлевает
# за 30 до конца; если осталось меньше 20 — продление уже не сработало.
CERT_WARN_DAYS=20

PASSED=0
FAILED=0
WARNED=0

usage() {
    cat <<'EOF'
Проверка боевого сайта Докуматики.

    bash scripts/smoke_prod.sh <домен>

    <домен>  канонический домен без схемы и без www, например dokumatika.ru

Код возврата: 0 — всё в порядке, 1 — есть провалы, 2 — неверный запуск.
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        -h|--help) usage; exit 0 ;;
        -*) usage; printf '\nНеизвестный флаг: %s\n' "$1" >&2; exit 2 ;;
        *)
            if [ -z "$DOMAIN" ]; then DOMAIN="$1"; else usage; exit 2; fi
            ;;
    esac
    shift
done

if [ -z "$DOMAIN" ]; then
    usage
    printf '\nНе передан домен. Пример: bash scripts/smoke_prod.sh dokumatika.ru\n' >&2
    exit 2
fi

DOMAIN="$(printf '%s' "$DOMAIN" | tr 'A-Z' 'a-z')"
case "$DOMAIN" in
    http*|*/*)
        printf 'Домен указывается без схемы и без слэша: dokumatika.ru, а не «%s».\n' "$DOMAIN" >&2
        exit 2
        ;;
esac

command -v curl >/dev/null 2>&1 || { printf 'Нужен curl: apt install curl\n' >&2; exit 2; }

BASE="https://${DOMAIN}"

# ------------------------------------------------------------------- вывод
ok()   { PASSED=$((PASSED + 1)); printf '[ok]   %s\n' "$*"; }
bad()  { FAILED=$((FAILED + 1)); printf '[FAIL] %s\n' "$*"; }
soft() { WARNED=$((WARNED + 1)); printf '[!]    %s\n' "$*"; }
head2() { printf '\n— %s\n' "$*"; }

# http_code URL — код ответа. Если соединения не было, curl сам печатает 000,
# поэтому его ненулевой код мы гасим, а не подставляем «000» вторым разом.
http_code() {
    curl -sS -o /dev/null -w '%{http_code}' --max-time "$TIMEOUT" "$1" 2>/dev/null || true
}

# headers URL — заголовки ответа в нижнем регистре, одной пачкой.
headers() {
    curl -sS -D - -o /dev/null --max-time "$TIMEOUT" "$1" 2>/dev/null | tr 'A-Z' 'a-z' || true
}

# body URL — тело ответа (для /healthz и robots.txt).
body() {
    curl -sS --max-time "$TIMEOUT" "$1" 2>/dev/null || true
}

# expect_code URL ОЖИДАЕМЫЙ ОПИСАНИЕ
expect_code() {
    local url="$1" want="$2" title="$3" got
    got="$(http_code "$url")"
    if [ "$got" = "$want" ]; then
        ok "${title} — ${got}"
    elif [ "$got" = "000" ]; then
        bad "${title} — нет ответа (домен не резолвится, порт закрыт или TLS сломан)"
    else
        bad "${title} — ${got}, ожидался ${want}"
    fi
}

printf 'Проверка боевого сайта: %s\n' "$BASE"
printf '========================================================================\n'

# --------------------------------------------------------------------- TLS
head2 "TLS"

if command -v openssl >/dev/null 2>&1; then
    # RU: Сертификат забираем один раз в PEM и дальше разбираем локально —
    # отдельные ключи вроде `-ext subjectAltName` есть не во всех сборках openssl,
    # и падение разбора нельзя путать с «сайт не отвечает по TLS».
    CERT_PEM="$(printf 'Q\n' \
        | openssl s_client -connect "${DOMAIN}:443" -servername "$DOMAIN" 2>/dev/null \
        | openssl x509 2>/dev/null || true)"
    if [ -z "$CERT_PEM" ]; then
        bad "сертификат не получен — TLS не отвечает на ${DOMAIN}:443"
    else
        NOT_AFTER="$(printf '%s\n' "$CERT_PEM" \
            | openssl x509 -noout -enddate 2>/dev/null | sed -n 's/^notAfter=//p' || true)"
        ok "сертификат получен, действует до ${NOT_AFTER:-неизвестной даты}"
        # RU: date -d понимает формат notAfter; если не понял — молча не считаем
        # дни, но сам факт валидного соединения уже проверен выше.
        EXP_TS=""
        if [ -n "$NOT_AFTER" ]; then
            EXP_TS="$(date -d "$NOT_AFTER" +%s 2>/dev/null || true)"
        fi
        if [ -n "$EXP_TS" ]; then
            DAYS_LEFT=$(( (EXP_TS - $(date +%s)) / 86400 ))
            if [ "$DAYS_LEFT" -lt 0 ]; then
                bad "сертификат ПРОСРОЧЕН ${DAYS_LEFT#-} дн. назад"
            elif [ "$DAYS_LEFT" -lt "$CERT_WARN_DAYS" ]; then
                soft "до истечения сертификата ${DAYS_LEFT} дн. — проверьте: certbot renew --dry-run"
            else
                ok "до истечения сертификата ${DAYS_LEFT} дн."
            fi
        fi
        if printf '%s\n' "$CERT_PEM" | openssl x509 -noout -text 2>/dev/null \
            | grep -qi "dns:www\.${DOMAIN}"; then
            ok "www.${DOMAIN} входит в сертификат"
        else
            soft "www.${DOMAIN} в сертификат не входит — редирект с www даст предупреждение браузера"
        fi
    fi
else
    soft "openssl не установлен — срок действия сертификата не проверен"
fi

# ---------------------------------------------------------------- страницы
head2 "Ключевые адреса"

expect_code "${BASE}/"            200 "главная /"
expect_code "${BASE}/komplekt/"   200 "витрина комплекта /komplekt/"
expect_code "${BASE}/healthz"     200 "здоровье /healthz"
expect_code "${BASE}/robots.txt"  200 "/robots.txt"
expect_code "${BASE}/sitemap.xml" 200 "/sitemap.xml"
expect_code "${BASE}/styles.css"  200 "статика /styles.css"

# RU: Robokassa не подключит магазин без этих четырёх страниц, и проверять их
# она будет руками. Дешевле убедиться самому, чем ждать отказ несколько дней.
head2 "Страницы, обязательные для Robokassa"
expect_code "${BASE}/oferta/"   200 "оферта /oferta/"
expect_code "${BASE}/kontakty/" 200 "контакты /kontakty/"
expect_code "${BASE}/privacy/"  200 "политика /privacy/"
expect_code "${BASE}/vozvrat/"  200 "возврат /vozvrat/"

# ---------------------------------------------------------------- редиректы
head2 "Канонический адрес"

# RU: Сначала код ответа, и только потом заголовки: у лежащего сайта заголовков
# нет вовсе, и «нет Location» означало бы совсем другую поломку.
HTTP_CODE="$(http_code "http://${DOMAIN}/")"
if [ "$HTTP_CODE" = "000" ]; then
    bad "http://${DOMAIN}/ не отвечает — порт 80 закрыт или nginx не поднят"
elif printf '%s' "$(headers "http://${DOMAIN}/")" | grep -q '^location: https://'; then
    ok "http:// уводит на https:// — ${HTTP_CODE}"
else
    bad "http://${DOMAIN}/ отдал ${HTTP_CODE} без редиректа на https — проверьте блок listen 80"
fi

WWW_CODE="$(http_code "https://www.${DOMAIN}/")"
if [ "$WWW_CODE" = "000" ]; then
    soft "www.${DOMAIN} не отвечает — нет DNS-записи или www не входит в сертификат"
elif printf '%s' "$(headers "https://www.${DOMAIN}/")" | grep -q "^location: https://${DOMAIN}"; then
    ok "www уводит на ${DOMAIN} — ${WWW_CODE}"
else
    bad "www.${DOMAIN} отдал ${WWW_CODE} и не редиректит на канонический ${DOMAIN}"
fi

# ------------------------------------------------------------- безопасность
head2 "Заголовки безопасности"

MAIN_CODE="$(http_code "${BASE}/")"
MAIN_HEAD="$(headers "${BASE}/")"
if [ "$MAIN_CODE" != "200" ]; then
    bad "главная отдала ${MAIN_CODE} — заголовки проверять не на чем"
else
    for header in content-security-policy x-content-type-options referrer-policy \
                  permissions-policy x-frame-options; do
        if printf '%s' "$MAIN_HEAD" | grep -q "^${header}:"; then
            ok "заголовок ${header}"
        else
            # RU: Обычная причина — add_header в location без include snippet:
            # свой add_header отменяет всё унаследованное от server, и молча.
            bad "нет заголовка ${header} — не подключён snippet в каком-то location"
        fi
    done
    if printf '%s' "$MAIN_HEAD" | grep -q '^server: nginx/'; then
        soft "nginx показывает свою версию — включите server_tokens off"
    fi
    if printf '%s' "$MAIN_HEAD" | grep -q '^strict-transport-security:'; then
        ok "HSTS включён"
    else
        # RU: Это не ошибка: HSTS включают через неделю стабильной работы, см.
        # комментарий в deployment/nginx/security-headers.conf.
        soft "HSTS выключен — включить после недели стабильной работы TLS"
    fi
fi

# ------------------------------------------------------------------ админка
head2 "Закрытые разделы"

ADMIN_CODE="$(http_code "${BASE}/admin/")"
case "$ADMIN_CODE" in
    200) bad "/admin/ открывается БЕЗ токена — немедленно проверьте ADMIN_TOKEN в .env" ;;
    401|403) ok "/admin/ без токена не пускает — ${ADMIN_CODE}" ;;
    404) soft "/admin/ отвечает 404 — админка отключена (пустой ADMIN_TOKEN)" ;;
    000) bad "/admin/ — нет ответа" ;;
    *) soft "/admin/ отвечает ${ADMIN_CODE} — ожидались 401 или 403" ;;
esac

# RU: Токен в адресной строке приложение принимать не должно: query-строка
# целиком ложится в access-лог nginx, в историю браузера и в Referer.
TOKEN_CODE="$(http_code "${BASE}/admin/?token=zavedomo-nevernyy")"
if [ "$TOKEN_CODE" = "200" ]; then
    bad "/admin/?token=... пустил по ссылке — токен обязан вводиться формой"
elif [ "$TOKEN_CODE" = "000" ]; then
    : # RU: Сайт не отвечает вовсе — об этом уже сказано выше, второй раз не повторяем.
else
    ok "/admin/?token=... по ссылке не пускает — ${TOKEN_CODE}"
fi

# ----------------------------------------------------------- база и контент
head2 "Приложение и база"

# RU: /healthz наружу отдаёт только «жив или нет»: подробности видны лишь по
# админ-токену. Но 200 здесь уже означает, что healthcheck базы прошёл —
# при недоступной базе приложение отвечает 503.
HEALTH="$(body "${BASE}/healthz")"
if printf '%s' "$HEALTH" | grep -q '"status"[[:space:]]*:[[:space:]]*"ok"'; then
    ok "база отвечает: /healthz вернул status=ok"
else
    bad "/healthz вернул «${HEALTH:-пусто}» — приложение или база недоступны"
fi

ROBOTS="$(body "${BASE}/robots.txt")"
if printf '%s' "$ROBOTS" | grep -qi "sitemap: *https://${DOMAIN}/sitemap.xml"; then
    ok "robots.txt указывает на sitemap боевого домена"
elif printf '%s' "$ROBOTS" | grep -qi 'sitemap:'; then
    bad "в robots.txt чужой домен в Sitemap — проверьте SITE_DOMAIN в .env"
else
    bad "в robots.txt нет строки Sitemap"
fi

HOME_BODY="$(body "${BASE}/")"
# RU: canonical собирается из SITE_DOMAIN. Если он не тот, сайт внешне работает,
# а поисковики склеивают страницы с чужим адресом — заметить это иначе нечем.
if printf '%s' "$HOME_BODY" | grep -q "rel=\"canonical\" href=\"https://${DOMAIN}/\""; then
    ok "canonical главной ведёт на https://${DOMAIN}/"
else
    bad "canonical главной не равен https://${DOMAIN}/ — проверьте SITE_DOMAIN в .env"
fi
# RU: Реквизиты в подвале — формальное требование Robokassa к сайту магазина.
# Пустые SELLER_* проходят все технические проверки и всплывают только отказом
# модерации через несколько дней, поэтому проверяем ровно ту надпись, которую
# показывает подвал, когда реквизиты не заполнены.
if printf '%s' "$HOME_BODY" | grep -q 'Реквизиты продавца не заполнены'; then
    bad "в подвале нет реквизитов — заполните SELLER_* в .env, иначе Robokassa откажет"
elif printf '%s' "$HOME_BODY" | grep -q 'ИНН'; then
    ok "реквизиты продавца видны в подвале"
else
    bad "подвал не отдал реквизиты — проверьте SELLER_* в .env"
fi

# --------------------------------------------------------------------- итог
printf '\n========================================================================\n'
printf 'Итого: %s в порядке, %s предупреждений, %s провалов\n' "$PASSED" "$WARNED" "$FAILED"

if [ "$FAILED" -gt 0 ]; then
    printf '\nСайт к бою не готов: сначала закройте пункты [FAIL].\n'
    printf 'Где смотреть: journalctl -u dokumatika -n 50 --no-pager\n'
    printf '              tail -20 /var/log/nginx/dokumatika-error.log\n'
    exit 1
fi
printf '\nПровалов нет. Предупреждения прочитайте — они не просто так.\n'
exit 0
