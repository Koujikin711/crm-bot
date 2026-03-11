# -*- coding: utf-8 -*-
"""Проверка: список продаж Excel + БД CRM — совместимость перед скриптом проставления is_sale."""
import os
import sqlite3
import pandas as pd

EXCEL_PATH = r"C:\Users\user\Desktop\Продажи бизнес.xlsx"
DB_DIR = r"C:\Users\user\Desktop\БД"
DB_PATH = os.path.join(DB_DIR, "crm_base.db")

def main():
    print("=== 1. Excel === ")
    if not os.path.exists(EXCEL_PATH):
        print("Файл не найден:", EXCEL_PATH)
        return
    df = pd.read_excel(EXCEL_PATH)
    print("Колонки:", list(df.columns))
    print("Строк:", len(df))
    print("Менеджеры в Excel:", df["Менеджер"].dropna().unique().tolist())
    # Номера без NaN
    with_num = df[df["Номер"].notna()].copy()
    with_num["phone_raw"] = with_num["Номер"].astype(int).astype(str)
    print("Примеры номеров (как в Excel int):", with_num["phone_raw"].head(5).tolist())

    print("\n=== 2. БД === ")
    if not os.path.exists(DB_PATH):
        print("БД не найдена:", DB_PATH)
        return
    conn = sqlite3.connect(DB_PATH)
    users = conn.execute(
        "SELECT user_id, fio, role, sphere FROM users WHERE role IN ('manager','admin') ORDER BY fio"
    ).fetchall()
    print("Пользователи (user_id, fio, role, sphere):")
    for r in users:
        print(" ", r)
    leads_phones = conn.execute("SELECT phone FROM leads LIMIT 10").fetchall()
    print("Примеры phone в leads:", [r[0] for r in leads_phones])
    # Есть ли колонка is_sale
    cur = conn.execute("PRAGMA table_info(leads)")
    cols = [r[1] for r in cur.fetchall()]
    print("Колонки leads:", cols)
    conn.close()

    print("\n=== 3. Сопоставление менеджеров === ")
    # Excel: Начиб, Мубин, Ахмад, Манижа, Мадина, Сиявуш
    # БД fio может быть "Наҷиб Назаров" и т.д. — ищем по вхождению имени
    excel_managers = set()
    for m in df["Менеджер"].dropna():
        for part in str(m).split(","):
            excel_managers.add(part.strip())
    print("Уникальные имена менеджеров из Excel (в т.ч. из пар):", sorted(excel_managers))
    conn = sqlite3.connect(DB_PATH)
    for name in sorted(excel_managers):
        row = conn.execute(
            "SELECT user_id, fio FROM users WHERE role IN ('manager','admin') AND (fio LIKE ? OR fio LIKE ?)",
            (f"%{name}%", f"%{name.capitalize()}%"),
        ).fetchone()
        print(f"  '{name}' -> БД: {row}")
    conn.close()

    print("\n=== 4. Сколько номеров из Excel есть в leads === ")
    conn = sqlite3.connect(DB_PATH)
    def normalize_phone(v):
        if pd.isna(v): return None
        s = str(int(float(v))).strip()
        if not s: return None
        # 992XXXXXXXXX или 9XXXXXXXXX
        if s.startswith("992") and len(s) >= 12:
            return s
        if s.startswith("9") and len(s) == 9:
            return "992" + s
        if len(s) == 10 and s.startswith("7"):
            return "992" + s[1:]  # 7 912... -> 992 912...
        if len(s) == 11 and s.startswith("79"):
            return "992" + s[1:]
        return s
    with_num["phone_norm"] = with_num["Номер"].apply(normalize_phone)
    found = 0
    not_found = []
    for ph in with_num["phone_norm"].dropna().unique():
        if not ph: continue
        # Пробуем как есть и с 992
        for candidate in [ph, ph.replace("992", "")]:
            r = conn.execute("SELECT 1 FROM leads WHERE phone = ? OR phone = ? OR phone = ?", (ph, candidate, "992"+candidate if not candidate.startswith("992") else candidate)).fetchone()
            if r:
                found += 1
                break
        else:
            not_found.append(ph)
    print("Найдено в leads (по уникальным номерам):", found)
    print("Не найдено примеры:", not_found[:10])
    conn.close()

if __name__ == "__main__":
    main()
