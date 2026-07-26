# Выкатка dokumatika.ru

От чистого сервера до первого боевого платежа.

Раньше здесь была инструкция на 13 шагов и больше сотни команд, которые нужно
было выполнить руками в правильном порядке. Теперь всё, что может сделать
машина, делает `scripts/bootstrap.sh`. Ниже — сначала честный список того, чего
автоматизировать нельзя, потом одна команда, потом справочник ручных шагов на
случай отладки.

Целевая машина: **2 vCPU / 4 GB**, на которой живут 3-5 таких проектов.
ОС: **Debian 12** или **Ubuntu 22.04 LTS / 24.04 LTS**.

---

## Что обязан сделать владелец лично

Это единственная часть, которую нельзя делегировать скрипту: везде нужен
человек с паспортом, картой или доступом в чужой личный кабинет.

| # | Что сделать | Время | Когда |
|---|---|---|---|
| 1 | **Купить домен** у регистратора (reg.ru, timeweb, nic.ru) | 10 мин | до всего |
| 2 | **Направить DNS на сервер**: A-запись `@` → IP сервера, CNAME `www` → домен | 5 мин | до `bootstrap.sh` |
| 3 | **Запустить `bootstrap.sh`** и дождаться конца | 2 мин + ожидание | после п. 2 |
| 4 | **Заполнить реквизиты продавца** в `/etc/dokumatika/.env` (ФИО, ИНН, e-mail, город — как в «Мой налог») | 5 мин | сразу после п. 3 |
| 5 | **Завести аккаунт Robokassa**, подать магазин на модерацию, вписать три URL в «Технические настройки» | 20 мин | после п. 4 |
| 6 | **Разрешить «Робочеки СМЗ»** в приложении «Мой налог» и включить их в кабинете Robokassa | 10 мин | после одобрения |
| 7 | **Вписать пароли Robokassa** в `.env`, прогнать тестовый платёж, затем боевой на 799 ₽ самому себе | 15 мин | после п. 6 |
| 8 | **Подать уведомление в Роскомнадзор** за сам сайт (`pd.rkn.gov.ru`, бесплатно, через Госуслуги) | 30 мин | до сбора заявок |
| 9 | **Подтвердить домен в Яндекс.Вебмастере**, добавить `sitemap.xml` | 10 мин | после п. 3 |
| 10 | **Завести счётчик Метрики** и вписать `METRIKA_ID` (по желанию — своя воронка в `/admin/` работает без него) | 10 мин | когда угодно |

**Итого около 117 минут — примерно два часа чистого времени владельца.**

Растянуты эти два часа будут на несколько дней, но не по вине проекта:

- DNS расходится по миру до 24 часов (обычно 15-60 минут);
- модерация магазина в Robokassa — 1-3 рабочих дня;
- Роскомнадзор вносит в реестр не сразу, но ждать внесения не нужно:
  обработку можно начинать с даты подачи.

Ждать — не то же самое, что работать. Пунктов, где владелец сидит за
клавиатурой, ровно десять.

### Чего в этом списке нет — и почему

Всё остальное скрипт делает сам: ставит пакеты, заводит системного
пользователя, раскладывает код, **генерирует `ADMIN_TOKEN` и `INDEXNOW_KEY`**,
создаёт swap, ограничивает журналы, ставит юниты systemd и таймер бэкапа,
включает файрвол, настраивает nginx под ваш домен, получает сертификат и
ставит в cron сверку зависших платежей.

Секреты владелец не придумывает: придуманный человеком токен почти всегда
короче и предсказуемее машинного, а по `ADMIN_TOKEN` видны почты всех
покупателей.

---

## Всё остальное — одна команда

На сервере, от root:

```bash
apt update && apt install -y git
git clone https://github.com/electr0n4ik/dokumatika.git /root/dokumatika
bash /root/dokumatika/scripts/bootstrap.sh dokumatika.ru
```

Или, если код выкладывается с рабочей машины:

