#!/usr/bin/env python3
import os
from pathlib import Path
from collections import defaultdict

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "backtest_levels" / "output"


def collect_images(base: Path):
    by_symbol: dict[str, dict[str, list[Path]]] = defaultdict(lambda: defaultdict(list))
    if not base.exists():
        return {}
    for sym_dir in sorted(base.iterdir()):
        if not sym_dir.is_dir():
            continue
        for month_dir in sorted(sym_dir.iterdir()):
            if not month_dir.is_dir():
                continue
            month = month_dir.name
            imgs = [p for p in sorted(month_dir.glob("*.png"))]
            if imgs:
                by_symbol[sym_dir.name][month] = imgs
    return by_symbol


def write_index(base: Path, tree: dict[str, dict[str, list[Path]]]):
    index_path = base / "index.html"
    html = [
        "<!DOCTYPE html>",
        "<html lang='en'>",
        "<head>",
        "<meta charset='utf-8'/>",
        "<meta name='viewport' content='width=device-width, initial-scale=1'/>",
        "<title>Backtest Gallery</title>",
        "<style>body{font-family:system-ui,Segoe UI,Arial,sans-serif;background:#0f172a;color:#e5e7eb;margin:0;padding:24px;}\n",
        "a{color:#93c5fd;text-decoration:none} a:hover{text-decoration:underline} \n",
        ".sym{margin-bottom:28px;padding-bottom:16px;border-bottom:1px solid #1f2937} \n",
        ".grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:12px;margin-top:10px} \n",
        ".card{background:#111827;border:1px solid #1f2937;border-radius:8px;padding:8px} \n",
        ".thumb{width:100%;height:170px;object-fit:cover;border-radius:6px;border:1px solid #374151;background:#0b1220} \n",
        ".title{font-weight:600;margin:0 0 6px 0} .desc{font-size:12px;color:#9ca3af} \n",
        "</style>",
        "</head>",
        "<body>",
        "<h1>Backtest Gallery</h1>",
        "<p>Клик по карточке открывает картинку в полном размере. Сгруппировано по тикеру и месяцу.</p>",
    ]

    for sym in sorted(tree.keys()):
        html.append(f"<div class='sym'><h2>{sym}</h2>")
        html.append("<div class='grid'>")
        months = tree[sym]
        for month in sorted(months.keys()):
            imgs = months[month]
            # берём первую как превью
            preview = imgs[0].relative_to(base)
            count = len(imgs)
            html.append(
                "<a class='card' href='" + str(preview) + "' target='_blank'>"
                + f"<p class='title'>{month} ({count})</p>"
                + f"<img class='thumb' src='{preview}' alt='{sym} {month}'/>"
                + "<p class='desc'>Первая картинка как превью • остальные в папке</p>"
                + "</a>"
            )
        html.append("</div></div>")

    html.append("</body></html>")
    index_path.write_text("\n".join(html), encoding="utf-8")
    return index_path


def main():
    tree = collect_images(OUT)
    index = write_index(OUT, tree)
    print("Gallery index:", index)


if __name__ == "__main__":
    main()


