"""Telegram-бот для раскладки закупок по поставщикам."""

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime

from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramUnauthorizedError
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    BotCommand,
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from .ai import AIClient, AIError, Claude, Gemini
from .config import Config
from .db import DB
from .excel import build_workbook
from .files import UnsupportedFile, price_chunks, text_chunks
from . import planner
from .models import Plan
from .planner import build_plan, fmt_money, summary_text, supplier_messages

log = logging.getLogger(__name__)

PRICE_SESSION_TTL = 30 * 60  # сколько ждём файлы после /price, сек
TG_LIMIT = 4000

HELP = """Я раскладываю списки закупок по поставщикам.

Как пользоваться:
1. Управляющий загружает прайсы в личке со мной: /price Название поставщика, затем присылает Excel, PDF, фото или текст прайса. Новый прайс заменяет старый у этого поставщика.
2. Повара и бармены пишут в общую группу, что нужно купить, как обычно.
3. Когда пора заказывать, пишите в группе /itog. Я разложу всё по поставщикам (беру где дешевле) и предложу, в каком виде прислать заказ: Excel-таблицей, одним сообщением или отдельным сообщением для каждого поставщика.

Команды:
/itog — собрать заказ из всех списков и обнулить их
/list — что я уже собрал в этом чате
/clear — удалить собранные списки без заказа
/price Поставщик — загрузить прайс (в личке)
/done — закончить загрузку прайса
/prices — какие прайсы загружены
/delprice Поставщик — удалить прайс"""


@dataclass
class PriceSession:
    supplier: str
    started: float
    replaced: bool = False
    added: int = 0


