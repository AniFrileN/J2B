"""Запросы к ИИ (Gemini или Claude): разбор прайсов и раскладка списков по позициям прайсов."""

import asyncio
import base64
import copy
import json
import io
import logging
from collections.abc import Awaitable, Callable

from .files import PriceChunk
from .models import CatalogItem, PendingMessage

log = logging.getLogger(__name__)

# Часть запроса: текст или файл (байты + MIME-тип).
Part = str | PriceChunk

Progress = Callable[[int, int], Awaitable[None]]

# Модели Claude, которые принимают серверный fallback при отказе.
FALLBACK_MODELS = {"claude-opus-5-5", "claude-opus-5", "claude-fable-5-1", "claude-sonnet-5-5"}

PRICE_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "unit": {"type": "string"},
                    "price": {"anyOf": [{"type": "number"}, {"type": "null"}]},
                },
                "required": ["name", "unit", "price"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["items"],
    "additionalProperties": False,
}

ORDER_SCHEMA = {
    "type": "object",
    "properties": {
        "lines": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "requested": {"type": "string"},
                    "author": {"type": "string"},
                    "quantity_text": {"type": "string"},
                    "candidates": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "item_id": {"type": "integer"},
                                "quantity": {"type": "number"},
                            },
                            "required": ["item_id", "quantity"],
                            "additionalProperties": False,
                        },
                    },
                    "comment": {"type": "string"},
                },
                "required": ["requested", "author", "quantity_text", "candidates", "comment"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["lines"],
    "additionalProperties": False,
}

PRICE_SYSTEM = """Ты помогаешь ресторану: разбираешь прайс-листы поставщиков продуктов и напитков.
Извлеки из присланного фрагмента прайса все товарные позиции.

Для каждой позиции:
- name: полное название, как в прайсе, вместе с брендом, фасовкой, жирностью, весом и т.п.;
- unit: единица, за которую указана цена (кг, л, шт, уп, бут, ящ и т.п.). Если цена за упаковку или штуку, а вес/объём указан в названии, пиши "шт" или "уп";
- price: цена за эту единицу числом, без валюты и пробелов. Если цен несколько (опт/розница, с НДС/без), бери обычную цену для заказа ресторана. Если цены нет, ставь null.

Пропускай заголовки, названия разделов, контакты, условия доставки и итоги. Ничего не выдумывай: только то, что есть в прайсе."""

ORDER_SYSTEM = """Ты помогаешь управляющему ресторана собрать заказ поставщикам.
Повара и бармены пишут в общий чат, что нужно закупить. Тебе дают каталог (все позиции из прайсов всех поставщиков) и сообщения из чата по порядку.

Найди в сообщениях все позиции, которые нужно заказать. Болтовню, вопросы и сообщения без заказа пропускай. Если позже кто-то отменил или поправил позицию («молоко не надо», «лимонов не 3, а 5 кг»), учти это и выдай итоговый вариант.

Для каждой позиции заполни:
- requested: что написали, коротко (например «молоко 10 л»);
- author: кто написал (имя из сообщения);
- quantity_text: количество, как его написали («10 л», «2 ящика»); если не указано — пустая строка;
- candidates: ВСЕ позиции каталога у ЛЮБЫХ поставщиков, которые подходят под запрос, то есть повар принял бы их как то же самое. Для каждой укажи item_id и quantity — сколько единиц ЭТОЙ позиции заказать (в её единице из каталога), чтобы покрыть запрошенное количество. Например, «молоко 10 л» и позиция «Молоко 3,2% 1 л» с единицей «шт» → 10; позиция «Молоко 3,2% 5 л» с единицей «шт» → 2. Упаковки округляй вверх до целых. Если количество не указано, бери 1 упаковку и напиши об этом в comment;
- comment: короткое пояснение, если есть сомнения или допущения, иначе пустая строка.

Не добавляй в candidates явно другой товар (другая жирность, другой сорт, если это важно, другой продукт). Если запрос общий («молоко»), включай только обычные, типичные варианты. Если в каталоге ничего подходящего нет, оставь candidates пустым — позиция попадёт в список «не найдено». Используй только item_id из каталога."""


class AIError(Exception):
    pass