```bash
rsync -az --exclude '.git' --exclude 'var' --exclude '.env' ./ root@СЕРВЕР:/root/dokumatika/
ssh root@СЕРВЕР 'bash /root/dokumatika/scripts/bootstrap.sh dokumatika.ru'
```

Аргумент один — домен, обязательно канонический, без `www` и без `https://`.
Вторым аргументом можно передать почту для уведомлений Let's Encrypt (по
умолчанию `admin@<домен>`). Флаг `-y` пропускает единственный вопрос.

Скрипт спросит подтверждение в начале и дальше не задаёт вопросов. В конце он
печатает адрес админки, токен к ней, список того, что осталось заполнить
руками, и следующий шаг.

### Что делает bootstrap.sh

1. Проверяет, что это Debian 12 / Ubuntu 22.04+, что запуск от root и что
   Python не старее 3.10 — иначе объясняет, что не так, и выходит.
2. Ставит пакеты: nginx, python3, sqlite3, certbot, curl, ufw, cron.
3. Включает ufw, **предварительно** разрешив ssh — в том числе на нестандартном
   порту, если вы подключены не по 22-му.
4. Создаёт swap 2 ГБ и выставляет `vm.swappiness=10`, если свопа ещё нет.
   В контейнере, где своп завести нельзя, честно об этом сообщает и продолжает.
5. Ограничивает journald тремястами мегабайтами.
6. Заводит системного пользователя `dokumatika` без shell и без пароля.
7. Раскладывает код в `/opt/dokumatika` (владелец root), базу в
   `/var/lib/dokumatika`, копии в `/var/backups/dokumatika`, конфиг в
   `/etc/dokumatika/.env` с правами `0640`.
8. Создаёт `.env`: генерирует `ADMIN_TOKEN` и `INDEXNOW_KEY`, прописывает домен,
   пути, порт и свежую `ASSET_VERSION`. Поля продавца и Robokassa оставляет
   пустыми — их заполняет человек.
9. Ставит юниты systemd и `apps.slice`, включает сервис и ежедневный таймер
   бэкапа, дожидается ответа приложения на `127.0.0.1:8081`.
10. Ставит в `/etc/cron.d` сверку зависших платежей раз в 15 минут.
11. Настраивает nginx под ваш домен, при необходимости переписывая `http2 on;`
    на старый синтаксис (в Debian 12 идёт nginx 1.22, директива появилась в
    1.25.1 — конфиг из репозитория там иначе не проходит `nginx -t`).
12. Получает сертификат Let's Encrypt и проверяет автопродление.
13. Прогоняет `preflight.py` и печатает итоговый блок.

### Повторный запуск

Безопасен и служит **обновлением кода**:

```bash
bash /root/dokumatika/scripts/bootstrap.sh dokumatika.ru -y
```

`.env` сохраняется целиком — дописываются только недостающие ключи. Секреты не
перегенерируются, сертификат не перевыпускается. `ASSET_VERSION` поднимается на
каждой выкатке автоматически, поэтому забыть про неё после правки css или js
физически невозможно.

### Если сертификат не выпустился

Единственное, чего скрипт сделать не может, — направить DNS. Если A-запись ещё
не указывает на сервер, скрипт настроит всё остальное, оставит временный
конфиг nginx, объяснит проблему, покажет, во что разрешается домен и какие
адреса у машины, и завершится с кодом 3.

Когда DNS разойдётся — просто повторите запуск.

---

## Проверка после выкатки

```bash
bash /opt/dokumatika/scripts/smoke_prod.sh dokumatika.ru
```

Скрипт ходит на боевой сайт снаружи и проверяет то же, что увидит посетитель:
коды ответа ключевых адресов, TLS и остаток срока сертификата, редиректы с
`http` и с `www`, заголовки безопасности, что `/admin/` не пускает без токена,
что база жива и что в подвале есть реквизиты. Печатает `[ok]` / `[FAIL]` и
возвращает ненулевой код при провале — годится и для мониторинга.

