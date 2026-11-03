"""Запросы к Claude: разбор прайсов и раскладка списков по позициям прайсов."""

import asyncio
import json
import logging

import anthropic

from .files import PriceChunk
from .models import CatalogItem, PendingMessage

log = logging.getLogger(__name__)

# Модели, которые принимают серверный fallback при отказе.
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


class Claude:
    def __init__(self, model: str, client: anthropic.AsyncAnthropic | None = None):
        self.model = model
        self.client = client or anthropic.AsyncAnthropic()

    async def _json(self, system: str, content: list[dict], schema: dict) -> dict:
        kwargs = {}
        if self.model in FALLBACK_MODELS:
            kwargs = {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}
        try:
            async with self.client.beta.messages.stream(
                model=self.model,
                max_tokens=64000,
                system=system,
                messages=[{"role": "user", "content": content}],
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
        text = next((b.text for b in message.content if b.type == "text"), None)
        if not text:
            raise AIError("Claude вернул пустой ответ.")
        return json.loads(text)

    async def extract_prices(self, chunks: list[PriceChunk], filename: str) -> list[dict]:
        sem = asyncio.Semaphore(4)

        async def one(i: int, chunk: PriceChunk) -> list[dict]:
            if chunk.block is not None:
                content = [chunk.block, {"type": "text", "text": "Извлеки позиции из этого прайса."}]
            else:
                content = [
                    {
                        "type": "text",
                        "text": f"Прайс «{filename}», часть {i + 1} из {len(chunks)}:\n\n{chunk.text}",
                    }
                ]
            async with sem:
                data = await self._json(PRICE_SYSTEM, content, PRICE_SCHEMA)
            return data["items"]

        results = await asyncio.gather(*(one(i, c) for i, c in enumerate(chunks)))
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
        content = [
            {
                "type": "text",
                "text": (
                    "Каталог (id | поставщик | товар | единица | цена за единицу):\n"
                    f"{catalog_text}\n\n"
                    "Сообщения из чата по порядку:\n\n"
                    f"{chat_text}"
                ),
            }
        ]
        data = await self._json(ORDER_SYSTEM, content, ORDER_SCHEMA)
        return data["lines"]