class AIClient:
    """Общая логика: что спрашивать у модели. Наследники отвечают только за сам запрос."""

    name = "ИИ"
    concurrency = 4

    async def _json(self, system: str, parts: list[Part], schema: dict) -> dict:
        raise NotImplementedError

    async def extract_prices(
        self, chunks: list[PriceChunk], filename: str, progress: Progress | None = None
    ) -> list[dict]:
        sem = asyncio.Semaphore(self.concurrency)
        done = 0

        async def one(i: int, chunk: PriceChunk) -> list[dict]:
            nonlocal done
            if chunk.data is not None:
                where = f" ({chunk.label})" if chunk.label else ""
                parts: list[Part] = [chunk, f"Извлеки позиции из этого прайса{where}."]
            else:
                parts = [f"Прайс «{filename}», часть {i + 1} из {len(chunks)}:\n\n{chunk.text}"]
            async with sem:
                data = await self._json(PRICE_SYSTEM, parts, PRICE_SCHEMA)
            done += 1
            if progress and len(chunks) > 1:
                try:
                    await progress(done, len(chunks))
                except Exception:
                    log.debug("progress update failed", exc_info=True)
            return data.get("items") or []

        results = await asyncio.gather(
            *(one(i, c) for i, c in enumerate(chunks)), return_exceptions=True
        )
        for r in results:
            if isinstance(r, BaseException):
                raise r
        items = []
        for part in results:
            for it in part:
                name = " ".join(str(it.get("name", "")).split())
                if not name:
                    continue
                price = it.get("price")
                items.append(
                    {
                        "name": name,
                        "unit": str(it.get("unit") or "").strip(),
                        "price": float(price) if isinstance(price, (int, float)) else None,
                    }
                )
        return items

    async def match_order(
        self, catalog: list[CatalogItem], messages: list[PendingMessage]
    ) -> list[dict]:
        catalog_text = "\n".join(
            f"{c.id} | {c.supplier} | {c.name} | {c.unit or '—'} | "
            f"{'—' if c.price is None else format(c.price, 'g')}"
            for c in catalog
        )
        chat_text = "\n\n".join(f"[{m.author}]\n{m.text}" for m in messages)
        prompt = (
            "Каталог (id | поставщик | товар | единица | цена за единицу):\n"
            f"{catalog_text}\n\n"
            "Сообщения из чата по порядку:\n\n"
            f"{chat_text}"
        )
        data = await self._json(ORDER_SYSTEM, [prompt], ORDER_SCHEMA)
        return data.get("lines") or []


def _parse_json(text: str | None) -> dict:
    if not text:
        raise AIError("ИИ вернул пустой ответ.")
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise AIError("ИИ вернул ответ в неверном формате, попробуйте ещё раз.") from e


class Claude(AIClient):
    name = "Claude"

    def __init__(self, model: str, client=None):
        import anthropic

        self.model = model
        self.client = client or anthropic.AsyncAnthropic()

    @staticmethod
    def _content(parts: list[Part]) -> list[dict]:
        content = []
        for p in parts:
            if isinstance(p, str):
                content.append({"type": "text", "text": p})
                continue
            b64 = base64.standard_b64encode(p.data).decode()
            kind = "document" if p.mime == "application/pdf" else "image"
            content.append(
                {"type": kind, "source": {"type": "base64", "media_type": p.mime, "data": b64}}
            )
        return content

    async def _json(self, system: str, parts: list[Part], schema: dict) -> dict:
        import anthropic

        kwargs = {}
        if self.model in FALLBACK_MODELS:
            kwargs = {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}
        try:
            async with self.client.beta.messages.stream(
                model=self.model,
                max_tokens=64000,
                system=system,
                messages=[{"role": "user", "content": self._content(parts)}],
                thinking={"type": "adaptive"},
                output_config={
                    "effort": "medium",
                    "format": {"type": "json_schema", "schema": schema},
                },
                **kwargs,
            ) as stream:
                message = await stream.get_final_message()
        except anthropic.AuthenticationError as e:
            raise AIError("Неверный ключ Claude API (ANTHROPIC_API_KEY).") from e
        except anthropic.RateLimitError as e:
            raise AIError("Слишком много запросов к Claude, попробуйте через минуту.") from e
        except anthropic.APIStatusError as e:
            raise AIError(f"Ошибка Claude API ({e.status_code}): {e.message}") from e
        except anthropic.APIConnectionError as e:
            raise AIError("Нет связи с Claude API. Проверьте интернет.") from e

        if message.stop_reason == "refusal":
            raise AIError("Claude отказался обрабатывать этот запрос.")
        if message.stop_reason == "max_tokens":
            raise AIError("Ответ получился слишком длинным. Разбейте данные на части.")
        return _parse_json(next((b.text for b in message.content if b.type == "text"), None))