class App:
    def __init__(self, config: Config, db: DB, ai: AIClient):
        self.config = config
        self.db = db
        self.ai = ai
        self.sessions: dict[int, PriceSession] = {}
        self.busy_chats: set[int] = set()
        self.router = Router()
        self._register()

    def is_admin(self, user_id: int | None) -> bool:
        return not self.config.admin_ids or user_id in self.config.admin_ids

    def session(self, user_id: int) -> PriceSession | None:
        s = self.sessions.get(user_id)
        if s and time.time() - s.started > PRICE_SESSION_TTL:
            del self.sessions[user_id]
            return None
        return s

    def _register(self) -> None:
        r = self.router
        r.message.register(self.cmd_start, CommandStart())
        r.message.register(self.cmd_start, Command("help"))
        r.message.register(self.cmd_price, Command("price"))
        r.message.register(self.cmd_done, Command("done"))
        r.message.register(self.cmd_prices, Command("prices"))
        r.message.register(self.cmd_delprice, Command("delprice"))
        r.message.register(self.cmd_itog, Command("itog"))
        r.message.register(self.cmd_list, Command("list"))
        r.message.register(self.cmd_clear, Command("clear"))
        r.callback_query.register(self.on_format, F.data.startswith("fmt:"))
        r.message.register(self.on_private_content, F.chat.type == ChatType.PRIVATE, self._has_session)
        r.message.register(self.on_text, F.text, ~F.text.startswith("/"))
        r.edited_message.register(self.on_edit, F.text)

    def _has_session(self, message: Message) -> bool:
        return message.from_user is not None and self.session(message.from_user.id) is not None

    # --- общие команды ---

    async def cmd_start(self, message: Message) -> None:
        uid = message.from_user.id if message.from_user else "?"
        await message.answer(f"{HELP}\n\nВаш Telegram ID: {uid}")

    # --- прайсы ---

    async def cmd_price(self, message: Message, command: CommandObject) -> None:
        if not self.is_admin(message.from_user.id):
            await message.answer("Загружать прайсы может только управляющий.")
            return
        if message.chat.type != ChatType.PRIVATE:
            await message.answer("Прайсы загружайте мне в личку, чтобы не засорять группу.")
            return
        name = (command.args or "").strip()
        if not name:
            await message.answer("Напишите название поставщика: /price Метро")
            return
        supplier = self.db.canonical_supplier(name)
        self.sessions[message.from_user.id] = PriceSession(supplier, time.time())
        if message.document or message.photo:
            await self.on_private_content(message)
            return
        await message.answer(
            f"Жду прайс «{supplier}»: пришлите Excel, CSV, PDF, фото или текст. "
            "Можно несколько файлов подряд. Когда закончите — /done"
        )

    async def cmd_done(self, message: Message) -> None:
        s = self.sessions.pop(message.from_user.id, None)
        if not s:
            await message.answer("Сейчас прайс не загружается.")
            return
        if not s.added:
            await message.answer(
                f"Прайс «{s.supplier}» не загрузился, прежний прайс (если был) остался как есть."
            )
            return
        await message.answer(f"Готово: у «{s.supplier}» в базе {plural(s.added, 'позиция', 'позиции', 'позиций')}.")

    async def on_private_content(self, message: Message) -> None:
        s = self.session(message.from_user.id)
        if s is None:
            return
        try:
            if message.document:
                doc = message.document
                if doc.file_size and doc.file_size > 20 * 1024 * 1024:
                    await message.answer("Файл больше 20 МБ, Telegram не даст его скачать.")
                    return
                data = (await message.bot.download(doc)).read()
                filename = doc.file_name or "file"
                chunks = price_chunks(filename, data, doc.mime_type)
            elif message.photo:
                data = (await message.bot.download(message.photo[-1])).read()
                filename = "photo.jpg"
                chunks = price_chunks(filename, data, "image/jpeg")
            elif message.text:
                filename = "текст"
                chunks = text_chunks(message.text)
            else:
                await message.answer("Пришлите файл, фото или текст прайса.")
                return
        except UnsupportedFile as e:
            await message.answer(str(e))
            return

        if not chunks:
            await message.answer("В файле не нашлось данных.")
            return
        total = len(chunks)
        parts = f", {plural(total, 'часть', 'части', 'частей')}" if total > 1 else ""
        status = await message.answer(f"Читаю прайс «{s.supplier}»{parts}… Это может занять пару минут.")
        state = {"done": 0}

        def status_text() -> str:
            done = f", готово {state['done']} из {total}" if total > 1 else ""
            return f"Читаю прайс «{s.supplier}»{done}… прошло {_elapsed(started)}"

        async def progress(done: int, _total: int) -> None:
            state["done"] = done
            await _safe_edit(status, status_text())

        started = time.monotonic()
        pulse = asyncio.create_task(_pulse(status, status_text))
        error = None
        try:
            items = await self.ai.extract_prices(chunks, filename, progress)
        except AIError as e:
            error = f"Не получилось прочитать прайс: {e}"
        except Exception:
            log.exception("price parse failed")
            error = "Не получилось прочитать прайс, попробуйте другой формат."
        finally:
            pulse.cancel()
        if error:
            await status.edit_text(error)
            return
        if not items:
            await status.edit_text("Не нашёл в этом прайсе ни одной позиции.")
            return

        self.db.add_items(s.supplier, items, replace=not s.replaced)
        s.replaced = True
        s.added += len(items)
        s.started = time.time()
        no_price = sum(1 for i in items if i["price"] is None)
        sample = "\n".join(
            f"• {i['name']} — {fmt_money(i['price'])}/{i['unit'] or 'ед.'}" for i in items[:5]
        )
        extra = f"\nБез цены: {no_price}." if no_price else ""
        await status.edit_text(
            f"«{s.supplier}»: добавлено {plural(len(items), 'позиция', 'позиции', 'позиций')}.{extra}\n\nНапример:\n{sample}\n\n"
            "Пришлите ещё файл или /done, если всё."
        )

    async def cmd_prices(self, message: Message) -> None:
        rows = self.db.suppliers()
        if not rows:
            await message.answer("Прайсов пока нет. Загрузите: /price Поставщик (в личке).")
            return
        text = "Загруженные прайсы:\n" + "\n".join(f"• {s} — {n} поз." for s, n in rows)
        await message.answer(text)

    async def cmd_delprice(self, message: Message, command: CommandObject) -> None:
        if not self.is_admin(message.from_user.id):
            await message.answer("Удалять прайсы может только управляющий.")
            return
        name = (command.args or "").strip()
        if not name:
            await message.answer("Напишите название: /delprice Метро")
            return
        supplier = self.db.canonical_supplier(name)
        n = self.db.delete_supplier(supplier)
        await message.answer(f"Удалил прайс «{supplier}» ({n} поз.)." if n else "Такого прайса нет.")

    # --- сбор списков ---

    async def on_text(self, message: Message) -> None:
        if message.from_user is None or message.from_user.is_bot:
            return
        self.db.save_pending(
            message.chat.id, message.message_id, message.from_user.full_name, message.text
        )

    async def on_edit(self, message: Message) -> None:
        self.db.update_pending(message.chat.id, message.message_id, message.text)

    async def cmd_list(self, message: Message) -> None:
        pending = self.db.pending(message.chat.id)
        if not pending:
            await message.answer("Пока ничего не собрал.")
            return
        lines = [f"Собрано сообщений: {len(pending)}"]
        for m in pending[-15:]:
            preview = " ".join(m.text.split())
            lines.append(f"• {m.author}: {preview[:80]}{'…' if len(preview) > 80 else ''}")
        if len(pending) > 15:
            lines.insert(1, "(последние 15)")
        await message.answer("\n".join(lines))

    async def cmd_clear(self, message: Message) -> None:
        if not self.is_admin(message.from_user.id):
            await message.answer("Очищать списки может только управляющий.")
            return
        self.db.delete_pending(message.chat.id)
        await message.answer("Собранные списки удалены.")

    async def on_format(self, callback: CallbackQuery) -> None:
        try:
            _, order_id, kind = callback.data.split(":")
            order = self.db.get_order(int(order_id))
        except ValueError:
            order = None
        if order is None or kind not in FORMATS:
            await callback.answer("Этот заказ устарел, соберите новый: /itog", show_alert=True)
            return
        if not self.is_admin(callback.from_user.id):
            await callback.answer("Получить заказ может только управляющий.", show_alert=True)
            return
        await callback.answer()
        chat_id, title, data = order
        plan = Plan.from_dict(data)
        bot = callback.bot
        target = callback.message.chat.id if callback.message else chat_id

        if kind == "xlsx":
            stamp = title.removeprefix("Заказ от ").replace(".", "-").replace(":", "-").replace(" ", "_")
            filename = f"zakaz_{stamp or order_id}.xlsx"
            await bot.send_document(
                target,
                BufferedInputFile(build_workbook(plan, title), filename),
                caption=summary_text(plan, title)[:1000],
            )
        elif kind == "one":
            for chunk in _pack([title, *supplier_messages(plan)]):
                await bot.send_message(target, chunk)
        else:
            texts = supplier_messages(plan)
            if not texts:
                await bot.send_message(target, "В заказе нет позиций.")
            for text in texts:
                for chunk in _pack([text]):
                    await bot.send_message(target, chunk)

    async def cmd_itog(self, message: Message) -> None:
        chat_id = message.chat.id
        if not self.is_admin(message.from_user.id):
            await message.answer("Собирать заказ может только управляющий.")
            return
        if chat_id in self.busy_chats:
            await message.answer("Уже собираю заказ, подождите.")
            return
        pending = self.db.pending(chat_id)
        if not pending:
            await message.answer("Списков нет: в чате ещё никто ничего не написал.")
            return
        catalog = self.db.catalog()
        if not catalog:
            await message.answer("Сначала загрузите прайсы: /price Поставщик (в личке со мной).")
            return

        self.busy_chats.add(chat_id)
        what = f"Разбираю {plural(len(pending), 'сообщение', 'сообщения', 'сообщений')}"
        status = await message.answer(f"{what}…")
        started = time.monotonic()
        pulse = asyncio.create_task(_pulse(status, lambda: f"{what}… прошло {_elapsed(started)}"))
        try:
            lines = await self.ai.match_order(catalog, pending)
            pulse.cancel()
            plan = build_plan(lines, catalog)
            if not plan.by_supplier and not plan.unmatched:
                await status.edit_text("Не нашёл в сообщениях ни одной позиции для заказа.")
                return
            title = f"Заказ от {datetime.now():%d.%m.%Y %H:%M}"
            order_id = self.db.save_order(chat_id, title, plan.to_dict())
            self.db.delete_pending(chat_id, [m.message_id for m in pending])
            counted = plural(len(pending), "сообщение", "сообщения", "сообщений")
            await status.edit_text(
                f"{summary_text(plan, title)}\n\nУчёл {counted}, списки обнулены.\n"
                "В каком виде прислать заказ?",
                reply_markup=format_keyboard(order_id),
            )
        except AIError as e:
            pulse.cancel()
            await status.edit_text(f"Не получилось собрать заказ: {e}\nСписки сохранены, попробуйте /itog ещё раз.")
        except Exception:
            pulse.cancel()
            log.exception("itog failed")
            await status.edit_text("Что-то пошло не так. Списки сохранены, попробуйте /itog ещё раз.")
        finally:
            pulse.cancel()
            self.busy_chats.discard(chat_id)


