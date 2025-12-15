"""
Калькулятор ОФЗ с фиксированным купоном.
Берёт данные по MOEX ISS API, считает купоны и реинвестирует их
в целые облигации. Остаток купонов переносится на следующие периоды.
"""

from datetime import date, datetime
from ofz_core import fetch_bond, simulate_reinvest_detailed, fmt_rub


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

    result = simulate_reinvest_detailed(
        bond=bond,
        purchase_date=purchase_date,
        initial_qty=qty,
        reinvest_price=bond["face_value"],
        allow_carry_over=allow_carry_over,
    )

    print("\nРезультаты:")
    print(f"ОФЗ: {bond['secid']}")
    print(f"Погашение: {bond['maturity_date']}")
    print(f"Цена покупки с НКД: {fmt_rub(bond['purchase_price_with_nkd'])} ₽")
    print(f"Начальная инвестиция: {fmt_rub(result['initial_investment'])} ₽")
    print(f"Итоговое кол-во: {result['final_quantity']} шт.")
    print(f"Сумма к погашению: {fmt_rub(result['final_amount'])} ₽")
    print(f"Прибыль: {fmt_rub(result['profit'])} ₽")
    print(f"Среднегодовая доходность: {result['annualized_return']:.2f} %")
    print("\nШаги реинвестирования:")
    print("\n".join(result["log"]))


if __name__ == "__main__":
    main()

