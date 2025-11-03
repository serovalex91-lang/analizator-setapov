#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Фильтрация сообщений для setup_messengers.csv

Берёт входной CSV messages.csv и отбирает только те строки, где:
 - В тексте есть метка автора: "Author: Setup Screener" (регистронезависимо)
 - И (chat_id == -1002423680272 ИЛИ chat_id == 616892418 ИЛИ chat_name
   содержит "Tradium Setups [TERMINAL]"/"Trade Setup Screener")

Выход: setup_messengers.csv с тем же хедером.
Параметры можно переопределить через аргументы командной строки.
"""

from __future__ import annotations

import argparse
import csv
import re
from pathlib import Path


def rebuild_setup_csv(
    input_path: Path,
    output_path: Path,
    screener_chat_id: int = -1002423680272,
    svetlana_chat_id: int = 616892418,
    self_chat_id: int | None = None,
) -> int:
    """Пересобирает выходной CSV на основе фильтров. Возвращает число записанных строк."""

    thread_name_patterns = [
        re.compile(r"tradium\s*setups\s*\[terminal\]", re.IGNORECASE),
        re.compile(r"trade\s*setup\s*screener", re.IGNORECASE),
    ]
    author_pattern = re.compile(r"author\s*:\s*setup\s*screener", re.IGNORECASE)

    if not input_path.exists():
        raise FileNotFoundError(f"Input not found: {input_path}")

    written = 0
    with input_path.open("r", encoding="utf-8", newline="") as fin, output_path.open(
        "w", encoding="utf-8", newline=""
    ) as fout:
        reader = csv.reader(fin)
        writer = csv.writer(fout)

        header = next(reader, None)
        if header is None:
            writer.writerow(["timestamp_utc", "chat_id", "chat_name", "message_text"])  # safety header
            return 0

        writer.writerow(header)
        try:
            idx_chat_id = header.index("chat_id")
            idx_chat_name = header.index("chat_name")
            idx_text = header.index("message_text")
        except ValueError:
            # fallback positions
            idx_chat_id, idx_chat_name, idx_text = 1, 2, 3

        for row in reader:
            try:
                cid = int(row[idx_chat_id]) if row[idx_chat_id] not in (None, "") else 0
            except Exception:
                cid = 0
            cname = row[idx_chat_name] or ""
            text = row[idx_text] or ""

            if not author_pattern.search(text):
                continue

            ok_chat = (
                cid == screener_chat_id
                or cid == svetlana_chat_id
                or any(p.search(cname) for p in thread_name_patterns)
                or (self_chat_id is not None and cid == self_chat_id)
            )
            if not ok_chat:
                continue

            writer.writerow(row)
            written += 1

    return written


def main() -> None:
    base = Path(__file__).resolve().parent
    default_in = base / "messages.csv"
    default_out = base / "setup_messengers.csv"

    parser = argparse.ArgumentParser(description="Фильтрация сообщений Setup Screener")
    parser.add_argument("--in", dest="input", default=str(default_in), help="Входной CSV (messages.csv)")
    parser.add_argument("--out", dest="output", default=str(default_out), help="Выходной CSV (setup_messengers.csv)")
    parser.add_argument("--screener-chat-id", type=int, default=-1002423680272, help="chat_id треда Screener")
    parser.add_argument("--svetlana-chat-id", type=int, default=616892418, help="chat_id чата Светланы")
    parser.add_argument("--self-chat-id", type=int, default=None, help="Ваш chat_id (Saved Messages)")
    args = parser.parse_args()

    written = rebuild_setup_csv(
        input_path=Path(args.input),
        output_path=Path(args.output),
        screener_chat_id=args.screener_chat_id,
        svetlana_chat_id=args.svetlana_chat_id,
        self_chat_id=args.self_chat_id,
    )
    print(f"Rebuilt {args.output} with {written} messages")


if __name__ == "__main__":
    main()