PULSE_SECONDS = 30

FORMATS = {
    "xlsx": "📊 Excel-таблица",
    "one": "💬 Одним сообщением",
    "each": "📨 По поставщикам",
}


def format_keyboard(order_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=label, callback_data=f"fmt:{order_id}:{kind}")]
            for kind, label in FORMATS.items()
        ]
    )


def _elapsed(started: float) -> str:
    sec = int(time.monotonic() - started)
    return f"{sec // 60} мин {sec % 60:02d} с" if sec >= 60 else f"{sec} с"


async def _safe_edit(message: Message, text: str) -> None:
    try:
        await message.edit_text(text)
    except Exception:
        log.debug("edit failed", exc_info=True)


async def _pulse(message: Message, text) -> None:
    """Раз в PULSE_SECONDS обновляет сообщение, чтобы было видно, что бот жив."""
    while True:
        await asyncio.sleep(PULSE_SECONDS)
        await _safe_edit(message, text())


def plural(n: int, one: str, few: str, many: str) -> str:
    if n % 10 == 1 and n % 100 != 11:
        word = one
    elif 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        word = few
    else:
        word = many
    return f"{n} {word}"


def _pack(texts: list[str]) -> list[str]:
    """Склеивает тексты в сообщения не длиннее лимита Telegram."""
    out, cur = [], ""
    for t in texts:
        while len(t) > TG_LIMIT:
            cut = t.rfind("\n", 0, TG_LIMIT)
            cut = cut if cut > 0 else TG_LIMIT
            if cur:
                out.append(cur)
                cur = ""
            out.append(t[:cut])
            t = t[cut:].lstrip("\n")
        if cur and len(cur) + 2 + len(t) > TG_LIMIT:
            out.append(cur)
            cur = t
        else:
            cur = f"{cur}\n\n{t}" if cur else t
    if cur:
        out.append(cur)
    return out


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    config = Config.from_env()
    planner.CURRENCY = config.currency
    if not config.admin_ids:
        log.warning("ADMIN_IDS не задан: загружать прайсы и делать /itog может любой.")
    if config.ai == "gemini":
        ai: AIClient = Gemini(
            config.model, config.gemini_api_key, fallback_model=config.gemini_fallback_model
        )
    else:
        ai = Claude(config.model)
    log.info("ИИ: %s (%s)", ai.name, config.model)
    app = App(config, DB(config.db_path), ai)
    bot = Bot(config.bot_token)
    dp = Dispatcher()
    dp.include_router(app.router)
    try:
        await _run(bot, dp)
    except TelegramUnauthorizedError:
        log.error("Telegram не принял BOT_TOKEN: проверьте токен в .env")
        raise SystemExit(1)
    finally:
        await bot.session.close()


async def _run(bot: Bot, dp: Dispatcher) -> None:
    await bot.set_my_commands(
        [
            BotCommand(command="itog", description="Собрать заказ по поставщикам"),
            BotCommand(command="list", description="Что уже собрано"),
            BotCommand(command="clear", description="Удалить собранные списки"),
            BotCommand(command="price", description="Загрузить прайс (в личке)"),
            BotCommand(command="done", description="Закончить загрузку прайса"),
            BotCommand(command="prices", description="Загруженные прайсы"),
            BotCommand(command="delprice", description="Удалить прайс"),
            BotCommand(command="help", description="Как пользоваться"),
        ]
    )
    log.info("Бот запущен")
    await dp.start_polling(bot, allowed_updates=["message", "edited_message", "callback_query"])


if __name__ == "__main__":
    asyncio.run(main())
