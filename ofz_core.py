"""
Общие функции для расчёта ОФЗ: получение данных, симуляция реинвеста,
поиск количества по целевой сумме и простейший кэш.
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Dict, List, Tuple, Optional
import json
import requests

BASE_URL = "https://iss.moex.com/iss"


def fmt_rub(value: float | int) -> str:
    """Форматирует сумму в рублях с разделением тысяч пробелами."""
    s = f"{value:,.2f}"
    int_part, dot, frac = s.partition(".")
    int_part = int_part.replace(",", " ")
    return int_part + (dot + frac if dot else "")


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


def simulate_reinvest_detailed(
    bond: Dict,
    purchase_date: date,
    initial_qty: int,
    reinvest_price: float,
    allow_carry_over: bool = True,
) -> Dict:
    """
    Реинвестирует каждый купон в целые облигации по цене reinvest_price.
    Возвращает расчёт и текстовый лог.
    """
    qty = float(initial_qty)
    cash = 0.0
    initial_investment = initial_qty * bond["purchase_price_with_nkd"]
    log: List[str] = [
        f"Покупка {purchase_date}: {initial_qty} шт. по {fmt_rub(bond['purchase_price_with_nkd'])} ₽ (с НКД)."
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
            f"на {fmt_rub(nearest_value)} ₽ за бумагу."
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
                        f"  купон {fmt_rub(coupon_value)} ₽ × {qty:.0f} шт. = {fmt_rub(coupon_income)} ₽",
                        f"  перенос с прошлых купонов: {fmt_rub(carry_over)} ₽",
                        f"  всего к зачислению: {fmt_rub(total_coupon)} ₽ (не реинвестируется)",
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
                    f"  купон {fmt_rub(coupon_value)} ₽ × {prev_qty:.0f} шт. = {fmt_rub(coupon_income)} ₽",
                    f"  перенос с прошлых купонов: {fmt_rub(carry_over)} ₽",
                    f"  всего доступно для докупки: {fmt_rub(total_coupon)} ₽",
                    f"  докуплено {reinvest_qty} шт. по {fmt_rub(reinvest_price)} ₽ = {fmt_rub(reinvest_cost)} ₽",
                    f"  остаток после докупки: {fmt_rub(cash)} ₽; итоговое количество: {qty:.0f} шт.",
                ]
            )
        )

    redemption = qty * bond["face_value"]
    final_amount = redemption + final_coupon_cash + cash
    profit = final_amount - initial_investment
    years = (bond["maturity_date"] - purchase_date).days / 365.25
    annualized = (profit / initial_investment) / years * 100 if years > 0 else 0.0

    log.append(
        f"Погашение {bond['maturity_date']}: номинал {fmt_rub(redemption)} ₽ + "
        f"финальный купон/остаток {fmt_rub(final_coupon_cash + cash)} ₽ = {fmt_rub(final_amount)} ₽."
    )

    return {
        "final_quantity": int(qty),
        "final_amount": final_amount,
        "initial_investment": initial_investment,
        "profit": profit,
        "annualized_return": annualized,
        "log": log,
    }


def simulate_reinvest_simple(
    bond: Dict,
    purchase_date: date,
    initial_qty: int,
    reinvest_price: float,
    allow_carry_over: bool = True,
) -> Dict:
    """Упрощённая симуляция без лога (для бинарного поиска)."""
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

    qty = 1
    result = simulate_reinvest_simple(
        bond, purchase_date, qty, reinvest_price, allow_carry_over=allow_carry_over
    )
    if result["final_amount"] >= target_amount:
        return {"initial_qty": qty, "final_amount": result["final_amount"]}

    lower_qty = qty
    lower_val = result["final_amount"]
    upper_qty = qty * 2
    while True:
        upper_res = simulate_reinvest_simple(
            bond, purchase_date, upper_qty, reinvest_price, allow_carry_over=allow_carry_over
        )
        if upper_res["final_amount"] >= target_amount:
            break
        lower_qty, lower_val = upper_qty, upper_res["final_amount"]
        upper_qty *= 2
        if upper_qty > 10_000_000:
            raise ValueError("Слишком большая целевая сумма, подберите меньшую или снизьте дату.")

    best_qty = upper_qty
    best_val = upper_res["final_amount"]
    left, right = lower_qty + 1, upper_qty
    while left <= right:
        mid = (left + right) // 2
        mid_res = simulate_reinvest_simple(
            bond, purchase_date, mid, reinvest_price, allow_carry_over=allow_carry_over
        )
        if mid_res["final_amount"] >= target_amount:
            if mid_res["final_amount"] < best_val or mid < best_qty:
                best_qty, best_val = mid, mid_res["final_amount"]
            right = mid - 1
        else:
            left = mid + 1

    return {"initial_qty": best_qty, "final_amount": best_val}


def save_cache(data: Dict[str, Dict], path: Path) -> None:
    """Сохраняет словарь bond-ов, конвертируя даты в строки для JSON."""
    serializable = {}
    for secid, bond in data.items():
        serializable[secid] = {
            **bond,
            "maturity_date": bond["maturity_date"].isoformat()
            if isinstance(bond["maturity_date"], (date, datetime))
            else bond["maturity_date"],
            "coupons": [
                (d.isoformat() if isinstance(d, (date, datetime)) else d, v)
                for d, v in bond["coupons"]
            ],
        }

    path.write_text(
        json.dumps(
            {"updated_at": datetime.utcnow().isoformat(), "items": serializable},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def load_cache(path: Path) -> Optional[Dict]:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _fetch_ofz_list() -> List[Dict]:
    """Возвращает список российских ОФЗ (SECID начинается с SU) с данными купона."""
    url = f"{BASE_URL}/engines/stock/markets/bonds/securities.json"
    params = {
        "iss.meta": "off",
        "iss.only": "securities",
        "limit": 5000,
        "securities.columns": "SECID,SHORTNAME,FACEVALUE,COUPONTYPE,COUPONPERCENT",
    }
    resp = requests.get(url, params=params, timeout=20)
    resp.raise_for_status()
    data = resp.json()
    cols = data["securities"]["columns"]
    rows = data["securities"]["data"]

    ofz_rows = []
    for row in rows:
        item = dict(zip(cols, row))
        secid = str(item.get("SECID", "")).upper()
        coupontype = (item.get("COUPONTYPE") or "").upper()
        shortname = item.get("SHORTNAME", "")
        if not secid.startswith("SU"):
            continue
        # Отбрасываем плавающие/индексируемые купоны, если можно определить.
        if coupontype in {"FLOAT", "VARIABLE", "INFL", "AMORT"}:
            continue
        ofz_rows.append(
            {
                "SECID": secid,
                "SHORTNAME": shortname,
                "COUPONTYPE": coupontype,
                "COUPONPERCENT": item.get("COUPONPERCENT"),
                "FACEVALUE": item.get("FACEVALUE"),
            }
        )
    return ofz_rows


def download_fixed_ofz_cache(cache_path: Path) -> Dict[str, Dict]:
    """
    Скачивает данные по всем найденным ОФЗ (фикс. купон по эвристике) и сохраняет кэш.
    Возвращает словарь secid -> bond dict.
    """
    bonds: Dict[str, Dict] = {}
    for item in _fetch_ofz_list():
        secid = item["SECID"]
        try:
            bonds[secid] = fetch_bond(secid)
        except Exception:
            # Пропускаем бумаги, которые не удалось загрузить
            continue
    save_cache(bonds, cache_path)
    return bonds


def get_bond_cached(secid: str, cache_path: Path, use_cache: bool = True) -> Dict:
    """
    Возвращает bond из кэша, если доступен и разрешён use_cache; иначе из API.
    При отсутствии в кэше делает запрос и не перезаписывает кэш.
    """
    secid_norm = secid.upper().strip()
    if use_cache:
        cached = load_cache(cache_path)
        if cached and "items" in cached:
            bond = cached["items"].get(secid_norm)
            if bond:
                # Восстанавливаем даты
                bond["maturity_date"] = datetime.strptime(bond["maturity_date"], "%Y-%m-%d").date()
                bond["coupons"] = [
                    (datetime.strptime(d, "%Y-%m-%d").date(), v) if isinstance(d, str) else (d, v)
                    for d, v in bond["coupons"]
                ]
                return bond
    return fetch_bond(secid_norm)


def cache_info(cache_path: Path) -> Optional[str]:
    cached = load_cache(cache_path)
    if not cached:
        return None
    return cached.get("updated_at")

