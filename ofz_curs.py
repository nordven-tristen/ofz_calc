"""
Калькулятор ОФЗ с фиксированным купоном.
Берёт данные по MOEX ISS API, считает купоны и реинвестирует их
в целые облигации. Остаток купонов переносится на следующие периоды.
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
    """Получает цену, НКД и график купонов для облигации с фиксированным купоном."""
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

        # Приоритет: явное значение из API, затем value_rub, затем расчёт из процентов.
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
    """
    Реинвестирует каждый купон в целые облигации по цене reinvest_price
    (для ОФЗ обычно 1000 руб. номинал). Остаток переносится к следующему купону.
    """
    qty = float(initial_qty)
    cash = 0.0
    initial_investment = initial_qty * bond["purchase_price_with_nkd"]
    log: List[str] = [
        f"Покупка {purchase_date}: {initial_qty} шт. по {bond['purchase_price_with_nkd']:.2f} ₽ (с НКД)."
    ]
    log.append(
        "Режим переноса остатка купонов: "
        + ("включён — остаток идёт в следующий купон" if allow_carry_over else "выключен — остаток не переносится")
    )

    final_coupon_cash = 0.0
    future_coupons = [c for c in bond["coupons"] if c[0] >= purchase_date]

    if not future_coupons:
        log.append(
            f"Купоны после {purchase_date} не найдены. Ожидается только погашение "
            f"{bond['maturity_date']} без промежуточных выплат."
        )

    else:
        nearest_date, nearest_value = future_coupons[0]
        log.append(
            f"Купонов до погашения: {len(future_coupons)} шт., ближайший {nearest_date} "
            f"на {nearest_value:.2f} ₽ за бумагу."
        )

    for idx, (pay_date, coupon_value) in enumerate(future_coupons, start=1):
        coupon_income = coupon_value * qty
        carry_over = cash if allow_carry_over else 0.0
        total_coupon = coupon_income + carry_over
        is_last_coupon = pay_date >= bond["maturity_date"]

        if is_last_coupon:
            final_coupon_cash = total_coupon
            cash = 0.0
            log.append(
                "\n".join(
                    [
                        f"Купон {idx} {pay_date} (последний):",
                        f"  купон {coupon_value:.2f} ₽ × {qty:.0f} шт. = {coupon_income:,.2f} ₽",
                        f"  перенос с прошлых купонов: {carry_over:,.2f} ₽",
                        f"  всего к зачислению: {total_coupon:,.2f} ₽ (не реинвестируется)",
                    ]
                )
            )
            break

        reinvest_qty = int(total_coupon // reinvest_price)
        reinvest_cost = reinvest_qty * reinvest_price
        cash = round(total_coupon - reinvest_cost, 2) if allow_carry_over else 0.0
        prev_qty = qty
        qty += reinvest_qty

        log.append(
            "\n".join(
                [
                    f"Купон {idx} {pay_date}:",
                    f"  купон {coupon_value:.2f} ₽ × {prev_qty:.0f} шт. = {coupon_income:,.2f} ₽",
                    f"  перенос с прошлых купонов: {carry_over:,.2f} ₽",
                    f"  всего доступно для докупки: {total_coupon:,.2f} ₽",
                    f"  докуплено {reinvest_qty} шт. по {reinvest_price:,.2f} ₽ = {reinvest_cost:,.2f} ₽",
                    f"  остаток после докупки: {cash:,.2f} ₽; итоговое количество: {qty:.0f} шт.",
                ]
            )
        )

    redemption = qty * bond["face_value"]
    final_amount = redemption + final_coupon_cash + cash
    profit = final_amount - initial_investment
    years = (bond["maturity_date"] - purchase_date).days / 365.25
    annualized = (profit / initial_investment) / years * 100 if years > 0 else 0.0

    log.append(
        f"Погашение {bond['maturity_date']}: номинал {redemption:,.2f} ₽ + "
        f"финальный купон/остаток {final_coupon_cash + cash:,.2f} ₽ = {final_amount:,.2f} ₽."
    )

    return {
        "final_quantity": int(qty),
        "final_amount": final_amount,
        "initial_investment": initial_investment,
        "profit": profit,
        "annualized_return": annualized,
        "log": log,
    }


def main() -> None:
    print("ОФЗ калькулятор (фикс. купон, реинвест целыми бумагами)\n")
    secid = input("SECID облигации (например SU26235RMFS0): ").strip()
    if not secid:
        print("SECID обязателен.")
        return

    date_raw = input("Дата покупки YYYY-MM-DD (Enter=сегодня): ").strip()
    purchase_date = (
        datetime.strptime(date_raw, "%Y-%m-%d").date() if date_raw else date.today()
    )
    qty = int(input("Количество при покупке (целое): ").strip())
    carry_ans = input("Переносить остаток купонов на следующий купон? [Y/n]: ").strip().lower()
    allow_carry_over = False if carry_ans in {"n", "no", "нет", "не", "false", "0"} else True

    try:
        bond = fetch_bond(secid)
    except Exception as exc:
        print(f"Не удалось получить данные по {secid}: {exc}")
        return

    result = simulate_reinvest(
        bond=bond,
        purchase_date=purchase_date,
        initial_qty=qty,
        reinvest_price=bond["face_value"],
        allow_carry_over=allow_carry_over,
    )

    print("\nРезультаты:")
    print(f"ОФЗ: {bond['secid']}")
    print(f"Погашение: {bond['maturity_date']}")
    print(f"Цена покупки с НКД: {bond['purchase_price_with_nkd']:.2f} ₽")
    print(f"Начальная инвестиция: {result['initial_investment']:,.2f} ₽")
    print(f"Итоговое кол-во: {result['final_quantity']} шт.")
    print(f"Сумма к погашению: {result['final_amount']:,.2f} ₽")
    print(f"Прибыль: {result['profit']:,.2f} ₽")
    print(f"Среднегодовая доходность: {result['annualized_return']:.2f} %")
    print("\nШаги реинвестирования:")
    print("\n".join(result["log"]))


if __name__ == "__main__":
    main()

