# Развёртывание dokumatika.ru с нуля

Пошаговая инструкция: от чистого сервера до первого боевого платежа.
Рассчитана на то, что её выполняет один человек, впервые, не помня наизусть
ничего из перечисленного. Команды выполняются от root (или через `sudo`).

Целевая машина: **2 vCPU / 4 GB**, на которой будут жить 3-5 таких проектов.
ОС: **Debian 12** или **Ubuntu 24.04 LTS**. Ubuntu 22.04 тоже подходит —
проекту достаточно Python 3.10, а он есть во всех трёх.

## Что получится

```
интернет ──► nginx :80/:443 ──► 127.0.0.1:8081 ──► python3 -m app.server
                 TLS                                    │
                 лимиты                                  └─► /var/lib/dokumatika/dokumatika.sqlite3
                 статика с диска
```

Приложение слушает только loopback: снаружи до него не достучаться в принципе,
даже если завтра в нём найдут дыру. TLS, сжатие, ограничение частоты запросов и
заголовки безопасности — забота nginx.

| Что | Где |
|---|---|
| Код | `/opt/dokumatika` (владелец root, приложение не может себя переписать) |
| База | `/var/lib/dokumatika/dokumatika.sqlite3` |
| Секреты | `/etc/dokumatika/.env` (root:dokumatika, 0640) |
| Резервные копии | `/var/backups/dokumatika` |
| Логи приложения | journald: `journalctl -u dokumatika` |
| Логи nginx | `/var/log/nginx/dokumatika-*.log` |

### Карта портов

Порт — единственное, что нельзя занять дважды. Заводим таблицу сразу, пока
проект один, иначе через полгода второй проект молча не поднимется.

| Порт | Проект | Пользователь | Юнит |
|---|---|---|---|
| 8081 | dokumatika.ru | `dokumatika` | `dokumatika.service` |
| 8082 | (свободен) | | |
| 8083 | (свободен) | | |
| 8084 | (свободен) | | |
| 8085 | (свободен) | | |

При клонировании проекта на новый домен меняются: порт, имя пользователя, пути
`/opt/<имя>` и `/var/lib/<имя>`, а в конфиге nginx — **имена зон**
`limit_req_zone`, имя `upstream` и имя `map`-переменной. Они глобальны для всего
nginx, и дубликат уронит `nginx -t` для всех сайтов сразу.

---

## Шаг 1. Пакеты

```bash
apt update && apt upgrade -y
apt install -y nginx python3 sqlite3 certbot python3-certbot-nginx \
               rsync curl ca-certificates ufw
python3 --version    # должно быть 3.10 или новее
```

Файрвол — сразу, до того как появится что закрывать:

```bash
ufw allow OpenSSH
ufw allow 80/tcp
ufw allow 443/tcp
ufw --force enable
ufw status
```

## Шаг 2. Swap 2 ГБ и параметры памяти

На 4 ГБ без свопа любой всплеск (например, `apt upgrade` рядом с работающими
сайтами) заканчивается тем, что OOM killer убивает не виновника, а самый
жирный процесс. Своп — это не «медленная память», а страховка от такого исхода.

```bash
fallocate -l 2G /swapfile
chmod 600 /swapfile
mkswap /swapfile
swapon /swapfile
echo '/swapfile none swap sw 0 0' >> /etc/fstab
swapon --show
```

```bash
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
sysctl --system
```

Тонкость: в юните `dokumatika.service` стоит `MemorySwapMax=0` — самому
приложению своп запрещён. Это осознанно: ушедшая в своп страница превращает
ответ за 5 мс в ответ за полсекунды. Своп нужен системе и соседям, а не сайту.

## Шаг 3. Ограничить журналы

По умолчанию journald может занять до 10% диска — на маленьком VPS это
десятки гигабайт, и однажды кончится место.

```bash
mkdir -p /etc/systemd/journald.conf.d
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
journalctl --disk-usage
```

Логи nginx ротирует свой logrotate из пакета — трогать не нужно.

## Шаг 4. Пользователь и каталоги

```bash
useradd --system --no-create-home --shell /usr/sbin/nologin \
        --home-dir /var/lib/dokumatika dokumatika

install -d -o root       -g root       -m 0755 /opt/dokumatika
install -d -o dokumatika -g dokumatika -m 0750 /var/lib/dokumatika
install -d -o dokumatika -g dokumatika -m 0750 /var/backups/dokumatika
install -d -o root       -g dokumatika -m 0750 /etc/dokumatika
install -d -o root       -g root       -m 0755 /var/www/certbot
```

