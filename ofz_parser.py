import json
import os
import sys
from datetime import datetime
from typing import List, Dict

import requests
from bs4 import BeautifulSoup


URL = "https://smart-lab.ru/q/ofz/?ofz_type=default&ysclid=mj6zviyhbz898653399"
CACHE_FILE = "ofz_cache.json"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def fetch_moex_secid_map() -> Dict[str, str]:
    """
    Получает соответствие SHORTNAME -> SECID из ISS MOEX для гос.облигаций.

    Используем два основных борда ОФЗ: TQOB и TQCB.
    """
    base = "https://iss.moex.com/iss/engines/stock/markets/bonds/boards/{board}/securities.json"
    secid_map: Dict[str, str] = {}

    for board in ("TQOB", "TQCB"):
        url = base.format(board=board)
        try:
            resp = requests.get(url, timeout=20)
            resp.raise_for_status()
        except Exception:
            continue

        j = resp.json()
        sec_block = j.get("securities", {})
        cols = sec_block.get("columns", [])
        data = sec_block.get("data", [])
        try:
            idx_shortname = cols.index("SHORTNAME")
            idx_secid = cols.index("SECID")
        except ValueError:
            continue

        for row in data:
            shortname = row[idx_shortname]
            secid = row[idx_secid]
            if not shortname or not secid:
                continue
            # если один и тот же SHORTNAME встречается на двух бордах, первый записанный оставляем
            secid_map.setdefault(shortname, secid)

    return secid_map


def fetch_ofz_data() -> Dict:
    """Загружает страницу и парсит таблицу ОФЗ.

    Возвращает словарь вида:
    {
        "fetched_at": "...",
        "source_url": "...",
        "bonds": [ { ... }, ... ]
    }
    """
    resp = requests.get(URL, timeout=20)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")

    # На странице основная таблица ОФЗ - ищем её по заголовку или структуре.
    # Берём первую таблицу с заголовком, содержащим "Котировки ОФЗ" либо просто первую большую таблицу.
    table = None

    # Попробуем сначала найти по тексту заголовка
    for h in soup.find_all(["h1", "h2", "h3"]):
        if "ОФЗ" in h.get_text():
            nxt = h.find_next("table")
            if nxt:
                table = nxt
                break

    # fallback: просто первая таблица с тегом <table>
    if table is None:
        table = soup.find("table")

    if table is None:
        raise RuntimeError("Не удалось найти таблицу ОФЗ на странице")

    # Заголовки столбцов
    header_cells = table.find("thead").find_all("th") if table.find("thead") else table.find("tr").find_all("th")
    headers = [" ".join(h.get_text(strip=True).split()) for h in header_cells]

    # Индексы интересующих столбцов (по тексту заголовков, могут немного меняться)
    def find_idx(substr: str) -> int | None:
        """
        Ищет индекс столбца по подстроке в заголовке.

        У некоторых заголовков на smart-lab могут быть переносы строк (<br>),
        поэтому дополнительно сравниваем варианты без пробелов.
        """
        substr_lower = substr.lower()
        substr_compact = substr_lower.replace(" ", "")
        for i, name in enumerate(headers):
            name_lower = name.lower()
            name_compact = name_lower.replace(" ", "")
            if substr_lower in name_lower or substr_compact in name_compact:
                return i
        return None

    idx_name = find_idx("Имя")
    idx_maturity = find_idx("Погашение")
    idx_years = find_idx("Лет до погаш")
    idx_yield = find_idx("Доходн")
    idx_price = find_idx("Цена")
    idx_coupon = find_idx("Купон")
    idx_freq = find_idx("Частота")

    # Получим карту SHORTNAME -> SECID с Московской биржи
    try:
        secid_map = fetch_moex_secid_map()
    except Exception:
        secid_map = {}

    bonds: List[Dict] = []

    body = table.find("tbody") or table
    for tr in body.find_all("tr"):
        tds = tr.find_all("td")
        if not tds:
            continue

        def get_cell(idx: int | None) -> str | None:
            if idx is None or idx >= len(tds):
                return None
            # текст может быть внутри <a>
            return " ".join(tds[idx].get_text(strip=True).split()) or None

        name = get_cell(idx_name)
        bond = {
            "name": get_cell(idx_name),
            "maturity": get_cell(idx_maturity),
            "years_to_maturity": get_cell(idx_years),
            "yield_to_maturity": get_cell(idx_yield),
            "price": get_cell(idx_price),
            "coupon": get_cell(idx_coupon),
            "payments_per_year": get_cell(idx_freq),
            "secid": secid_map.get(name) if name else None,
        }

        # пропускаем пустые строки
        if not bond["name"]:
            continue

        bonds.append(bond)

    fetched_at = datetime.now().strftime(DATE_FORMAT)
    return {
        "fetched_at": fetched_at,
        "source_url": URL,
        "bonds": bonds,
    }


