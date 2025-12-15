"""
Обратная задача: по целевой сумме к погашению определить,
сколько ОФЗ (фиксированный купон) нужно купить в указанную дату.
Берём данные из MOEX ISS API. Купоны реинвестируются в целые облигации,
остаток переносится на следующий купон. Нужен результат >= целевой
с минимальным превышением.
"""

from datetime import date, datetime
from ofz_core import fetch_bond, find_min_qty_for_target, fmt_rub


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
    print(f"Затраты на покупку (с НКД): {fmt_rub(total_cost)} ₽")
    print(f"Ожидаемая сумма к погашению: {fmt_rub(res['final_amount'])} ₽")


if __name__ == "__main__":
    main()