Запускать после каждого обновления кода: `systemctl status` бывает зелёным и
тогда, когда посетитель видит 502.

Отдельно — готовность конфигурации (реквизиты, ключи, права на базу):

```bash
sudo -u dokumatika python3 /opt/dokumatika/scripts/preflight.py --env-file /etc/dokumatika/.env
```

Запускать именно от `dokumatika`, а не от root: так вы заодно проверяете, что у
сервиса есть доступ к базе.

---

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

## Подключение Robokassa

Единственный блок, который целиком лежит на владельце, — поэтому он здесь, а не
в справочнике внизу.

### Что должно быть на сайте до подачи заявки

Robokassa проверяет сайт руками, и отказ по формальному признаку стоит
нескольких дней. `smoke_prod.sh` проверяет этот список автоматически, кроме
последнего пункта:

- [ ] сайт открывается по https и доступен публично;
- [ ] **реквизиты в подвале**: «Самозанятый Фамилия Имя Отчество, ИНН 000000000000, город»;
- [ ] **оферта** — `/oferta/`, с ценой, предметом, порядком получения;
- [ ] **контакты** на видном месте — `/kontakty/`, рабочий e-mail;
- [ ] **политика обработки персональных данных** — `/privacy/`;
- [ ] **порядок возврата** — `/vozvrat/`;
- [ ] описание товара с **реальной ценой 799 ₽**, совпадающей с той, что уйдёт в чек.

### Кабинет Robokassa

1. Зарегистрировать магазин, указать сайт `https://dokumatika.ru`.
2. «Мои магазины» → «Технические настройки»:
   - Result URL: `https://dokumatika.ru/robokassa/result`, метод **POST**;
   - Success URL: `https://dokumatika.ru/oplata/uspeh/`, метод GET;
   - Fail URL: `https://dokumatika.ru/oplata/otmena/`, метод GET;
   - алгоритм расчёта хеша — **тот же, что в `ROBOKASSA_HASH_ALGORITHM`**
     (по умолчанию в кабинете стоит MD5, у нас в `.env` — sha256; расхождение
     даёт на платёжной странице ошибку 29 и больше ничего не сообщает).
3. Забрать Пароль #1, Пароль #2 и тестовые пароли, положить в `.env`,
   затем `systemctl restart dokumatika`.

### Чеки для самозанятого

Онлайн-касса самозанятому не нужна (ч. 2.2 ст. 2 54-ФЗ) — чек формируется в
«Мой налог». Чтобы он выбивался автоматически при каждой оплате, подключите
бесплатный сервис **«Робочеки СМЗ»**: в приложении «Мой налог» разрешите
интеграцию с Robokassa (Настройки → Партнёры), после чего включите Робочеки в
кабинете Robokassa. Чек по электронной оплате обязан выбиваться **в момент
расчёта** (ст. 14 422-ФЗ).

Комиссия Robokassa: карты РФ ~3,4–3,9%, СБП ~3,0–3,5%. С 799 ₽ после комиссии и
налога НПД 4% остаётся примерно 740 ₽.

### Тестовый, а затем боевой платёж

```bash
# в .env: ROBOKASSA_TEST_MODE=1
systemctl restart dokumatika
```

Пройдите путь целиком: `/komplekt/` → оформление → тестовая оплата → возврат на
сайт → страница `/zakaz/<токен>/` с документами → письмо на почту. Затем
проверьте, что колбэк дошёл:

```bash
tail -5 /var/log/nginx/dokumatika-robokassa.log     # ожидаем 200, не 403
journalctl -u dokumatika | grep -E 'order_paid|order_email'
```

**403 в этом логе** означает, что запрос пришёл не с адресов Robokassa
(185.59.216.65 / 185.59.217.65) и был отсечён IP-фильтром. Фильтр — это второй
рубеж, подпись всё равно проверяется в приложении; как быстро его снять,
написано прямо в `dokumatika.conf` над блоком `location = /robokassa/result`.