def load_cache(path: str = CACHE_FILE) -> Dict | None:
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def save_cache(data: Dict, path: str = CACHE_FILE) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def print_table(bonds: List[Dict], limit: int | None = None) -> None:
    """Выводит данные по ОФЗ в виде простой таблицы в терминал."""
    if not bonds:
        print("Нет данных для отображения")
        return

    headers = ["Имя", "SECID", "Погашение", "Лет до погаш.", "Доходн, %", "Цена"]
    keys = ["name", "secid", "maturity", "years_to_maturity", "yield_to_maturity", "price"]

    rows = []
    for b in bonds[: limit or len(bonds)]:
        rows.append([b.get(k) or "" for k in keys])

    # Вычисляем ширину столбцов
    col_widths = []
    for col_idx in range(len(headers)):
        max_len = len(headers[col_idx])
        for r in rows:
            max_len = max(max_len, len(str(r[col_idx])))
        col_widths.append(max_len)

    def fmt_row(cells):
        return " | ".join(str(c).ljust(col_widths[i]) for i, c in enumerate(cells))

    # Печатаем таблицу
    print(fmt_row(headers))
    print("-+-".join("-" * w for w in col_widths))
    for r in rows:
        print(fmt_row(r))


def ask_yes_no(prompt: str, default: bool | None = None) -> bool:
    """Простой вопрос да/нет.

    default: True/False/None (если None, ответ обязателен)
    """
    if default is True:
        suffix = " [Y/n]: "
    elif default is False:
        suffix = " [y/N]: "
    else:
        suffix = " [y/n]: "

    while True:
        ans = input(prompt + suffix).strip().lower()
        if not ans and default is not None:
            return default
        if ans in ("y", "д", "да", "yes"):
            return True
        if ans in ("n", "н", "нет", "no"):
            return False
        print("Пожалуйста, ответьте 'y' или 'n'.")


def main() -> None:
    cache = load_cache()
    if cache and isinstance(cache, dict):
        fetched_at = cache.get("fetched_at")
        print(f"Найден локальный кэш: {CACHE_FILE}")
        if fetched_at:
            print(f"Дата последней загрузки: {fetched_at}")
    else:
        print("Локальный кэш не найден.")

    if cache is None:
        # кэша нет — нужно загрузить
        print("Выполняю первичную загрузку данных с сайта Smart-Lab...")
        try:
            cache = fetch_ofz_data()
            save_cache(cache)
            print("Кэш успешно сохранён.")
        except Exception as e:
            print(f"Ошибка при загрузке данных: {e}", file=sys.stderr)
            sys.exit(1)
    else:
        # спросим пользователя, обновлять ли кэш
        if ask_yes_no("Обновить кэш данных ОФЗ с сайта Smart-Lab?", default=False):
            print("Обновляю кэш...")
            try:
                cache = fetch_ofz_data()
                save_cache(cache)
                print("Кэш успешно обновлён.")
            except Exception as e:
                print(f"Ошибка при обновлении данных: {e}", file=sys.stderr)
        else:
            print("Используем существующий кэш.")

    bonds = (cache or {}).get("bonds") or []
    print(f"Всего облигаций в наборе данных: {len(bonds)}")

    if ask_yes_no("Вывести данные по ОФЗ в терминале в табличном виде?", default=True):
        # дополнительно можно спросить про лимит строк
        try:
            limit_str = input("Введите максимальное количество строк для вывода (Enter — без ограничения): ").strip()
            limit = int(limit_str) if limit_str else None
        except ValueError:
            print("Некорректное число, покажу все строки.")
            limit = None
        print_table(bonds, limit=limit)


if __name__ == "__main__":
    main()
