# Деплой по пушу

На сервері лежить bare-репозиторій, у якого в `hooks/post-receive` стоїть
скрипт з цієї теки. Пуш у `main` викладає код у робочу теку, перебудовує **усі**
образи і піднімає стек.

## Перше налаштування на новому сервері

```bash
# 1. bare-репозиторій
git init --bare ~/repos/data_agregator.git
git -C ~/repos/data_agregator.git symbolic-ref HEAD refs/heads/main

# 2. хук (після першого пуша файл уже буде в робочій теці)
mkdir -p ~/deploy/data_agregator
cp ~/deploy/data_agregator/scripts/deploy/post-receive ~/repos/data_agregator.git/hooks/
chmod +x ~/repos/data_agregator.git/hooks/post-receive

# 3. секрети і порти цього хоста (у git не потрапляє)
python3 scripts/generate_secrets.py > ~/deploy/data_agregator/.env   # далі відредагувати порти
```

## Порти

Усе, крім веб-входу, слухає лише `127.0.0.1`. Назовні дивиться тільки
`HTTP_PORT`. На спільному сервері задайте в `.env` ті, що не конфліктують:

```
HTTP_PORT=8090
POSTGRES_PORT=55432
CLICKHOUSE_HTTP_PORT=58123
MINIO_PORT=59000
MINIO_CONSOLE_PORT=59001
NATS_PORT=54222
NATS_MONITOR_PORT=58222
TOR_PORT=59050
API_PORT=58000
FRONTEND_PORT=55173
TRAEFIK_DASHBOARD_PORT=58081
```

## Хости й адреси

Traefik за замовчуванням відповідає на будь-який `Host`, а SPA звертається до
API відносними URL — тож стек відкривається і за іменем, і за IP без
перезбірки. Щоб прив'язати до конкретного імені, задайте в `.env`:

```
TRAEFIK_FRONT_RULE=Host(`example.com`)
TRAEFIK_API_RULE=Host(`example.com`) && PathPrefix(`/api`)
```

`VITE_API_BASE` лишайте порожнім, якщо API живе на тому ж домені.

## Звідки пушити

```bash
git remote add prod mrgrinch@<host>:repos/data_agregator.git
git push prod main
```

Вивід збірки і підняття йде у відповідь на push і дублюється в
`~/deploy/deploy.log`. Якщо якийсь сервіс не піднявся, git повідомляє про
помилку хука і в логу видно, який саме — але гілка на сервері вже оновлена:
`post-receive` виконується після оновлення ref, відкотити його хук не може.
Тому виправляйте і пуште ще раз.
