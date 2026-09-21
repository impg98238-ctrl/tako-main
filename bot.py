"""Бот и сервер для Mini App «Арена».

Один процесс делает всё: отвечает в Telegram, раздаёт index.html и держит мультиплеер (WebSocket /ws).

Запуск:
    pip install aiogram          # aiohttp ставится вместе с ним
    export BOT_TOKEN="123:ABC..."                  # токен от @BotFather
    export WEBAPP_URL="https://твой-домен/"        # публичный HTTPS-адрес этого сервера
    python bot.py

Необязательно:
    PORT=8080            порт сервера
    DB_PATH=arcade.db    файл базы с балансами
    WS_URL=wss://...     адрес мультиплеера, если index.html лежит на другом хостинге
    ALLOW_GUEST=1        пускать без подписи Telegram (только для локальных тестов)
"""
import asyncio
import os
import urllib.parse
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import KeyboardButton, MenuButtonWebApp, Message, ReplyKeyboardMarkup, WebAppInfo
from aiohttp import web

import server

BOT_TOKEN = os.environ["BOT_TOKEN"]
WEBAPP_URL = os.environ["WEBAPP_URL"]
WS_URL = os.environ.get("WS_URL", "")
PORT = int(os.environ.get("PORT", "8080"))

dp = Dispatcher()


def app_url() -> str:
    if not WS_URL:
        return WEBAPP_URL
    sep = "&" if "?" in WEBAPP_URL else "?"
    return f"{WEBAPP_URL}{sep}ws={urllib.parse.quote(WS_URL, safe='')}"


@dp.message(CommandStart())
async def start(message: Message):
    keyboard = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="Играть", web_app=WebAppInfo(url=app_url()))]],
        resize_keyboard=True,
    )
    await message.answer("Ракетка и хоккей на виртуальные тако. Жми кнопку ниже.", reply_markup=keyboard)


async def main():
    bot = Bot(BOT_TOKEN)
    hub = server.Hub(
        BOT_TOKEN,
        db_path=os.environ.get("DB_PATH", "arcade.db"),
        allow_guest=os.environ.get("ALLOW_GUEST") == "1",
    )
    runner = web.AppRunner(server.make_app(hub, str(Path(__file__).with_name("index.html"))))
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    hub.start()
    await bot.set_chat_menu_button(
        menu_button=MenuButtonWebApp(text="Арена", web_app=WebAppInfo(url=app_url()))
    )
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