Почему код принадлежит root, а не приложению: сервис работает от `dokumatika` и
не имеет права записи в собственный каталог. Даже при удалённом выполнении кода
подменить файлы сайта не выйдет. Каталог `/var/backups/dokumatika` создаём
руками — при `ProtectSystem=strict` сервис бэкапа создать его не сможет.

## Шаг 5. Выложить код

С рабочей машины, из корня проекта:

```bash
rsync -az --delete \
      --exclude '.git' --exclude 'var' --exclude '__pycache__' --exclude '*.pyc' \
      ./ root@СЕРВЕР:/opt/dokumatika/
```

На сервере:

```bash
chown -R root:root /opt/dokumatika
find /opt/dokumatika -type d -exec chmod 0755 {} +
find /opt/dokumatika -type f -exec chmod 0644 {} +
chmod 0755 /opt/dokumatika/scripts/backup.sh
```

Права 0755/0644 нужны и nginx: он отдаёт статику прямо с диска из
`/opt/dokumatika/src/static`.

## Шаг 6. Файл окружения

```bash
touch /etc/dokumatika/.env
chown root:dokumatika /etc/dokumatika/.env
chmod 0640 /etc/dokumatika/.env
```

Сгенерируйте секреты:

```bash
python3 -c "import secrets; print('ADMIN_TOKEN=' + secrets.token_urlsafe(24))"
python3 -c "import secrets; print('INDEXNOW_KEY=' + secrets.token_hex(16))"
```

Содержимое `/etc/dokumatika/.env` (формат systemd `EnvironmentFile`: без кавычек
вокруг значений, без подстановок `$VAR`, комментарии с `#`):

```ini
# --- сайт ---
SITE_DOMAIN=dokumatika.ru
SITE_SCHEME=https
SUPPORT_EMAIL=hello@dokumatika.ru
ASSET_VERSION=20260726-3

# --- реквизиты продавца: обязательны, Robokassa проверяет их в подвале сайта ---
SELLER_LEGAL_FORM=Самозанятый
SELLER_NAME=Фамилия Имя Отчество
SELLER_INN=000000000000
SELLER_EMAIL=hello@dokumatika.ru
SELLER_ADDRESS=Москва

# --- Robokassa ---
ROBOKASSA_MERCHANT_LOGIN=dokumatika
ROBOKASSA_PASSWORD1=...
ROBOKASSA_PASSWORD2=...
ROBOKASSA_TEST_PASSWORD1=...
ROBOKASSA_TEST_PASSWORD2=...
# 1 на время проверки, 0 в бою. Забыть переключить = продавать за воздух.
ROBOKASSA_TEST_MODE=1
# Должен совпадать с алгоритмом в кабинете Robokassa, иначе ошибка 29.
ROBOKASSA_HASH_ALGORITHM=sha256
# Самозанятый НДС не платит.
ROBOKASSA_RECEIPT_TAX=none

# --- почта (необязательно, но без неё покупатель не получит письмо) ---
SMTP_HOST=smtp.yandex.ru
SMTP_PORT=587
SMTP_USER=hello@dokumatika.ru
SMTP_PASSWORD=...
SMTP_SENDER=hello@dokumatika.ru
SMTP_USE_TLS=1

# --- служебное ---
ADMIN_TOKEN=...
PAYMENTS_ENABLED=1
APP_DEBUG=0

# --- аналитика и индексация (можно позже) ---
METRIKA_ID=
INDEXNOW_KEY=

# --- пути (ОБЯЗАТЕЛЬНО, см. пояснение ниже) ---
APP_HOST=127.0.0.1
APP_PORT=8081
DATABASE_PATH=/var/lib/dokumatika/dokumatika.sqlite3
STATIC_ROOT=/opt/dokumatika/src/static
BACKUP_DIR=/var/backups/dokumatika
BACKUP_KEEP=14
```

**Пути обязаны быть именно здесь, а не только в юните.** Юнит задаёт их через
`Environment=` — но эти значения видит только сам сервис. Скрипты
`preflight.py`, `reconcile_payments.py`, `indexnow_ping.py` и `backup.sh`
запускаются отдельно (по таймеру или руками) и читают `/etc/dokumatika/.env`.
Без `DATABASE_PATH` в файле сверка платежей полезет за базой по пути из кода —
то есть в каталог с исходниками, — не найдёт там заказов и промолчит. Зависший
платёж останется незамеченным.