После успешного теста — `ROBOKASSA_TEST_MODE=0`, перезапуск, и покупка
комплекта самому себе настоящей картой за настоящие 799 ₽. Это единственный
способ убедиться, что боевые пароли, чек и доставка работают.

- [ ] деньги списались, страница вернулась на сайт;
- [ ] `/zakaz/<токен>/` показывает документы;
- [ ] письмо со ссылкой пришло (и не в спам);
- [ ] чек пришёл покупателю и виден в «Мой налог»;
- [ ] заказ в статусе `paid` в админке;
- [ ] в `/var/log/nginx/dokumatika-robokassa.log` — код 200.

---

## Эксплуатация

### Обновление кода

Повторный `bootstrap.sh` — и есть обновление: он снимает копию кода, чинит
права, поднимает `ASSET_VERSION` и перезапускает сервис.

```bash
rsync -az --exclude '.git' --exclude 'var' --exclude '.env' ./ root@СЕРВЕР:/root/dokumatika/
ssh root@СЕРВЕР 'systemctl start dokumatika-backup &&
                 bash /root/dokumatika/scripts/bootstrap.sh dokumatika.ru -y'
bash scripts/smoke_prod.sh dokumatika.ru
```

Копия базы перед выкладкой — 10 секунд, которые однажды спасут. Схема базы
создаётся сама при старте (`CREATE TABLE IF NOT EXISTS`), отдельного шага
миграции нет.

### Вход в админку

Адрес — `https://dokumatika.ru/admin/`, токен из `ADMIN_TOKEN` в `.env`.
**Токен вводится в форму на странице, а не в адресную строку**: query-строка
целиком ложится в access-лог nginx, в историю браузера и в Referer, а по этому
токену видны почты всех покупателей. Ссылка вида `/admin/?token=...` не
сработает намеренно.

Забыли токен:

```bash
grep '^ADMIN_TOKEN=' /etc/dokumatika/.env
```

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

`bootstrap.sh` уже поставил её в `/etc/cron.d/dokumatika-reconcile`, раз в 15
минут. Если ResultURL не дошёл (сервер лежал, сертификат протух, сработал
IP-фильтр), заказ останется в `pending`; скрипт сам спросит статус у Robokassa
и, при подтверждении оплаты, отметит заказ и дошлёт письмо.

```bash
journalctl -t dokumatika-reconcile --since today
sudo -u dokumatika python3 /opt/dokumatika/scripts/reconcile_payments.py \
     --env-file /etc/dokumatika/.env --dry-run
```

В тестовом режиме скрипт откажется работать — OpStateExt тестовые операции не
показывает. Пока Robokassa не настроена, он раз в 15 минут пишет об этом в
журнал: это не поломка, а напоминание.

### Резервные копии

Таймер включён `bootstrap.sh`, копия снимается ежедневно в 04:17.

```bash
systemctl list-timers dokumatika-backup.timer
systemctl start dokumatika-backup          # прогнать прямо сейчас
ls -lh /var/backups/dokumatika/
```

Копия снимается через `VACUUM INTO` — согласованный снимок живой базы.
Копировать файл базы через `cp` **нельзя**: в режиме WAL свежие записи лежат в
соседнем `-wal`, и такая копия будет либо устаревшей, либо битой.

Хранится 14 последних копий, каждая проверяется `PRAGMA integrity_check` сразу
после создания. Порядок восстановления описан в конце `scripts/backup.sh`.

**Проверьте восстановление хотя бы один раз** — на тестовой копии, до того как
оно понадобится в панике. Бэкап, из которого ни разу не восстанавливались, —
это не бэкап, а надежда. И сразу настройте выгрузку копий с сервера к себе:
копия рядом с базой не спасает от потери самого сервера.

### Отдать сайт поисковикам

```bash
sudo -u dokumatika python3 /opt/dokumatika/scripts/indexnow_ping.py \
     --env-file /etc/dokumatika/.env
```

