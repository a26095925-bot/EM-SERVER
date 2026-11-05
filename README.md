# Roblox-Style Game Backend (FastAPI + Cloudflare R2 + bcrypt)

## Render deployment

`server.py` і `requirements.txt` лежать у корені репозиторію. У Render залиште **Root Directory порожнім**, задайте Build Command `pip install -r requirements.txt` і Start Command:

```text
uvicorn server:app --host 0.0.0.0 --port $PORT
```

Файл `.python-version` фіксує Python 3.12: нові версії Pydantic у зафіксованих залежностях можуть не мати готового `pydantic-core` для Python 3.14, через що Render намагається зібрати Rust-пакет у середовищі без доступного Cargo cache.

Після деплою відкрийте кореневу адресу сервісу. У JSON мають бути `"status":"online"`, `"multiplayer_websocket":true` та `"models"` > 0. Це підтверджує, що Render запустив саме версію з маршрутом мультиплеєра та тулбоксом моделей.

Для версії 2.0.2 перевірте також `"auth_ready":true`, `"auth_key_source":"environment"` і непорожній `"deployment_commit"`. У `/health` є ті самі безпечні діагностичні поля, зокрема `instance_id`, щоб зіставити запити між інстансами. Секрет або його хеш ніколи не повертаються. Якщо `JWT_SECRET` відсутній чи коротший за 32 байти, Render-застосунок тепер зупиняється замість видавати токени, які перестануть працювати після перезапуску чи на іншому інстансі.

### Перевірка WebSocket

Клієнт підключається до `wss://<домен>/ws/<place_id>?token=<JWT>`. Для діагностики можна виконати handshake з будь-яким 32-символьним ID та навмисно недійсним токеном. У поточному застосунку очікується HTTP `403` (маршрут знайдено, токен відхилено). HTTP `404` означає, що запит потрапив на іншу/старішу версію застосунку або інший сервіс.

Render Web Services підтримують WebSocket-з'єднання; окремий перемикач увімкнення WebSocket не потрібен. Переконайтеся, що клієнт використовує `wss://` для HTTPS-домену. Кімнати зараз зберігаються в пам'яті одного процесу, тому мультиплеєр не синхронізується між кількома інстансами сервера.

## Що вміє сервер (2.0.2)

1. **Акаунти** — bcrypt-хешування паролів, JWT-сесії на 30 днів, захист від перебору.
2. **Плейси** — збереження/завантаження, шари, спавн, фізика (швидкість, стрибок, гравітація, смерть), GUI-елементи, PNG-обкладинка.
3. **Соціальні функції** — лайки (`/like`), перегляди (`/view`), запуски (`/play`), друзі, статистика смертей.
4. **Меню розробника** — `/api/dev/status`, `/api/dev/users`, `/api/dev/places`, видалення плейса, рекомендовані плейси.
5. **Тулбокс моделей** — 13 вбудованих `.obj` генеруються на диску сервера, завантаження власних `.obj/.glb/.gltf`, список, скачування та видалення.
6. **Адмінпанель** — перегляд гравців, kick/ban/mute, head/teleport; власник може видавати/відкликати додаткові ролі. Забанені не входять у мультиплеєр, повідомлення зам'ютених не передаються.
7. **Мультиплеєр** — presence, чат, live-редагування блоків автором плейса, GUI-стан.

## API

