#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PDFツール GUI
  タブ1: フォルダ内PDFのページ数カウント
  タブ2: 指定WebページからPDF等のファイルを一括ダウンロード

必須インストール:
    pip install pypdf cryptography requests beautifulsoup4

起動:
    python pdf_tool_gui.py
"""

import os
import re
import sys
import time
import threading
import unicodedata
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from urllib.parse import urljoin, urlparse, parse_qs, unquote

# ---- 外部ライブラリ（未インストールでもGUI自体は起動できるように遅延チェック） ----
try:
    from pypdf import PdfReader
    HAS_PYPDF = True
except ImportError:
    HAS_PYPDF = False

try:
    import requests
    from bs4 import BeautifulSoup
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False


HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}


# =========================================================
# タブ1: PDFページ数カウント（count_print_pages.py 相当）
# =========================================================

def count_pdf_pages(folder, log_func, progress_func=None):
    """folder内のPDFのページ数を数え、ログ行のリストと合計を返す"""
    if not HAS_PYPDF:
        raise RuntimeError("pypdf がインストールされていません。 pip install pypdf cryptography")

    names = sorted(n for n in os.listdir(folder) if n.lower().endswith(".pdf"))
    total_pages = 0
    rows = []
    for i, name in enumerate(names, 1):
        path = os.path.join(folder, name)
        try:
            reader = PdfReader(path)
            pages = len(reader.pages)
        except Exception as e:
            pages = f"読取失敗({e})"
        rows.append((name, pages))
        if isinstance(pages, int):
            total_pages += pages
        if progress_func:
            progress_func(i, len(names))
        log_func(f"{name:50} {pages}")
    return rows, total_pages


class CountPagesTab(ttk.Frame):
    def __init__(self, master):
        super().__init__(master, padding=10)
        self.folder_var = tk.StringVar()

        row = ttk.Frame(self)
        row.pack(fill="x", pady=(0, 8))
        ttk.Label(row, text="対象フォルダ:").pack(side="left")
        ttk.Entry(row, textvariable=self.folder_var).pack(
            side="left", fill="x", expand=True, padx=6
        )
        ttk.Button(row, text="参照...", command=self.browse_folder).pack(side="left")

        btn_row = ttk.Frame(self)
        btn_row.pack(fill="x", pady=(0, 8))
        self.run_btn = ttk.Button(btn_row, text="ページ数を数える", command=self.run)
        self.run_btn.pack(side="left")
        self.progress = ttk.Progressbar(btn_row, mode="determinate")
        self.progress.pack(side="left", fill="x", expand=True, padx=10)

        self.text = tk.Text(self, wrap="none", height=20)
        self.text.pack(fill="both", expand=True)
        yscroll = ttk.Scrollbar(self, orient="vertical", command=self.text.yview)
        yscroll.place(relx=1.0, rely=0, relheight=1.0, anchor="ne")

        self.total_var = tk.StringVar(value="合計ページ数: -")
        ttk.Label(self, textvariable=self.total_var, font=("", 10, "bold")).pack(
            anchor="e", pady=(6, 0)
        )

    def browse_folder(self):
        folder = filedialog.askdirectory()
        if folder:
            self.folder_var.set(folder)

    def log(self, line):
        self.text.insert("end", line + "\n")
        self.text.see("end")

    def run(self):
        folder = self.folder_var.get().strip() or "."
        if not os.path.isdir(folder):
            messagebox.showerror("エラー", "フォルダが見つかりません。")
            return
        self.text.delete("1.0", "end")
        self.total_var.set("合計ページ数: -")
        self.run_btn.config(state="disabled")

        def worker():
            try:
                names = [n for n in os.listdir(folder) if n.lower().endswith(".pdf")]
                self.progress.config(maximum=max(len(names), 1), value=0)

                def progress_func(i, n):
                    self.progress.config(value=i)

                rows, total = count_pdf_pages(folder, self.log, progress_func)
                self.total_var.set(f"合計ページ数: {total} 枚（{len(rows)} ファイル）")
            except Exception as e:
                messagebox.showerror("エラー", str(e))
            finally:
                self.run_btn.config(state="normal")

        threading.Thread(target=worker, daemon=True).start()


# =========================================================
# タブ2: PDF一括ダウンロード（download_pdfs.py 相当）
# =========================================================

def sanitize_filename(name: str) -> str:
    name = unicodedata.normalize("NFC", name)
    name = re.sub(r'[\\/:*?"<>|]', "_", name)
    return name.strip() or "unnamed.pdf"


def guess_filename(file_url: str, extensions):
    parsed = urlparse(file_url)
    qs = parse_qs(parsed.query)
    for key in ("n", "name", "filename"):
        if key in qs and qs[key]:
            candidate = unquote(qs[key][0])
            if any(candidate.lower().endswith(ext) for ext in extensions):
                return sanitize_filename(candidate)
    base = os.path.basename(parsed.path)
    if base:
        return sanitize_filename(unquote(base))
    return "unnamed" + (extensions[0] if extensions else "")


def find_target_links(page_url: str, extensions):
    resp = requests.get(page_url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    resp.encoding = resp.apparent_encoding

    soup = BeautifulSoup(resp.text, "html.parser")
    links = []
    seen = set()

    candidates = []
    for a in soup.find_all("a", href=True):
        candidates.append(a["href"])
    for img in soup.find_all("img", src=True):
        candidates.append(img["src"])
    for tag in soup.find_all(["video", "source"], src=True):
        candidates.append(tag["src"])

    for href in candidates:
        full_url = urljoin(page_url, href)
        lower = full_url.lower()
        if any(ext in lower for ext in extensions):
            if full_url not in seen:
                seen.add(full_url)
                links.append(full_url)
    return links


def download_file(url: str, out_dir: str, delay: float, extensions):
    filename = guess_filename(url, extensions)
    out_path = os.path.join(out_dir, filename)
    root, ext = os.path.splitext(out_path)
    counter = 1
    while os.path.exists(out_path):
        out_path = f"{root}_{counter}{ext}"
        counter += 1

    resp = requests.get(url, headers=HEADERS, timeout=60)
    resp.raise_for_status()
    with open(out_path, "wb") as f:
        f.write(resp.content)
    time.sleep(delay)
    return os.path.basename(out_path)


class DownloadTab(ttk.Frame):
    def __init__(self, master):
        super().__init__(master, padding=10)
        self.url_var = tk.StringVar()
        self.output_var = tk.StringVar(value="./downloads")
        self.custom_ext_var = tk.StringVar()
        self.delay_var = tk.DoubleVar(value=0.5)
        self.EXT_CHOICES = [
            "pdf", "jpg", "jpeg", "png", "gif",
            "mp4", "mov", "avi",
            "doc", "docx", "xls", "xlsx", "ppt", "pptx",
            "zip",
        ]
        self.dry_run_var = tk.BooleanVar(value=False)
        self._links_cache = []

        row1 = ttk.Frame(self)
        row1.pack(fill="x", pady=(0, 6))
        ttk.Label(row1, text="対象ページURL:").pack(side="left")
        ttk.Entry(row1, textvariable=self.url_var).pack(
            side="left", fill="x", expand=True, padx=6
        )

        row2 = ttk.Frame(self)
        row2.pack(fill="x", pady=(0, 6))
        ttk.Label(row2, text="保存先フォルダ:").pack(side="left")
        ttk.Entry(row2, textvariable=self.output_var).pack(
            side="left", fill="x", expand=True, padx=6
        )
        ttk.Button(row2, text="参照...", command=self.browse_output).pack(side="left")

        ext_frame = ttk.LabelFrame(self, text="対象拡張子（複数選択可）")
        ext_frame.pack(fill="x", pady=(0, 6))

        list_row = ttk.Frame(ext_frame)
        list_row.pack(fill="x", padx=6, pady=6)
        self.ext_listbox = tk.Listbox(
            list_row, selectmode="extended", height=4, exportselection=False
        )
        for ext in self.EXT_CHOICES:
            self.ext_listbox.insert("end", ext)
        self.ext_listbox.selection_set(0)  # デフォルトで pdf を選択
        self.ext_listbox.pack(side="left", fill="x", expand=True)
        ext_scroll = ttk.Scrollbar(
            list_row, orient="vertical", command=self.ext_listbox.yview
        )
        ext_scroll.pack(side="left", fill="y")
        self.ext_listbox.config(yscrollcommand=ext_scroll.set)

        custom_row = ttk.Frame(ext_frame)
        custom_row.pack(fill="x", padx=6, pady=(0, 6))
        ttk.Label(custom_row, text="その他(カンマ区切り):").pack(side="left")
        ttk.Entry(custom_row, textvariable=self.custom_ext_var).pack(
            side="left", fill="x", expand=True, padx=6
        )

        row3 = ttk.Frame(self)
        row3.pack(fill="x", pady=(0, 6))
        ttk.Label(row3, text="間隔(秒):").pack(side="left")
        ttk.Spinbox(
            row3, from_=0, to=10, increment=0.1, textvariable=self.delay_var, width=6
        ).pack(side="left", padx=6)
        ttk.Checkbutton(row3, text="Dry-run(一覧のみ)", variable=self.dry_run_var).pack(
            side="left", padx=(12, 0)
        )

        btn_row = ttk.Frame(self)
        btn_row.pack(fill="x", pady=(0, 8))
        self.search_btn = ttk.Button(btn_row, text="リンクを検索", command=self.search)
        self.search_btn.pack(side="left")
        self.download_btn = ttk.Button(
            btn_row, text="ダウンロード実行", command=self.download, state="disabled"
        )
        self.download_btn.pack(side="left", padx=6)
        self.progress = ttk.Progressbar(btn_row, mode="determinate")
        self.progress.pack(side="left", fill="x", expand=True, padx=10)

        self.text = tk.Text(self, wrap="none", height=18)
        self.text.pack(fill="both", expand=True)

    def browse_output(self):
        folder = filedialog.askdirectory()
        if folder:
            self.output_var.set(folder)

    def log(self, line):
        self.text.insert("end", line + "\n")
        self.text.see("end")

    def get_extensions(self):
        selected = [self.ext_listbox.get(i) for i in self.ext_listbox.curselection()]
        custom = [
            e.strip().lstrip(".").lower()
            for e in self.custom_ext_var.get().split(",")
            if e.strip()
        ]
        merged = []
        for e in selected + custom:
            e = "." + e.lower()
            if e not in merged:
                merged.append(e)
        return merged

    def search(self):
        if not HAS_REQUESTS:
            messagebox.showerror(
                "エラー", "requests / beautifulsoup4 がインストールされていません。"
            )
            return
        url = self.url_var.get().strip()
        if not url:
            messagebox.showerror("エラー", "URLを入力してください。")
            return

        extensions = self.get_extensions()
        if not extensions:
            messagebox.showerror("エラー", "拡張子を1つ以上選択してください。")
            return

        self.text.delete("1.0", "end")
        self.download_btn.config(state="disabled")
        self.search_btn.config(state="disabled")

        def worker():
            try:
                self.log(f"対象拡張子: {', '.join(extensions)}")
                self.log(f"ページを解析中: {url}")
                links = find_target_links(url, extensions)
                self._links_cache = links
                if not links:
                    self.log("該当ファイルへのリンクが見つかりませんでした。")
                    return
                self.log(f"{len(links)} 件のリンクが見つかりました。")
                for i, link in enumerate(links, 1):
                    self.log(f"  [{i}] {guess_filename(link, extensions)}")
                self.download_btn.config(state="normal")
            except Exception as e:
                messagebox.showerror("エラー", str(e))
            finally:
                self.search_btn.config(state="normal")

        threading.Thread(target=worker, daemon=True).start()

    def download(self):
        if self.dry_run_var.get():
            self.log("\n--dry-run のため、ダウンロードは行いません。")
            return
        links = self._links_cache
        if not links:
            messagebox.showinfo("情報", "先に「リンクを検索」を実行してください。")
            return

        out_dir = self.output_var.get().strip() or "./downloads"
        delay = self.delay_var.get()
        extensions = self.get_extensions()
        os.makedirs(out_dir, exist_ok=True)

        self.download_btn.config(state="disabled")
        self.search_btn.config(state="disabled")
        self.progress.config(maximum=len(links), value=0)

        def worker():
            ok, ng = 0, 0
            self.log(f"\n保存先: {os.path.abspath(out_dir)}")
            for i, link in enumerate(links, 1):
                try:
                    fname = download_file(link, out_dir, delay, extensions)
                    self.log(f"  ダウンロード完了: {fname}")
                    ok += 1
                except Exception as e:
                    self.log(f"  失敗: {link} -> {e}")
                    ng += 1
                self.progress.config(value=i)
            self.log(f"\n完了: 成功 {ok} 件 / 失敗 {ng} 件")
            self.download_btn.config(state="normal")
            self.search_btn.config(state="normal")

        threading.Thread(target=worker, daemon=True).start()


# =========================================================
# メインウィンドウ
# =========================================================

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("PDFツール")
        self.geometry("780x560")

        notebook = ttk.Notebook(self)
        notebook.pack(fill="both", expand=True)

        notebook.add(CountPagesTab(notebook), text="PDFページ数カウント")
        notebook.add(DownloadTab(notebook), text="PDF一括ダウンロード")

        missing = []
        if not HAS_PYPDF:
            missing.append("pypdf / cryptography")
        if not HAS_REQUESTS:
            missing.append("requests / beautifulsoup4")
        if missing:
            self.after(
                300,
                lambda: messagebox.showwarning(
                    "ライブラリ不足",
                    "以下がインストールされていません:\n"
                    + "\n".join(missing)
                    + "\n\npip install pypdf cryptography requests beautifulsoup4",
                ),
            )


if __name__ == "__main__":
    App().mainloop()