Ключ IndexNow уже сгенерирован `bootstrap.sh`; файл-подтверждение
`https://dokumatika.ru/<INDEXNOW_KEY>.txt` приложение отдаёт само. Ответы 200 и
202 — успех. Яндекс.Вебмастер и Google Search Console остаются за владельцем:
IndexNow ускоряет обход, но не заменяет их.

### Изменение цены

Цена живёт единственным числом `amount_minor` в `src/app/products.py`. Меняя её,
поднимите заодно `UPDATED`/`EDITION` в `legal_oferta.py` и `legal_vozvrat.py` и
`LEGAL_TEXTS_VERSION` в `handlers.py` — иначе в заказе зафиксируется версия
текстов, не соответствующая цене, которую человек принял. Затем выкатка и
проверка цены на `/komplekt/`, в оферте и в JSON-LD.

### Куда смотреть, когда что-то не так

```bash
journalctl -u dokumatika -n 100 --no-pager        # логи приложения (JSON построчно)
journalctl -u dokumatika -p err --since today     # только ошибки
tail -f /var/log/nginx/dokumatika-error.log
tail -f /var/log/nginx/dokumatika-robokassa.log   # платёжные уведомления
systemd-cgtop -1 --depth=2                        # кто ест память и CPU
curl -s https://dokumatika.ru/healthz             # {"status": "ok"} — база жива
```

Персональных данных и секретов в логах нет: почта маскируется, подписи и пароли
не пишутся вообще.

---

# Справочник: ручные шаги

Ниже — то же самое, что делает `bootstrap.sh`, но руками. Нужно только для
отладки: когда скрипт остановился на конкретном шаге и надо понять, почему.
В обычной выкатке эти команды выполнять не требуется.

## Пакеты и файрвол

```bash
apt update && apt upgrade -y
apt install -y nginx python3 sqlite3 certbot python3-certbot-nginx \
               rsync curl ca-certificates ufw cron
python3 --version    # должно быть 3.10 или новее

ufw allow OpenSSH
ufw allow 80/tcp
ufw allow 443/tcp
ufw --force enable
```

Файрвол включаем **после** разрешения ssh — иначе выкатка закончится потерей
доступа к серверу.

## Swap 2 ГБ и параметры памяти

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
vm.swappiness = 10
vm.vfs_cache_pressure = 50
net.core.somaxconn = 1024
EOF
sysctl --system
```

Тонкость: в юните `dokumatika.service` стоит `MemorySwapMax=0` — самому
приложению своп запрещён. Это осознанно: ушедшая в своп страница превращает
ответ за 5 мс в ответ за полсекунды. Своп нужен системе и соседям, а не сайту.

## Ограничить журналы

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

## Пользователь и каталоги

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
заранее — при `ProtectSystem=strict` сервис бэкапа создать его не сможет и не
стартует вовсе.

## Выложить код

```bash
rsync -az --delete \
      --exclude '.git' --exclude 'var' --exclude '.env' \
      --exclude '__pycache__' --exclude '*.pyc' \
      ./ root@СЕРВЕР:/opt/dokumatika/

