from datetime import date
from pathlib import Path
import streamlit as st

from ofz_core import (
    cache_info,
    download_fixed_ofz_cache,
    find_min_qty_for_target,
    fmt_rub,
    get_bond_cached,
    simulate_reinvest_detailed,
)

CACHE_PATH = Path("ofz_cache.json")


def format_currency(value: float) -> str:
    return f"{fmt_rub(value)} ₽"


def format_percent(value: float) -> str:
    return f"{value:.2f} %"


def sidebar_cache_controls() -> bool:
    st.sidebar.header("Кэш котировок ОФЗ")
    use_cache = st.sidebar.checkbox("Использовать локальный кэш", True)
    info = cache_info(CACHE_PATH)
    st.sidebar.caption(f"Кэш: {info or 'нет'}")

    if st.sidebar.button("Скачать/обновить кэш"):
        with st.spinner("Скачиваем данные по ОФЗ..."):
            bonds = download_fixed_ofz_cache(CACHE_PATH)
        st.sidebar.success(f"Сохранено {len(bonds)} бумаг в {CACHE_PATH}")
    return use_cache


def section_income(use_cache: bool) -> None:
    st.subheader("1) Сколько получу при погашении (с реинвестом)")
    col1, col2, col3 = st.columns(3)
    secid = col1.text_input("SECID", placeholder="SU26235RMFS0").strip()
    purchase_date = col2.date_input("Дата покупки", value=date.today())
    qty = col3.number_input("Количество", min_value=1, value=1, step=1)
    allow_carry = st.checkbox("Переносить остаток купонов на следующий купон", True)

    if st.button("Рассчитать доходность"):
        if not secid:
            st.error("Укажите SECID.")
            return
        try:
            bond = get_bond_cached(secid, CACHE_PATH, use_cache=use_cache)
            result = simulate_reinvest_detailed(
                bond=bond,
                purchase_date=purchase_date,
                initial_qty=int(qty),
                reinvest_price=bond["face_value"],
                allow_carry_over=allow_carry,
            )
        except Exception as exc:
            st.error(f"Ошибка: {exc}")
            return

        st.success("Готово")
        col_a, col_b, col_c = st.columns(3)
        col_a.metric("Начальная инвестиция", format_currency(result["initial_investment"]))
        col_b.metric("Сумма к погашению", format_currency(result["final_amount"]))
        col_c.metric("Прибыль", format_currency(result["profit"]))
        st.metric("Среднегодовая доходность", format_percent(result["annualized_return"]))

        with st.expander("Детальный лог реинвестирования"):
            st.text("\n".join(result["log"]))


def section_target(use_cache: bool) -> None:
    st.subheader("2) Сколько купить, чтобы получить целевую сумму")
    col1, col2, col3 = st.columns(3)
    secid = col1.text_input("SECID (обратная задача)", placeholder="SU26235RMFS0").strip()
    purchase_date = col2.date_input("Дата покупки (обратная)", value=date.today(), key="date_target")
    target_amount = col3.number_input("Целевая сумма, ₽", min_value=0.0, value=1_000_000.0, step=10_000.0)
    allow_carry = st.checkbox("Переносить остаток купонов на следующий купон (обратная)", True)

    if st.button("Подобрать количество"):
        if not secid:
            st.error("Укажите SECID.")
            return
        try:
            bond = get_bond_cached(secid, CACHE_PATH, use_cache=use_cache)
            res = find_min_qty_for_target(
                bond=bond,
                purchase_date=purchase_date,
                target_amount=float(target_amount),
                allow_carry_over=allow_carry,
            )
        except Exception as exc:
            st.error(f"Ошибка: {exc}")
            return

        total_cost = res["initial_qty"] * bond["purchase_price_with_nkd"]
        st.success("Готово")
        col_a, col_b, col_c = st.columns(3)
        col_a.metric("Нужно купить, шт.", res["initial_qty"])
        col_b.metric("Затраты (с НКД)", format_currency(total_cost))
        col_c.metric("Ожидаемая сумма к погашению", format_currency(res["final_amount"]))


def main():
    st.set_page_config(page_title="OFZ Planner", layout="wide")
    st.title("ОФЗ планировщик")
    st.caption("Фиксированный купон, реинвест целыми бумагами")

    use_cache = sidebar_cache_controls()

    section_income(use_cache)
    st.divider()
    section_target(use_cache)

    st.info(
        "Запуск: `streamlit run app.py`. "
        "Кэш сохраняется в ofz_cache.json. "
        "При отключении кэша данные берутся напрямую из MOEX ISS API."
    )


if __name__ == "__main__":
    main()

