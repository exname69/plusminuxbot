"""Strict, predictable parser for short financial entries."""
from dataclasses import dataclass
from decimal import Decimal
import re


class ParseError(ValueError):
    pass


@dataclass(frozen=True)
class Entry:
    amount_minor: int  # Signed amount in hundredths (never float)
    comment: str = ""
    yesterday: bool = False


_PATTERN = re.compile(
    r"^(?P<sign>[+\-−])\s*"
    r"(?P<amount>(?:\d{1,3}(?:[ _\u00a0]\d{3})+|\d+)(?:[.,]\d{1,2})?)"
    r"(?:\s+(?P<comment>.*))?$"
)
MAX_ENTRIES = 20
MAX_COMMENT_LENGTH = 200
MAX_AMOUNT_MINOR = 999_999_999_99


def parse_line(line: str) -> Entry:
    line = line.strip()
    yesterday = False
    if line.casefold().startswith("вчера "):
        yesterday = True
        line = line[6:].strip()

    match = _PATTERN.fullmatch(line)
    if not match:
        raise ParseError("Формат: +100, -50 аренда или вчера +240 ногти")

    raw = match.group("amount").replace(" ", "").replace("_", "").replace("\u00a0", "").replace(",", ".")
    amount = int(Decimal(raw) * 100)
    if not amount or amount > MAX_AMOUNT_MINOR:
        raise ParseError("Сумма должна быть больше нуля и не слишком большой")

    if match.group("sign") in ("-", "−"):
        amount = -amount

    comment = (match.group("comment") or "").strip()
    if len(comment) > MAX_COMMENT_LENGTH:
        raise ParseError(f"Комментарий длиннее {MAX_COMMENT_LENGTH} символов")
    return Entry(amount_minor=amount, comment=comment, yesterday=yesterday)


def parse_message(text: str) -> list[Entry]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        raise ParseError("Отправь +100 или -50 аренда")
    if len(lines) > MAX_ENTRIES:
        raise ParseError(f"За раз можно добавить максимум {MAX_ENTRIES} операций")

    result: list[Entry] = []
    for index, line in enumerate(lines, start=1):
        try:
            result.append(parse_line(line))
        except ParseError as exc:
            if len(lines) == 1:
                raise
            raise ParseError(f"Ошибка в строке {index} ({line[:30]}): {exc}") from exc
    return result