| Метод | Шлях | Опис |
| --- | --- | --- |
| POST | `/api/auth/register`, `/api/auth/login` | bcrypt + JWT |
| GET | `/api/auth/me` | перевірка токена, поточний користувач, права розробника |
| GET/PUT | `/api/skins/me` | завантаження та збереження аватара |
| POST | `/api/places/save` | створити/оновити плейс |
| GET | `/api/places/list?q=&author=&limit=` | каталог з лайками/переглядами/запусками |
| GET | `/api/places/{id}` | повний плейс |
| DELETE | `/api/places/{id}` | автор або адмін |
| POST | `/api/places/{id}/like` | перемкнути вподобайку |
| POST | `/api/places/{id}/view` | перегляд (1 на IP/плейс/10 хв) |
| POST | `/api/places/{id}/play` | запуск + статистика |
| POST | `/api/places/{id}/feature` | рекомендований плейс (dev) |
| GET | `/api/places/{id}/thumbnail` | PNG-обкладинка |
| GET | `/api/stats/me`, POST `/api/stats/death` | статистика гравця |
| GET/POST | `/api/friends`, `/api/friends/request|accept|remove` | друзі |
| GET | `/api/models` | каталог тулбокса |
| GET | `/api/models/{file}` | файл моделі |
| POST | `/api/models/upload` | multipart-завантаження (до 8 МБ) |
| DELETE | `/api/models/{file}` | автор моделі або адмін |
| GET | `/api/dev/status|users|places|whoami` | меню розробника |
| GET | `/api/admin/overview`, `/api/admin/admins` | активність і ролі (адмін / власник) |
| POST | `/api/admin/admins`, `/api/admin/action` | керування ролями та модерація |
| WS | `/ws/{place_id}?token=` | мультиплеєр |
| GET | `/health` | health-check |

## Моделі на диску сервера

* Вбудовані моделі генеруються під час старту в `server/storage/models/` (13 шт.: crystal, pyramid, spike, checkpoint_flag, coin, crate, barrel, star, sword, arch, tree, rock, mushroom).
* Завантажені моделі також зберігаються в `server/storage/models/` і дублюються в R2 (якщо налаштовано).
* Індекс — `models_index.json` (у R2 та локально).
* Клієнт синхронізує каталог у свою папку `assets/models/` і одразу може вставляти моделі у сцену або прив'язувати до блоків.

`STORAGE_DIR` дозволяє змінити каталог сховища (корисно для тестів або Render Disk).

## Змінні середовища

| Змінна | Обов'язково | Опис |
| --- | --- | --- |
| `JWT_SECRET` | так (для Render) | стабільний випадковий рядок ≥ 32 байт. На Render без нього сервер не стартує; локально для розробки створюється `storage/.jwt_secret` |
| `R2_ACCOUNT_ID`, `R2_BUCKET_NAME` | ні | мають значення за замовчуванням |
| `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY` | для хмари | без них дані живуть на локальному диску |
| `CORS_ORIGINS` | ні | список origin через кому; за замовчуванням `*` |
| `ADMIN_USERS` | для публічного сервера | логіни адмінів через кому |
| `OWNER_USERS` | так для керування ролями на Render | логіни вже створених акаунтів власників через кому; Render не використовує жодного власника за замовчуванням. Локально типово `mrkotik,arsen2014a`. Тільки ці акаунти можуть видавати/відкликати динамічні адмін-ролі |
| `ALLOW_OPEN_DEV` | ні | `1` (типово) відкриває dev-меню всім авторизованим, якщо `ADMIN_USERS` порожній; для публічного сервера поставте `0` |
| `STORAGE_DIR` | ні | шлях до сховища |

> **Безпека:** для публічного деплою задайте `JWT_SECRET`, `OWNER_USERS`, `ADMIN_USERS` і `ALLOW_OPEN_DEV=0`. Зарезервовані owner/admin імена не можна самостійно зареєструвати, тому вказуйте в Render лише вже створені акаунти. Динамічні адмін-ролі зберігаються у `admin/admins.json`; змінювати їх може лише власник. Без `OWNER_USERS` сервер не дозволить змінювати ролі.

## Локальний запуск

```powershell
cd server
$env:JWT_SECRET = "dev-secret-at-least-32-bytes-long-123456"
pip install -r requirements.txt
uvicorn server:app --host 0.0.0.0 --port 10000
```

Клієнт за замовчуванням використовує Render-адресу; у вікні входу або в налаштуваннях вкажіть `http://127.0.0.1:10000`.

## Де взяти R2 Keys у Cloudflare

1. Cloudflare Dashboard → **R2 Object Storage**.
2. **Manage R2 API Tokens** → **Create API Token** з правами **Object Read & Write**.
3. Скопіюйте `Access Key ID` і `Secret Access Key` у змінні Render.

## Обмеження

* Кімнати та rate-limit живуть у пам'яті одного процесу.
* Для кількох інстансів потрібне транзакційне сховище (Postgres/Redis).
* Локальне сховище на Render ефемерне; для довготривалих даних використовуйте R2 або Render Disk.
