# -*- coding: utf-8 -*-
"""
Скрипт: проставить по списку продаж (Excel) в БД CRM is_sale=1 и manager_id.
Запуск: python set_sales_from_excel.py
Перед запуском закрой бота/приложения, работающие с БД. После — залей обновлённую БД на Amvera.
"""
import os
import sqlite3
import pandas as pd

EXCEL_PATH = r"C:\Users\user\Desktop\Продажи бизнес.xlsx"
DB_PATH = r"C:\Users\user\Desktop\БД\crm_base.db"

# Явное соответствие: имя из Excel -> user_id в БД (бизнес-менеджеры)
MANAGER_MAP = {
    "Начиб": 5498363451,       # Наҷиб Назаров (разное написание)
    "Сиявуш": 8253026519,      # Сиёвуш Назаров
    "Сиёвуш": 8253026519,
    "Мубин": 6441200447,       # Азизов Мубин Муҳсинович
    "Фарух": 7254250538,       # Ахмедов Фаррух
    "Ахмад": 7433892956,      # Саидзода Ахмад
    "Манижа": 8198640059,     # Манижа Холикова (бизнес)
    "Мадина": 8606270335,     # Мадина Хусравова (бизнес)
}

def normalize_phone(v):
    if pd.isna(v):
        return None
    s = str(int(float(v))).strip()
    if not s:
        return None
    if s.startswith("992") and len(s) >= 12:
        return s
    if s.startswith("9") and len(s) == 9:
        return "992" + s
    if len(s) == 10 and s.startswith("7"):
        return "992" + s[1:]
    if len(s) == 11 and s.startswith("79"):
        return "992" + s[1:]
    return s

def manager_name_to_id(name):
    if pd.isna(name) or not str(name).strip():
        return None
    first = str(name).split(",")[0].strip()
    return MANAGER_MAP.get(first) or MANAGER_MAP.get(first.capitalize())

def phone_variants(phone_norm):
    if not phone_norm:
        return []
    variants = [phone_norm]
    if phone_norm.startswith("992") and len(phone_norm) == 12:
        variants.append(phone_norm[3:])
    if len(phone_norm) == 9 and phone_norm.startswith("9"):
        variants.append("992" + phone_norm)
    if phone_norm.startswith("992") and len(phone_norm) == 12:
        variants.append("7" + phone_norm[3:])
    return variants

def main():
    if not os.path.exists(EXCEL_PATH):
        print("Не найден файл:", EXCEL_PATH)
        return
    if not os.path.exists(DB_PATH):
        print("Не найдена БД:", DB_PATH)
        return

    df = pd.read_excel(EXCEL_PATH)
    df = df[df["Номер"].notna()].copy()
    df["phone_norm"] = df["Номер"].apply(normalize_phone)
    df["manager_id"] = df["Менеджер"].apply(manager_name_to_id)

    conn = sqlite3.connect(DB_PATH)

    cur = conn.execute("PRAGMA table_info(leads)")
    cols = [r[1] for r in cur.fetchall()]
    if "is_sale" not in cols:
        conn.execute("ALTER TABLE leads ADD COLUMN is_sale INTEGER DEFAULT 0")
        conn.commit()
        print("В таблицу leads добавлена колонка is_sale.")

    updated = 0
    not_found_list = []  # (phone_norm, manager_id) для тех, кого нет в leads
    no_manager = []

    for _, row in df.iterrows():
        ph = row["phone_norm"]
        mid = row["manager_id"]
        if not ph:
            continue
        if mid is None:
            no_manager.append((ph, row.get("Менеджер", "")))
            continue
        variants = phone_variants(ph)
        found_phone = None
        for v in variants:
            r = conn.execute("SELECT phone FROM leads WHERE phone = ?", (v,)).fetchone()
            if r:
                found_phone = r[0]
                break
        if not found_phone:
            not_found_list.append((ph, mid))
            continue
        conn.execute(
            "UPDATE leads SET is_sale = 1, manager_id = ?, status = 'closed', is_answered = 1 WHERE phone = ?",
            (mid, found_phone),
        )
        updated += 1

    # Доп. продажи: лиды из Excel, которых нет в CRM — считаем шт по менеджерам для KPI
    conn.execute(
        "CREATE TABLE IF NOT EXISTS manager_extra_sales (manager_id INTEGER PRIMARY KEY, extra_sales INTEGER DEFAULT 0)"
    )
    from collections import Counter
    extra_per_manager = Counter(mid for _, mid in not_found_list)
    conn.execute("DELETE FROM manager_extra_sales")
    for mid, cnt in extra_per_manager.items():
        conn.execute(
            "INSERT OR REPLACE INTO manager_extra_sales (manager_id, extra_sales) VALUES (?, ?)",
            (mid, cnt),
        )

    conn.commit()
    conn.close()

    not_found_phones = [p for p, _ in not_found_list]

    print(f"Обновлено лидов: {updated}")
    if not_found_phones:
        print(f"Номеров из Excel нет в БД (всего {len(not_found_phones)}):", not_found_phones[:20])
        if len(not_found_phones) > 20:
            print("  ... и ещё", len(not_found_phones) - 20)
        print("Их учтено как доп. продажи в KPI (таблица manager_extra_sales).")
    if no_manager:
        print("Строк без сопоставленного менеджера:", len(no_manager), "— примеры:", no_manager[:5])

if __name__ == "__main__":
    main()
