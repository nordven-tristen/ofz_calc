"""
Обратная задача: по целевой сумме к погашению определить,
сколько ОФЗ (фиксированный купон) нужно купить в указанную дату.
Берём данные из MOEX ISS API. Купоны реинвестируются в целые облигации,
остаток переносится на следующий купон. Нужен результат >= целевой
с минимальным превышением.
"""

from datetime import date, datetime
from typing import Dict, List, Tuple
import requests

BASE_URL = "https://iss.moex.com/iss"


def _parse_float(value) -> float:
    if value is None or value == "":
        return 0.0
    if isinstance(value, str):
        value = value.replace(",", ".")
    try:
        return float(value)
    except ValueError:
        return 0.0


def fetch_bond(secid: str) -> Dict:
    """Получает цену, НКД и график купонов."""
    secid = secid.upper().strip()

    url = f"{BASE_URL}/engines/stock/markets/bonds/securities/{secid}.json"
    params = {
        "iss.meta": "off",
        "securities.columns": "SECID,FACEVALUE,MATDATE",
        "marketdata.columns": "SECID,BOARDID,LAST,PREVPRICE,ACCRUEDINT",
    }
    resp = requests.get(url, params=params, timeout=15)
    resp.raise_for_status()
    data = resp.json()

    sec = dict(zip(data["securities"]["columns"], data["securities"]["data"][0]))
    face_value = float(sec.get("FACEVALUE", 1000))
    maturity_date = datetime.strptime(sec["MATDATE"], "%Y-%m-%d").date()

    market_rows = data["marketdata"]["data"]
    market_cols = data["marketdata"]["columns"]
    market = next(
        (
            dict(zip(market_cols, row))
            for row in market_rows
            if dict(zip(market_cols, row)).get("BOARDID") in ("TQOB", "TQOD")
        ),
        dict(zip(market_cols, market_rows[0])) if market_rows else {},
    )

    clean_price_pct = _parse_float(market.get("LAST")) or _parse_float(
        market.get("PREVPRICE")
    )
    if clean_price_pct == 0:
        raise ValueError("Не удалось получить цену на TQOB/TQOD")

    accrued_int = _parse_float(market.get("ACCRUEDINT"))
    clean_price_rub = clean_price_pct / 100 * face_value
    purchase_price_with_nkd = clean_price_rub + accrued_int

    coupon_url = f"{BASE_URL}/securities/{secid}/bondization.json"
    resp_coupon = requests.get(
        coupon_url, params={"iss.meta": "off", "limit": 5000, "start": 0}, timeout=15
    )
    resp_coupon.raise_for_status()
    coup_json = resp_coupon.json()
    coup_cols = coup_json["coupons"]["columns"]
    coup_rows = coup_json["coupons"]["data"]

    coupons: List[Tuple[date, float]] = []
    for row in coup_rows:
        coupon = dict(zip(coup_cols, row))
        pay_date = datetime.strptime(coupon["coupondate"], "%Y-%m-%d").date()
        start_raw = coupon.get("startdate")
        start_date = (
            datetime.strptime(start_raw, "%Y-%m-%d").date() if start_raw else None
        )

        value_nominal = _parse_float(coupon.get("value"))
        value_rub = _parse_float(coupon.get("value_rub"))
        value_pct = _parse_float(coupon.get("valueprc"))

        period_days = (pay_date - start_date).days if start_date else 0
        value_from_pct = (
            value_pct / 100 * face_value * period_days / 365 if value_pct and period_days > 0 else 0.0
        )

        value = 0.0
        if value_nominal > 0:
            value = value_nominal
        elif value_rub > 0:
            value = value_rub
        elif value_from_pct > 0:
            value = value_from_pct

        if value > 0:
            coupons.append((pay_date, round(value, 4)))
    coupons.sort(key=lambda x: x[0])

    return {
        "secid": secid,
        "face_value": face_value,
        "maturity_date": maturity_date,
        "clean_price_rub": clean_price_rub,
        "accrued_int": accrued_int,
        "purchase_price_with_nkd": purchase_price_with_nkd,
        "coupons": coupons,
    }


