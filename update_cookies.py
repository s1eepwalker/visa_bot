#!/usr/bin/env python3
"""
Быстрое обновление cookies.
Запустите этот скрипт когда бот сообщит что сессия устарела.
"""
import json
import re

print("=" * 50)
print("  Обновление cookies для монитора визы США")
print("=" * 50)
print()
print("1. Зайдите на ais.usvisa-info.com и залогиньтесь")
print("2. Нажмите F12 -> Network -> обновите страницу (F5)")
print("3. Нажмите на запрос к сайту -> Headers")
print("4. Найдите строку 'cookie:' в Request Headers")
print("5. Скопируйте ВСЁ значение после 'cookie:'")
print()

cookie_str = input("Вставьте строку cookie сюда и нажмите Enter:\n> ").strip()

print()
print("Теперь найдите 'x-csrf-token:' в тех же Headers")
csrf = input("Вставьте значение x-csrf-token (или Enter чтобы пропустить):\n> ").strip()

# Парсим cookie строку в словарь
cookies = {}
for part in cookie_str.split(";"):
    part = part.strip()
    if "=" in part:
        key, _, value = part.partition("=")
        cookies[key.strip()] = value.strip()

if csrf:
    cookies["_csrf_token"] = csrf

with open("cookies.json", "w", encoding="utf-8") as f:
    json.dump(cookies, f, ensure_ascii=False, indent=2)

print()
print(f"Готово! Сохранено {len(cookies)} cookies.")
print("Перезапустите monitor.py если он был остановлен.")