Проверить, что скрипты видят ту же базу:

```bash
set -a; . /etc/dokumatika/.env; set +a
sudo -u dokumatika PYTHONPATH=/opt/dokumatika/src python3 /opt/dokumatika/scripts/preflight.py
# в выводе должен быть боевой путь /var/lib/dokumatika/dokumatika.sqlite3 и порт 8081
```

## Шаг 7. systemd

```bash
cp /opt/dokumatika/deployment/systemd/apps.slice                 /etc/systemd/system/
cp /opt/dokumatika/deployment/systemd/dokumatika.service         /etc/systemd/system/
cp /opt/dokumatika/deployment/systemd/dokumatika-backup.service  /etc/systemd/system/
cp /opt/dokumatika/deployment/systemd/dokumatika-backup.timer    /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now dokumatika
systemctl status dokumatika --no-pager
```

Проверка, что приложение действительно отвечает:

```bash
curl -sS http://127.0.0.1:8081/healthz
# {"status":"ok","uptime_s":3,"payments":"on","maintenance":false,"db":"ok","wal_mb":0.0}
```

Полезные проверки после запуска:

```bash
systemctl show dokumatika -p MemoryMax -p MemoryHigh -p CPUQuota -p TasksMax
systemd-cgtop -1 --depth=2          # сколько на самом деле ест слайс apps.slice
systemd-analyze security dokumatika.service   # оценка изоляции, чем ниже тем лучше
journalctl -u dokumatika -f
```

Если сервис не стартует, 90% случаев — это:
* опечатка в пути из `.env` (`DATABASE_PATH` вне `ReadWritePaths`);
* каталог базы принадлежит root, потому что кто-то запустил приложение из-под
  root раньше сервиса — `chown -R dokumatika:dokumatika /var/lib/dokumatika`;
* Python старее 3.10.

## Шаг 8. nginx и TLS

DNS домена должен уже указывать A-записью на IP сервера — проверьте до того, как
запускать certbot: `dig +short dokumatika.ru`.

**8.1. Временный конфиг только для проверки владения доменом.** Боевой конфиг
ссылается на сертификат, которого ещё нет, и `nginx -t` на нём упадёт.

```bash
cat > /etc/nginx/sites-available/dokumatika-acme.conf <<'EOF'
server {
    listen 80;
    listen [::]:80;
    server_name dokumatika.ru www.dokumatika.ru;
    location ^~ /.well-known/acme-challenge/ { root /var/www/certbot; }
    location / { return 404; }
}
EOF
ln -sf /etc/nginx/sites-available/dokumatika-acme.conf /etc/nginx/sites-enabled/
nginx -t && systemctl reload nginx
```

**8.2. Сертификат** на оба имени (www нужен, чтобы редирект с него работал по
https, а не через предупреждение браузера):

```bash
certbot certonly --webroot -w /var/www/certbot \
        -d dokumatika.ru -d www.dokumatika.ru \
        --email hello@dokumatika.ru --agree-tos --no-eff-email
ls /etc/letsencrypt/live/dokumatika.ru/
```

**8.3. Боевой конфиг:**

```bash
rm /etc/nginx/sites-enabled/dokumatika-acme.conf
install -d /etc/nginx/snippets
cp /opt/dokumatika/deployment/nginx/security-headers.conf \
   /etc/nginx/snippets/dokumatika-security-headers.conf
cp /opt/dokumatika/deployment/nginx/dokumatika.conf /etc/nginx/sites-available/
ln -sf /etc/nginx/sites-available/dokumatika.conf /etc/nginx/sites-enabled/
nginx -t && systemctl reload nginx
```

Если `nginx -t` ругается на `http2 on;` — у вас nginx старше 1.25.1
(`nginx -v`). В конфиге на этот случай есть закомментированный вариант с
`listen 443 ssl http2;`.

**8.4. Автопродление** сертификата:

```bash
certbot renew --dry-run
systemctl list-timers certbot.timer
```

Продление обслуживает таймер из пакета certbot. Проверьте `--dry-run` сейчас, а
не через 80 дней: перевыпуск через webroot ломается, если временный ACME-конфиг
удалён, а в боевом нет `location /.well-known/acme-challenge/` (в нашем — есть,
в HTTP-блоке).

## Шаг 9. Проверка снаружи