chown -R root:root /opt/dokumatika
find /opt/dokumatika -type d -exec chmod 0755 {} +
find /opt/dokumatika -type f -exec chmod 0644 {} +
chmod 0755 /opt/dokumatika/scripts/*.sh
```

Права 0755/0644 нужны и nginx: он отдаёт статику прямо с диска из
`/opt/dokumatika/src/static`.

## Файл окружения

```bash
touch /etc/dokumatika/.env
chown root:dokumatika /etc/dokumatika/.env
chmod 0640 /etc/dokumatika/.env

python3 -c "import secrets; print('ADMIN_TOKEN=' + secrets.token_urlsafe(32))"
python3 -c "import secrets; print('INDEXNOW_KEY=' + secrets.token_hex(16))"
```

Формат — systemd `EnvironmentFile`: без кавычек вокруг значений, без подстановок
`$VAR`, комментарии с `#`. Полный набор ключей с пояснениями — в `.env.example`
и в шаблоне, который пишет `bootstrap.sh`.

**Пути обязаны быть именно здесь, а не только в юните.** Юнит задаёт их через
`Environment=` — но эти значения видит только сам сервис. Скрипты
`preflight.py`, `reconcile_payments.py`, `indexnow_ping.py` и `backup.sh`
запускаются отдельно (по таймеру или руками) и читают `/etc/dokumatika/.env`.
Без `DATABASE_PATH` в файле сверка платежей полезет за базой по пути из кода —
то есть в каталог с исходниками, — не найдёт там заказов и промолчит. Зависший
платёж останется незамеченным.

## systemd

```bash
cp /opt/dokumatika/deployment/systemd/apps.slice                 /etc/systemd/system/
cp /opt/dokumatika/deployment/systemd/dokumatika.service         /etc/systemd/system/
cp /opt/dokumatika/deployment/systemd/dokumatika-backup.service  /etc/systemd/system/
cp /opt/dokumatika/deployment/systemd/dokumatika-backup.timer    /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now dokumatika
systemctl enable --now dokumatika-backup.timer
systemctl status dokumatika --no-pager
```

Проверка, что приложение действительно отвечает:

```bash
curl -sS http://127.0.0.1:8081/healthz
# {"status": "ok"}
```

Наружу `/healthz` отдаёт только «жив или нет»: время с рестарта, состояние
платёжного контура и размер WAL видны лишь по админ-токену — по ним читается
окно, когда ResultURL мог не дойти.

```bash
curl -sS -H "X-Admin-Token: $(grep '^ADMIN_TOKEN=' /etc/dokumatika/.env | cut -d= -f2-)" \
     http://127.0.0.1:8081/healthz
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

## nginx и TLS

DNS домена должен уже указывать A-записью на IP сервера — проверьте до того, как
запускать certbot: `getent ahostsv4 dokumatika.ru`.

**1. Временный конфиг только для проверки владения доменом.** Боевой конфиг
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

**2. Сертификат** на оба имени (www нужен, чтобы редирект с него работал по
https, а не через предупреждение браузера):

```bash
certbot certonly --webroot -w /var/www/certbot \
        -d dokumatika.ru -d www.dokumatika.ru \
        --email hello@dokumatika.ru --agree-tos --no-eff-email
ls /etc/letsencrypt/live/dokumatika.ru/
```

`certbot certonly`, в отличие от установщика nginx, **не раскладывает**
`/etc/letsencrypt/options-ssl-nginx.conf` и `ssl-dhparams.pem`, а боевой конфиг
их подключает. Если их нет — скопируйте из пакета certbot
(`find /usr/lib/python3 -name options-ssl-nginx.conf`) или сгенерируйте
`openssl dhparam -out /etc/letsencrypt/ssl-dhparams.pem 2048`.

**3. Боевой конфиг:**

```bash
rm /etc/nginx/sites-enabled/dokumatika-acme.conf
install -d /etc/nginx/snippets
cp /opt/dokumatika/deployment/nginx/security-headers.conf \
   /etc/nginx/snippets/dokumatika-security-headers.conf
sed 's/dokumatika\.ru/ВАШ-ДОМЕН/g' /opt/dokumatika/deployment/nginx/dokumatika.conf \
   > /etc/nginx/sites-available/dokumatika.conf
ln -sf /etc/nginx/sites-available/dokumatika.conf /etc/nginx/sites-enabled/
nginx -t && systemctl reload nginx
```

Замена по точному `dokumatika.ru` безопасна: имена зон `limit_req_zone`,
`upstream`, путей и логов начинаются с `dokumatika` без `.ru` и не задеваются.

Если `nginx -t` ругается на `http2 on;` — у вас nginx старше 1.25.1 (`nginx -v`;
в Debian 12 это 1.22, в Ubuntu 22.04 — 1.18). Удалите строки `http2 on;` и
допишите суффикс: `listen 443 ssl http2;`.

**4. Автопродление** сертификата:

```bash
certbot renew --dry-run
systemctl list-timers certbot.timer
```

Продление обслуживает таймер из пакета certbot. Проверьте `--dry-run` сейчас, а
не через 80 дней: перевыпуск через webroot ломается, если временный ACME-конфиг
удалён, а в боевом нет `location /.well-known/acme-challenge/` (в нашем — есть,
в HTTP-блоке).

## Проверка снаружи руками

Всё это делает `smoke_prod.sh`; здесь — если нужно посмотреть глазами.

```bash
curl -sI https://dokumatika.ru/ | sort
curl -sI http://dokumatika.ru/            | grep -i location   # 301 на https
curl -sI https://www.dokumatika.ru/       | grep -i location   # 301 на apex
curl -s  https://dokumatika.ru/healthz
curl -s  https://dokumatika.ru/robots.txt
```

Проверка лимита частоты (должны появиться 429):

```bash
for i in $(seq 1 60); do curl -so /dev/null -w '%{http_code} ' https://dokumatika.ru/; done; echo
```

---

## Что здесь осознанно не идеально

Честный список компромиссов — чтобы через полгода не выяснять их заново.

**1. CSP и счётчик Метрики.** Политика строгая: `script-src 'self'`. Весь наш JS
живёт отдельными файлами в `/js/`, включая автосабмит формы оплаты, поэтому
исключений для своих страниц не нужно. А вот счётчик Яндекс.Метрики вставляется
inline и грузит `tag.js` с `mc.yandex.ru` — строгая CSP его блокирует. Если
включаете `METRIKA_ID`, замените CSP на закомментированный вариант в
`security-headers.conf`. Цена — `'unsafe-inline'` в `script-src`, то есть
основная защита от XSS. Альтернатива: обойтись встроенной воронкой в `/admin/`,
она считает события на сервере и не зависит от блокировщиков.

**2. HSTS выключен.** Заголовок в `security-headers.conf` закомментирован
намеренно. Он запоминается браузером на год, и если TLS сломается, посетители
получат страницу ошибки без обхода. Включать через неделю стабильной работы,
после успешного `certbot renew --dry-run`.

**3. IP-фильтр на колбэке Robokassa.** Адреса 185.59.216.65 и 185.59.217.65 взяты
из документации и могут смениться без предупреждения. Это второй рубеж, не
первый: настоящая проверка — подпись на Пароле #2 внутри приложения. Страховка
от смены адресов — сверка платежей по крону.

**4. SQLite, а не PostgreSQL.** При десятках заказов в месяц отдельная СУБД съела
бы 200–300 МБ ни за что. Интерфейс репозиториев узкий: переезд при росте — это
переписать один модуль, не трогая HTTP-слой.

**5. Заглушка обслуживания без оформления.** См. выше про CSP и inline-стили.

**6. `bootstrap.sh` включает ufw.** Если сервер уже жил со своими правилами
файрвола, скрипт их не удаляет, но `ufw --force enable` может конфликтовать с
ручным iptables. На чистой машине это правильное поведение, на обжитой —
посмотрите `ufw status` до и после.

---

## Финальный чек-лист

- [ ] `bootstrap.sh` отработал без ошибок и напечатал токен админки
- [ ] `smoke_prod.sh` не выдаёт `[FAIL]`
- [ ] `preflight.py` не выдаёт `[FAIL]`
- [ ] реквизиты продавца заполнены и видны в подвале
- [ ] `certbot renew --dry-run` проходит
- [ ] бэкап создан, из копии хотя бы раз восстанавливались
- [ ] выгрузка копий с сервера к себе настроена
- [ ] алгоритм подписи в `.env` и в кабинете Robokassa совпадает — проверено глазами
- [ ] `ROBOKASSA_TEST_MODE=0`
- [ ] боевой платёж прошёл целиком: деньги, документы, письмо, чек
- [ ] `journalctl -t dokumatika-reconcile` показывает работу сверки
- [ ] IndexNow отправлен, sitemap добавлен в Яндекс.Вебмастер
- [ ] уведомление в Роскомнадзор за сам сайт подано
