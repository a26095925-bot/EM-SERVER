# Roblox-Style Game Backend (FastAPI + Cloudflare R2 + bcrypt)

Цей сервер забезпечує:
1. Безпечну реєстрацію та авторизацію користувачів з хешуванням паролів (**bcrypt**).
2. Захист запитів через **JWT токени**.
3. Збереження та завантаження створених гравцями плейсів у хмару **Cloudflare R2** без бази даних (всі акаунти та плейси зберігаються як JSON у вашому бакеті).

## Змінні середовища для Render.com (Environment Variables):

У панелі вашого сервісу на Render (`Environment` -> `Add Environment Variable`) додайте:
* `R2_ACCOUNT_ID`: `4cf18f6b81f018297ba7018547ad6005`
* `R2_BUCKET_NAME`: `aient`
* `R2_ACCESS_KEY_ID`: *(Ваш R2 Token Access Key)*
* `R2_SECRET_ACCESS_KEY`: *(Ваш R2 Token Secret Key)*
* `JWT_SECRET`: *(Будь-який довгий секретний рядок для підпису токенів)*

### Де взяти R2 Keys у Cloudflare:
1. Зайдіть у Cloudflare Dashboard -> **R2 Object Storage**.
2. Праворуч натисніть **Manage R2 API Tokens**.
3. Натисніть **Create API Token**, надайте права **Object Read & Write**.
4. Скопіюйте `Access Key ID` та `Secret Access Key` в змінні Render.