```bash
curl -sI https://dokumatika.ru/ | sort
curl -sI http://dokumatika.ru/            | grep -i location   # 301 на https
curl -sI https://www.dokumatika.ru/       | grep -i location   # 301 на apex
curl -s  https://dokumatika.ru/healthz
curl -s  https://dokumatika.ru/robots.txt
curl -s  https://dokumatika.ru/sitemap.xml | head -c 300
```

В заголовках главной должны быть: `content-security-policy`,
`x-content-type-options`, `referrer-policy`, `permissions-policy`,
`x-frame-options`. Если какого-то нет — вы добавили `add_header` в location и не
подключили там snippet (см. комментарий в начале `security-headers.conf`).

Проверка лимита частоты (должны появиться 429):

```bash
for i in $(seq 1 60); do curl -so /dev/null -w '%{http_code} ' https://dokumatika.ru/; done; echo
```

Готовность конфигурации целиком:

```bash
sudo -u dokumatika python3 /opt/dokumatika/scripts/preflight.py --env-file /etc/dokumatika/.env
```

Скрипт печатает чек-лист и возвращает ненулевой код, если есть `[FAIL]`.
Запускать его от `dokumatika`, а не от root — так вы заодно проверяете, что у
сервиса действительно есть доступ к базе.

## Шаг 10. Резервные копии

```bash
systemctl enable --now dokumatika-backup.timer
systemctl start dokumatika-backup          # первый прогон вручную
journalctl -u dokumatika-backup --no-pager | tail -20
ls -lh /var/backups/dokumatika/
```

Копия снимается через `VACUUM INTO` — согласованный снимок живой базы. Копировать
файл базы через `cp` **нельзя**: в режиме WAL свежие записи лежат в соседнем
`-wal`, и такая копия будет либо устаревшей, либо битой.

Хранится 14 последних копий, каждая проверяется `PRAGMA integrity_check` сразу
после создания. Порядок восстановления описан в конце `scripts/backup.sh`.

**Проверьте восстановление хотя бы один раз** — на тестовой копии, до того как
оно понадобится в панике. Бэкап, из которого ни разу не восстанавливались, — это
не бэкап, а надежда.

И сразу настройте выгрузку копий с сервера к себе: копия рядом с базой не
спасает от потери самого сервера.

## Шаг 11. Подключение Robokassa

### 11.1. Что должно быть на сайте до подачи заявки

Robokassa проверяет сайт руками, и отказ по формальному признаку стоит нескольких
дней. Проверьте по списку:

- [ ] сайт открывается по https и доступен публично;
- [ ] **реквизиты в подвале**: «Самозанятый Фамилия Имя Отчество, ИНН 000000000000, город»;
- [ ] **оферта** — `/oferta/`, с ценой, предметом, порядком получения;
- [ ] **контакты** на видном месте — `/kontakty/`, рабочий e-mail;
- [ ] **политика обработки персональных данных** — `/privacy/`;
- [ ] **порядок возврата** — `/vozvrat/`;
- [ ] описание товара с **реальной ценой 799 ₽**, совпадающей с той, что уйдёт в чек.

### 11.2. Кабинет Robokassa

1. Зарегистрировать магазин, указать сайт `https://dokumatika.ru`.
2. «Мои магазины» → «Технические настройки»:
   - Result URL: `https://dokumatika.ru/robokassa/result`, метод **POST**;
   - Success URL: `https://dokumatika.ru/oplata/uspeh/`, метод GET;
   - Fail URL: `https://dokumatika.ru/oplata/otmena/`, метод GET;
   - алгоритм расчёта хеша — **тот же, что в `ROBOKASSA_HASH_ALGORITHM`**
     (по умолчанию в кабинете стоит MD5, у нас в `.env` — sha256; расхождение
     даёт на платёжной странице ошибку 29 и больше ничего не сообщает).
3. Забрать Пароль #1, Пароль #2 и тестовые пароли, положить в `.env`.

### 11.3. Чеки для самозанятого

Онлайн-касса самозанятому не нужна (ч. 2.2 ст. 2 54-ФЗ) — чек формируется в
«Мой налог». Чтобы он выбивался автоматически при каждой оплате, подключите
бесплатный сервис **«Робочеки СМЗ»**: в приложении «Мой налог» разрешите
интеграцию с Robokassa (Настройки → Партнёры), после чего включите Робочеки в
кабинете Robokassa. Чек по электронной оплате обязан выбиваться **в момент
расчёта** (ст. 14 422-ФЗ).

