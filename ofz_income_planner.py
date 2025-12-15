import json
import math
import os
from datetime import datetime, timedelta
from typing import Dict, List, Any

import requests

from ofz_core import fmt_rub

CACHE_FILE = "ofz_cache_parsed.json"
NOMINAL = 1000  # номинал ОФЗ в рублях
DATE_FORMAT = "%Y-%m-%d"


def load_cache(path: str = CACHE_FILE) -> Dict[str, Any]:
    if not os.path.exists(path):
        raise FileNotFoundError(f"Не найден кэш-файл {path}. Сначала запустите ofz_parser.py для его создания.")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def to_float(val: Any) -> float | None:
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val)
    s = str(val).strip()
    if not s:
        return None
    # убираем лишние символы
    for ch in ["%", "Р", "р", "руб", "руб."]:
        s = s.replace(ch, "")
    s = s.replace(" ", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def to_int(val: Any) -> int | None:
    f = to_float(val)
    if f is None:
        return None
    return int(round(f))


def choose_best_bond(bonds: List[Dict[str, Any]], target_income_year: float, years_needed: float) -> Dict[str, Any] | None:
    """Выбирает выпуск ОФЗ, который обеспечит нужный годовой купонный доход
    в течение указанного количества лет при минимальных затратах на покупку.
    """
    best = None

    for b in bonds:
        coupon = to_float(b.get("coupon"))  # купон в рублях на одно начисление
        freq = to_int(b.get("payments_per_year"))  # количество купонов в год
        price_pct = to_float(b.get("price"))  # цена в % от номинала
        years_to_maturity = to_float(b.get("years_to_maturity"))

        if coupon is None or freq is None or freq <= 0:
            continue
        if price_pct is None or price_pct <= 0:
            continue
        if years_to_maturity is None or years_to_maturity < years_needed:
            # выпуск не покрывает нужное число лет
            continue

        annual_coupon_per_bond = coupon * freq
        if annual_coupon_per_bond <= 0:
            continue

        # сколько облигаций нужно купить, чтобы годовой купон >= целевого дохода
        bonds_needed = math.ceil(target_income_year / annual_coupon_per_bond)
        if bonds_needed <= 0:
            bonds_needed = 1

        price_per_bond = NOMINAL * price_pct / 100.0
        total_cost = bonds_needed * price_per_bond

        candidate = dict(b)
        candidate.update(
            {
                "annual_coupon_per_bond": annual_coupon_per_bond,
                "bonds_needed": bonds_needed,
                "price_per_bond": price_per_bond,
                "total_cost": total_cost,
            }
        )

        if best is None or total_cost < best["total_cost"]:
            best = candidate

    return best


def fetch_coupon_schedule(secid: str, years_needed: float) -> List[Dict[str, Any]]:
    """Получает график будущих купонов по SECID из MOEX ISS и
    отфильтровывает выплаты только на ближайшие указанное количество лет.
    """
    url = f"https://iss.moex.com/iss/securities/{secid}/bondization.json"
    resp = requests.get(url, timeout=20)
    resp.raise_for_status()
    j = resp.json()

    coupons_block = j.get("coupons", {})
    cols = coupons_block.get("columns", [])
    data = coupons_block.get("data", [])

    # Ищем индексы колонок более гибко: value или value_rub,
    # currencyid опциональна.
    try:
        idx_date = cols.index("coupondate")
    except ValueError:
        return []

    idx_value = None
    for cand in ("value", "value_rub"):
        if cand in cols:
            idx_value = cols.index(cand)
            break
    if idx_value is None:
        return []

    idx_currency = cols.index("currencyid") if "currencyid" in cols else None

    today = datetime.today().date()
    end_date = today + timedelta(days=int(years_needed * 365.25))

    future: List[Dict[str, Any]] = []
    all_future: List[Dict[str, Any]] = []

    for row in data:
        raw_date = row[idx_date]
        value = row[idx_value]
        currency = row[idx_currency] if idx_currency is not None else "RUB"
        if not raw_date or value is None:
            continue
        try:
            d = datetime.strptime(raw_date[:10], "%Y-%m-%d").date()
        except Exception:
            continue
        if d < today:
            continue

        item = {
            "date": d.strftime(DATE_FORMAT),
            "value": float(value),
            "currency": currency,
        }

        all_future.append(item)
        if d <= end_date:
            future.append(item)

    # Если в выбранном окне ничего нет, вернём все будущие купоны
    res = future if future else all_future
    res.sort(key=lambda x: x["date"])
    return res


def main() -> None:
    cache = load_cache()
    bonds = cache.get("bonds") or []
    fetched_at = cache.get("fetched_at")

    print(f"Загружено облигаций из кэша: {len(bonds)}")
    if fetched_at:
        print(f"Дата кэша: {fetched_at}")

    # Запрос параметров у пользователя
    while True:
        try:
            target_income_year = float(input("Введите желаемый годовой купонный доход (в рублях): ").replace(",", "."))
            if target_income_year <= 0:
                raise ValueError
            break
        except ValueError:
            print("Введите положительное число.")

    while True:
        try:
            years_needed = float(input("Сколько лет вы планируете получать этот доход (например, 5): ").replace(",", "."))
            if years_needed <= 0:
                raise ValueError
            break
        except ValueError:
            print("Введите положительное число (можно с дробной частью).")

    best = choose_best_bond(bonds, target_income_year, years_needed)
    if not best:
        print("Не удалось подобрать подходящий выпуск ОФЗ по заданным параметрам.")
        return

    secid = best.get("secid")
    name = best.get("name")
    coupon_val = to_float(best.get("coupon"))

    print("\n=== Подобранная ОФЗ ===")
    print(f"Имя: {name}")
    print(f"SECID: {secid}")
    print(f"Погашение: {best.get('maturity')}")
    print(f"Лет до погашения: {best.get('years_to_maturity')}")
    print(f"Купон, руб: {fmt_rub(coupon_val) if coupon_val is not None else best.get('coupon')}")
    print(f"Частота выплат в год: {best.get('payments_per_year')}")
    print(f"Текущая цена, % от номинала: {best.get('price')}")

    print("\n=== Расчёты ===")
    print(f"Годовой купон на одну облигацию: {fmt_rub(best['annual_coupon_per_bond'])} руб")
    print(f"Необходимый годовой доход: {fmt_rub(target_income_year)} руб")
    print(f"Необходимое количество облигаций: {best['bonds_needed']}")
    print(f"Цена одной облигации (≈): {fmt_rub(best['price_per_bond'])} руб")
    print(f"Итого затраты на покупку сегодня (≈): {fmt_rub(best['total_cost'])} руб")

    if not secid:
        print("\nSECID отсутствует, не могу получить детальный график купонов с MOEX.")
        return

    print("\nЗагружаю график будущих купонных выплат с MOEX...")
    try:
        coupons = fetch_coupon_schedule(secid, years_needed)
    except Exception as e:
        print(f"Не удалось получить график купонов по SECID {secid}: {e}")
        return

    if not coupons:
        print("Будущие купонные выплаты за указанный период не найдены.")
        return

    print("\n=== Будущие купонные выплаты на ОДНУ облигацию ===")
    total_coupons_one = 0.0
    for c in coupons:
        print(f"{c['date']}: {c['value']} {c['currency']}")
        total_coupons_one += c["value"]

    print(f"Итого купонов за период на одну облигацию: {fmt_rub(total_coupons_one)} руб")
    print(f"Итого купонов за период на весь объём ({best['bonds_needed']} шт): {fmt_rub(total_coupons_one * best['bonds_needed'])} руб")


if __name__ == "__main__":
    main()