def simulate_reinvest(
    bond: Dict,
    purchase_date: date,
    initial_qty: int,
    reinvest_price: float,
    allow_carry_over: bool = True,
) -> Dict:
    qty = float(initial_qty)
    cash = 0.0
    final_coupon_cash = 0.0

    for pay_date, coupon_value in (c for c in bond["coupons"] if c[0] >= purchase_date):
        total_coupon = coupon_value * qty + (cash if allow_carry_over else 0.0)
        is_last_coupon = pay_date >= bond["maturity_date"]

        if is_last_coupon:
            final_coupon_cash = total_coupon
            cash = 0.0
            break

        reinvest_qty = int(total_coupon // reinvest_price)
        cash = round(total_coupon - reinvest_qty * reinvest_price, 2) if allow_carry_over else 0.0
        qty += reinvest_qty

    redemption = qty * bond["face_value"]
    final_amount = redemption + final_coupon_cash + cash
    return {"final_amount": final_amount, "final_qty": int(qty)}


def find_min_qty_for_target(
    bond: Dict, purchase_date: date, target_amount: float, allow_carry_over: bool = True
) -> Dict:
    """Подбирает минимальное целое количество, дающее сумму >= target_amount."""
    reinvest_price = bond["face_value"]

    # 1) Начинаем с 1 облигации
    qty = 1
    result = simulate_reinvest(
        bond, purchase_date, qty, reinvest_price, allow_carry_over=allow_carry_over
    )
    if result["final_amount"] >= target_amount:
        return {"initial_qty": qty, "final_amount": result["final_amount"]}

    # 2) Экспоненциально ищем верхнюю границу, где сумма >= target
    lower_qty = qty
    lower_val = result["final_amount"]
    upper_qty = qty * 2
    while True:
        upper_res = simulate_reinvest(
            bond, purchase_date, upper_qty, reinvest_price, allow_carry_over=allow_carry_over
        )
        if upper_res["final_amount"] >= target_amount:
            break
        lower_qty, lower_val = upper_qty, upper_res["final_amount"]
        upper_qty *= 2
        if upper_qty > 10_000_000:
            raise ValueError("Слишком большая целевая сумма, подберите меньшую или снизьте дату.")

    # 3) Бинарный поиск между lower_qty и upper_qty
    best_qty = upper_qty
    best_val = upper_res["final_amount"]
    left, right = lower_qty + 1, upper_qty
    while left <= right:
        mid = (left + right) // 2
        mid_res = simulate_reinvest(
            bond, purchase_date, mid, reinvest_price, allow_carry_over=allow_carry_over
        )
        if mid_res["final_amount"] >= target_amount:
            if mid_res["final_amount"] < best_val or mid < best_qty:
                best_qty, best_val = mid, mid_res["final_amount"]
            right = mid - 1
        else:
            left = mid + 1

    return {"initial_qty": best_qty, "final_amount": best_val}


def main() -> None:
    print("ОФЗ калькулятор (обратная задача: нужное количество по цели)\n")
    secid = input("SECID облигации (например SU26235RMFS0): ").strip()
    if not secid:
        print("SECID обязателен.")
        return

    date_raw = input("Дата покупки YYYY-MM-DD (Enter=сегодня): ").strip()
    purchase_date = (
        datetime.strptime(date_raw, "%Y-%m-%d").date() if date_raw else date.today()
    )

    target_raw = input("Желаемая сумма к погашению, ₽: ").strip().replace(",", ".")
    try:
        target_amount = float(target_raw)
    except ValueError:
        print("Неверный ввод суммы.")
        return

    try:
        bond = fetch_bond(secid)
    except Exception as exc:
        print(f"Не удалось получить данные по {secid}: {exc}")
        return

    try:
        res = find_min_qty_for_target(bond, purchase_date, target_amount)
    except Exception as exc:
        print(f"Ошибка расчёта: {exc}")
        return

    total_cost = res["initial_qty"] * bond["purchase_price_with_nkd"]

    print("\nРезультаты:")
    print(f"ОФЗ: {bond['secid']}")
    print(f"Погашение: {bond['maturity_date']}")
    print(f"Нужно купить: {res['initial_qty']} шт.")
    print(f"Затраты на покупку (с НКД): {total_cost:,.2f} ₽")
    print(f"Ожидаемая сумма к погашению: {res['final_amount']:,.2f} ₽")


if __name__ == "__main__":
    main()