Комиссия Robokassa: карты РФ ~3,4–3,9%, СБП ~3,0–3,5%. С 799 ₽ после комиссии и
налога НПД 4% остаётся примерно 740 ₽.

### 11.4. Тестовый платёж

```bash
# в .env: ROBOKASSA_TEST_MODE=1
systemctl restart dokumatika
```

Пройдите путь целиком: `/komplekt/` → оформление → тестовая оплата → возврат на
сайт → страница `/zakaz/<токен>/` с документами → письмо на почту.

Затем проверьте, что колбэк дошёл:

```bash
tail -5 /var/log/nginx/dokumatika-robokassa.log     # ожидаем 200, не 403
journalctl -u dokumatika | grep -E 'order_paid|order_email'
```

**403 в этом логе** означает, что запрос пришёл не с адресов Robokassa
(185.59.216.65 / 185.59.217.65) и был отсечён IP-фильтром. Фильтр — это второй
рубеж, подпись всё равно проверяется в приложении; как быстро его снять,
написано прямо в `dokumatika.conf` над блоком `location = /robokassa/result`.

## Шаг 12. Первый боевой платёж

```bash
# в .env: ROBOKASSA_TEST_MODE=0
systemctl restart dokumatika
sudo -u dokumatika python3 /opt/dokumatika/scripts/preflight.py --env-file /etc/dokumatika/.env
```

Купите комплект сами, настоящей картой, за настоящие 799 ₽. Это единственный
способ убедиться, что боевые пароли, чек и доставка работают. Проверьте всё:

- [ ] деньги списались, страница вернулась на сайт;
- [ ] `/zakaz/<токен>/` показывает документы;
- [ ] письмо со ссылкой пришло (и не в спам);
- [ ] чек пришёл покупателю и виден в «Мой налог»;
- [ ] заказ в статусе `paid` в админке `https://dokumatika.ru/admin/?token=...`;
- [ ] в `/var/log/nginx/dokumatika-robokassa.log` — код 200.

## Шаг 13. Отдать сайт поисковикам

```bash
sudo -u dokumatika python3 /opt/dokumatika/scripts/indexnow_ping.py \
     --env-file /etc/dokumatika/.env
```

Скрипт читает `sitemap.xml` с боевого сайта и отправляет адреса на
`api.indexnow.org` и `yandex.com/indexnow`. Ответы 200 и 202 — успех.
Перед этим убедитесь, что открывается файл-подтверждение
`https://dokumatika.ru/<INDEXNOW_KEY>.txt`.

Дальше — руками, один раз: Яндекс.Вебмастер (подтвердить домен, добавить
sitemap), Google Search Console. IndexNow ускоряет обход, но не заменяет их.

---

## Эксплуатация

### Обновление кода

```bash
# 1. Копия базы перед выкладкой — 10 секунд, которые однажды спасут
systemctl start dokumatika-backup

# 2. Код
rsync -az --delete --exclude '.git' --exclude 'var' \
      ./ root@СЕРВЕР:/opt/dokumatika/
ssh root@СЕРВЕР 'chown -R root:root /opt/dokumatika &&
                 find /opt/dokumatika -type f -exec chmod 0644 {} + &&
                 find /opt/dokumatika -type d -exec chmod 0755 {} + &&
                 chmod 0755 /opt/dokumatika/scripts/backup.sh'

# 3. Если менялись css/js — поднять ASSET_VERSION в .env, иначе у посетителей
#    останется старая статика из кэша на год вперёд
systemctl restart dokumatika
curl -s https://dokumatika.ru/healthz
```

Схема базы создаётся сама при старте (`CREATE TABLE IF NOT EXISTS`), отдельного
шага миграции нет.

### Режим обслуживания

Мягкий рубильник: сайт отдаёт заглушку с кодом 503 и `Retry-After`, а колбэк
Robokassa продолжает работать — иначе деньги списались бы, а заказ остался бы
неоплаченным.

```bash
sudo -u dokumatika touch /var/lib/dokumatika/MAINTENANCE   # включить
rm /var/lib/dokumatika/MAINTENANCE                          # выключить
```

Перезапуск не нужен. Заглушка рендерится с inline-стилем, который блокирует CSP,
поэтому выглядеть она будет просто текстом — это ожидаемо.

### Сверка зависших платежей

Если ResultURL не дошёл (сервер лежал, сертификат протух, сработал IP-фильтр),
заказ останется в `pending`. Скрипт сам спросит статус у Robokassa и, при
подтверждении оплаты, отметит заказ и дошлёт письмо.