def _gemini_schema(schema: dict) -> dict:
    """Gemini не нужен additionalProperties — убираем, чтобы схема точно прошла."""
    schema = copy.deepcopy(schema)

    def walk(node):
        if isinstance(node, dict):
            node.pop("additionalProperties", None)
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(schema)
    return schema


class Gemini(AIClient):
    name = "Gemini"
    concurrency = 2  # бесплатный тариф ограничивает число запросов в минуту
    retry_delays = (15, 45)  # паузы перед повторами на одной модели, сек
    transient = {429, 500, 502, 503, 504}
    upload_over = 14 * 1024 * 1024  # файлы больше — через Files API

    def __init__(self, model: str, api_key: str, client=None, fallback_model: str = ""):
        if client is None:
            from google import genai

            client = genai.Client(api_key=api_key)
        self.model = model
        self.fallback_model = fallback_model
        self.client = client

    async def _contents(self, parts: list[Part], uploaded: list[str]) -> list:
        from google.genai import types

        contents = []
        for p in parts:
            if isinstance(p, str):
                contents.append(types.Part.from_text(text=p))
            elif len(p.data) > self.upload_over:
                f = await self.client.aio.files.upload(
                    file=io.BytesIO(p.data), config={"mime_type": p.mime}
                )
                uploaded.append(f.name)
                contents.append(types.Part.from_uri(file_uri=f.uri, mime_type=p.mime))
            else:
                contents.append(types.Part.from_bytes(data=p.data, mime_type=p.mime))
        return contents

    async def _json(self, system: str, parts: list[Part], schema: dict) -> dict:
        from google.genai import errors, types

        config = types.GenerateContentConfig(
            system_instruction=system,
            response_mime_type="application/json",
            response_json_schema=_gemini_schema(schema),
        )
        models = [self.model]
        if self.fallback_model and self.fallback_model != self.model:
            models.append(self.fallback_model)

        uploaded: list[str] = []
        last: errors.APIError | None = None
        try:
            contents = await self._contents(parts, uploaded)
            for model in models:
                for delay in (0, *self.retry_delays):
                    if delay:
                        await asyncio.sleep(delay)
                    try:
                        response = await self.client.aio.models.generate_content(
                            model=model, contents=contents, config=config
                        )
                        return self._result(response)
                    except errors.APIError as e:
                        if e.code not in self.transient:
                            raise self._error(e) from e
                        last = e
                        log.warning("Gemini %s: ошибка %s, повторю", model, e.code)
        except errors.APIError as e:
            raise self._error(e) from e
        except OSError as e:
            raise AIError("Нет связи с Gemini. Проверьте интернет.") from e
        finally:
            for name in uploaded:
                try:
                    await self.client.aio.files.delete(name=name)
                except Exception:
                    log.debug("не удалось удалить файл %s", name, exc_info=True)

        if last is not None and last.code == 429:
            raise AIError(
                "Исчерпан лимит бесплатного Gemini. Подождите (дневной лимит "
                "сбрасывается ночью) или переключитесь на Claude в .env."
            ) from last
        raise AIError(
            "Серверы Gemini сейчас перегружены. Попробуйте через 10–15 минут "
            "или переключитесь на Claude в .env."
        ) from last

    @staticmethod
    def _error(e) -> AIError:
        if e.code in (400, 401, 403) and "API key" in str(e):
            return AIError("Неверный ключ Gemini (GEMINI_API_KEY).")
        return AIError(f"Ошибка Gemini ({e.code}): {e.message}")

    @staticmethod
    def _result(response) -> dict:
        candidate = (response.candidates or [None])[0]
        reason = getattr(candidate, "finish_reason", None)
        reason = getattr(reason, "name", reason)
        if reason == "MAX_TOKENS":
            raise AIError("Ответ получился слишком длинным. Разбейте данные на части.")
        if reason in ("SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "RECITATION"):
            raise AIError("Gemini отказался обрабатывать этот запрос.")
        return _parse_json(response.text)
