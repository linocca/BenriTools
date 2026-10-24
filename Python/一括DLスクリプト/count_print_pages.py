# 必須インストール　
# $ pip install pypdf
# $ pip install cryptography

import os
import sys
from pypdf import PdfReader



folder = sys.argv[1] if len(sys.argv) > 1 else "."

total_pages = 0
print(f"{'ファイル名':50} ページ数")
print("-" * 65)

for name in sorted(os.listdir(folder)):
    if name.lower().endswith(".pdf"):
        path = os.path.join(folder, name)
        try:
            reader = PdfReader(path)
            pages = len(reader.pages)
        except Exception as e:
            pages = f"読取失敗({e})"
        print(f"{name:50} {pages}")
        if isinstance(pages, int):
            total_pages += pages

print("-" * 65)
print(f"合計ページ数: {total_pages} 枚")
