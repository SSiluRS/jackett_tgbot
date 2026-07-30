# Jackett Telegram Bot

Telegram-бот для поиска торрентов через Jackett API и автоматической отправки файлов `.torrent` и `.magnet` в папку автозагрузки qBittorrent.

## Особенности
- Поиск по всем трекерам Jackett.
- Поддержка скачивания `.torrent` файлов и сохранения `.magnet` ссылок.
- Работа через HTTP/HTTPS прокси (Xray / VLESS).
- Ограничение доступа по `ALLOWED_ID` / `USERS`.
- Экранирование HTML спецсимволов.

## Быстрый запуск через Docker Compose

```bash
docker-compose up -d --build jackett2telegram
```

## Переменные окружения

| Переменная | Описание |
|------------|----------|
| `TG_TOKEN` | Токен Telegram бота |
| `USERS` / `ALLOWED_ID` | Telegram ID пользователей через запятую |
| `JACKETT_URL` | Адрес Jackett API (например, `http://jackett:9117`) |
| `JACKETT_API` | API-ключ Jackett |
| `HTTP_PROXY` | Прокси для обращений к Jackett/трекерам |
| `WATCH_DIR` | Папка назначения автозагрузки (по умолчанию `/watch`) |