```bash
sudo -u dokumatika python3 /opt/dokumatika/scripts/reconcile_payments.py \
     --env-file /etc/dokumatika/.env --dry-run
```

По расписанию — раз в 15 минут:

```bash
cat > /etc/cron.d/dokumatika-reconcile <<'EOF'
*/15 * * * * dokumatika /usr/bin/python3 /opt/dokumatika/scripts/reconcile_payments.py --env-file /etc/dokumatika/.env 2>&1 | /usr/bin/logger -t dokumatika-reconcile
EOF
chmod 0644 /etc/cron.d/dokumatika-reconcile
```

Вывод уходит в journald: `journalctl -t dokumatika-reconcile`. В тестовом режиме
скрипт откажется работать — OpStateExt тестовые операции не показывает.

### Куда смотреть, когда что-то не так

```bash
journalctl -u dokumatika -n 100 --no-pager        # логи приложения (JSON построчно)
journalctl -u dokumatika -p err --since today     # только ошибки
tail -f /var/log/nginx/dokumatika-error.log
tail -f /var/log/nginx/dokumatika-robokassa.log   # платёжные уведомления
systemd-cgtop -1 --depth=2                        # кто ест память и CPU
curl -s https://dokumatika.ru/healthz             # wal_mb растёт? нужен checkpoint
```

Персональных данных и секретов в логах нет: почта маскируется, подписи и пароли
не пишутся вообще.

---

## Что здесь осознанно не идеально

Честный список компромиссов — чтобы через полгода не выяснять их заново.

**1. CSP и inline-скрипты.** Политика строгая: `script-src 'self'`. Два места из
неё выбиваются:

* Страница `/pay/` содержит inline-скрипт автосабмита формы на Robokassa. Для
  неё в `dokumatika.conf` прописана отдельная CSP, где разрешён ровно этот
  скрипт — по SHA-256 его содержимого. **При правке этой строки в
  `handlers.py` хеш надо пересчитать**, иначе автопереход перестанет работать.
  Страница при этом останется рабочей: под формой есть видимая кнопка.
* Счётчик Яндекс.Метрики вставляется inline и грузит `tag.js` с `mc.yandex.ru` —
  строгая CSP его блокирует. Если включаете `METRIKA_ID`, замените CSP на
  закомментированный вариант в `security-headers.conf`. Цена — `'unsafe-inline'`
  в `script-src`, то есть основная защита от XSS. Альтернатива: обойтись
  встроенной воронкой в `/admin/`, она считает события на сервере и не зависит
  от блокировщиков.

**2. HSTS выключен.** Заголовок в `security-headers.conf` закомментирован
намеренно. Он запоминается браузером на год, и если TLS сломается, посетители
получат страницу ошибки без обхода. Включать через неделю стабильной работы,
после успешного `certbot renew --dry-run`, — и не забыть раскомментировать его
и в блоке `location ^~ /pay/`.

**3. IP-фильтр на колбэке Robokassa.** Адреса 185.59.216.65 и 185.59.217.65 взяты
из документации и могут смениться без предупреждения. Это второй рубеж, не
первый: настоящая проверка — подпись на Пароле #2 внутри приложения. Страховка
от смены адресов — сверка платежей по крону (см. выше).

**4. SQLite, а не PostgreSQL.** При десятках заказов в месяц отдельная СУБД съела
бы 200–300 МБ ни за что. Интерфейс репозиториев узкий: переезд при росте — это
переписать один модуль, не трогая HTTP-слой.

**5. Заглушка обслуживания без оформления.** См. выше про CSP и inline-стили.

---

## Финальный чек-лист

- [ ] `systemctl is-enabled dokumatika` → `enabled`
- [ ] `systemctl is-enabled dokumatika-backup.timer` → `enabled`
- [ ] `preflight.py` не выдаёт `[FAIL]`
- [ ] `https://dokumatika.ru/` открывается, `http://` и `www` редиректят на неё
- [ ] заголовки безопасности видны в `curl -sI`
- [ ] `certbot renew --dry-run` проходит
- [ ] бэкап создан, из копии хотя бы раз восстанавливались
- [ ] `ROBOKASSA_TEST_MODE=0`
- [ ] боевой платёж прошёл целиком: деньги, документы, письмо, чек
- [ ] cron сверки платежей стоит
- [ ] IndexNow отправлен, sitemap добавлен в Вебмастер
- [ ] реквизиты, оферта, контакты, политика и возврат — на сайте
