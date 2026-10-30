# -*- coding: utf-8 -*-
"""
paste_to_markdown.py
---------------------
クリップボードに貼り付けた文章（プレーンテキスト / Webページや
Copilotなどからコピーしたリッチテキスト＝HTML）をMarkdownに
変換するための、Windows専用の簡易GUIツールです。

■ 事前準備（初回のみ、コマンドプロンプトで実行）
    pip install pywin32 markdownify beautifulsoup4 markdown
    （PDF出力には markdown が必要です。ブラウザは Windows標準の Edge、
      または Chrome を自動で探して使います。追加インストールは不要です）

■ 使い方
    1) 変換したい文章をコピーしておく
       （Webページの表や、Copilotの回答をそのまま選択してコピーでOK）
    2) このスクリプトを実行する
           python paste_to_markdown.py
    3) 「クリップボードから取り込んで変換」ボタンを押す
       - コピー元がHTML（表などを含むリッチテキスト）を持っている場合は
         自動的にそちらを優先して変換します（表もMarkdown表になります）
       - HTMLが無い場合（メモ帳などのプレーンテキスト）は、
         箇条書き・簡易的な表（スペース/タブ区切り）・コードブロックらしき
         インデント行を判定してMarkdownに整形します
    4) 結果は右側に表示されます。
       「結果をコピー」でクリップボードに戻す、
       「.mdで保存」でファイルに保存、
       「PDFで保存」で出力欄のMarkdownをPDFにして保存できます。
       （出力欄を手で直してからPDF化してもOKです）

    直接、下の「入力」欄にテキストを貼り付けて
    「入力欄のテキストを変換」ボタンで変換することもできます
    （この場合はプレーンテキストとして処理されます）。
"""

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox, filedialog

# --- Windows専用のクリップボード関連ライブラリ（無ければ後で警告表示） ---
try:
    import win32clipboard
    import win32con
    HAVE_WIN32 = True
except ImportError:
    HAVE_WIN32 = False

# --- HTML→Markdown変換（表対応）。無ければHTML変換だけ使えなくする ---
try:
    from markdownify import markdownify as html_to_md
    HAVE_MARKDOWNIFY = True
except ImportError:
    HAVE_MARKDOWNIFY = False

# --- Markdown→HTML変換（PDF出力用）。無ければPDF保存だけ使えなくする ---
try:
    import markdown as md_lib
    HAVE_MARKDOWN_LIB = True
except ImportError:
    HAVE_MARKDOWN_LIB = False


# =========================================================
# クリップボード読み取り
# =========================================================
def read_clipboard_html():
    """クリップボードからHTML形式(CF_HTML)を取得する。無ければNone。"""
    if not HAVE_WIN32:
        return None
    win32clipboard.OpenClipboard()
    try:
        cf_html = win32clipboard.RegisterClipboardFormat("HTML Format")
        if win32clipboard.IsClipboardFormatAvailable(cf_html):
            raw = win32clipboard.GetClipboardData(cf_html)
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", errors="ignore")
            return _extract_html_fragment(raw)
        return None
    finally:
        win32clipboard.CloseClipboard()


def read_clipboard_text():
    """クリップボードからプレーンテキストを取得する。無ければNone。"""
    if not HAVE_WIN32:
        return None
    win32clipboard.OpenClipboard()
    try:
        if win32clipboard.IsClipboardFormatAvailable(win32con.CF_UNICODETEXT):
            return win32clipboard.GetClipboardData(win32con.CF_UNICODETEXT)
        return None
    finally:
        win32clipboard.CloseClipboard()


def write_clipboard_text(text):
    if not HAVE_WIN32:
        return
    win32clipboard.OpenClipboard()
    try:
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardData(win32con.CF_UNICODETEXT, text)
    finally:
        win32clipboard.CloseClipboard()


def _extract_html_fragment(raw_cf_html):
    """
    Windowsの "HTML Format" は先頭にオフセット情報のヘッダが付くので、
    StartFragment/EndFragment を見て実際のHTML断片だけを取り出す。
    """
    start_match = re.search(r"StartFragment:(\d+)", raw_cf_html)
    end_match = re.search(r"EndFragment:(\d+)", raw_cf_html)
    if start_match and end_match:
        start = int(start_match.group(1))
        end = int(end_match.group(1))
        # CF_HTMLのオフセットはUTF-8バイト位置基準なので合わせて計算
        raw_bytes = raw_cf_html.encode("utf-8", errors="ignore")
        try:
            return raw_bytes[start:end].decode("utf-8", errors="ignore")
        except Exception:
            pass
    return raw_cf_html


# =========================================================
# プレーンテキスト → Markdown（簡易ヒューリスティック変換）
# =========================================================
BULLET_PATTERN = re.compile(r"^[\s]*([・●○▪◦*\-])\s*(.+)$")
NUM_BULLET_PATTERN = re.compile(r"^[\s]*(\d+)[\.\)、]\s*(.+)$")
MULTI_SPACE_SPLIT = re.compile(r"\s{2,}|\t+")


def looks_like_table_block(lines):
    """2行以上、かつ各行が2区切り以上に分割できるなら表とみなす"""
    if len(lines) < 2:
        return False
    counts = [len(MULTI_SPACE_SPLIT.split(l.strip())) for l in lines if l.strip()]
    return len(counts) >= 2 and all(c >= 2 for c in counts) and len(set(counts)) <= 2


def convert_table_block(lines):
    rows = [MULTI_SPACE_SPLIT.split(l.strip()) for l in lines if l.strip()]
    col_count = max(len(r) for r in rows)
    rows = [r + [""] * (col_count - len(r)) for r in rows]
    md = []
    md.append("| " + " | ".join(rows[0]) + " |")
    md.append("| " + " | ".join(["---"] * col_count) + " |")
    for r in rows[1:]:
        md.append("| " + " | ".join(r) + " |")
    return "\n".join(md)


def plain_text_to_markdown(text):
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out = []
    i = 0
    n = len(lines)

    while i < n:
        line = lines[i]

        # 空行はそのまま
        if line.strip() == "":
            out.append("")
            i += 1
            continue

        # インデントされた行 → コードブロック候補としてまとめて取り込む
        if line.startswith("    ") or line.startswith("\t"):
            block = []
            while i < n and (lines[i].startswith("    ") or lines[i].startswith("\t") or lines[i].strip() == ""):
                block.append(lines[i])
                i += 1
            while block and block[-1].strip() == "":
                block.pop()
            code = "\n".join(l[4:] if l.startswith("    ") else l[1:] for l in block)
            out.append("```")
            out.append(code)
            out.append("```")
            continue

        # 表っぽいブロックをまとめて判定
        block = []
        j = i
        while j < n and lines[j].strip() != "" and not lines[j].startswith(("    ", "\t")):
            block.append(lines[j])
            j += 1
        if looks_like_table_block(block):
            out.append(convert_table_block(block))
            i = j
            continue

        # 箇条書き（記号）
        m = BULLET_PATTERN.match(line)
        if m:
            out.append(f"- {m.group(2).strip()}")
            i += 1
            continue

        # 箇条書き（数字）
        m = NUM_BULLET_PATTERN.match(line)
        if m:
            out.append(f"{m.group(1)}. {m.group(2).strip()}")
            i += 1
            continue

        # Setext見出し（次の行が ==== か ---- の場合）
        if i + 1 < n and re.match(r"^\s*=+\s*$", lines[i + 1] or ""):
            out.append(f"# {line.strip()}")
            i += 2
            continue
        if i + 1 < n and re.match(r"^\s*-{3,}\s*$", lines[i + 1] or ""):
            out.append(f"## {line.strip()}")
            i += 2
            continue

        # 通常の行はそのまま
        out.append(line)
        i += 1

    # 連続する空行を1つに整理
    result = []
    blank = False
    for l in out:
        if l == "":
            if not blank:
                result.append(l)
            blank = True
        else:
            result.append(l)
            blank = False
    return "\n".join(result).strip() + "\n"


# =========================================================
# HTML → Markdown（表対応、markdownify利用）
# =========================================================
def html_block_to_markdown(html):
    md = html_to_md(html, heading_style="ATX", bullets="-")
    # markdownifyは空行を詰めすぎる/開けすぎることがあるので整理
    md = re.sub(r"\n{3,}", "\n\n", md).strip() + "\n"
    return md


# =========================================================
# Markdown → HTML → PDF（Edge / Chrome のヘッドレス印刷を利用）
# =========================================================
PDF_CSS = """
@page { size: A4; margin: 15mm; }
body {
  font-family: "Yu Gothic", "游ゴシック", "Meiryo", "メイリオ", "MS PGothic", sans-serif;
  font-size: 10.5pt; line-height: 1.7; color: #222;
}
h1, h2, h3, h4 { line-height: 1.35; margin: 1.2em 0 0.5em; }
h1 { font-size: 1.7em; border-bottom: 2px solid #888; padding-bottom: 0.2em; }
h2 { font-size: 1.4em; border-bottom: 1px solid #bbb; padding-bottom: 0.15em; }
h3 { font-size: 1.2em; }
table { border-collapse: collapse; margin: 0.8em 0; }
th, td { border: 1px solid #888; padding: 4px 10px; vertical-align: top; }
th { background: #eee; }
tr { page-break-inside: avoid; }
pre {
  background: #f5f5f5; border: 1px solid #ddd; border-radius: 4px;
  padding: 8px 10px; white-space: pre-wrap; word-break: break-all;
  font-size: 9.5pt; page-break-inside: avoid;
}
code { font-family: Consolas, "MS Gothic", monospace; background: #f5f5f5; }
pre code { background: none; }
blockquote { margin: 0.8em 0; padding: 0 1em; color: #555; border-left: 4px solid #ccc; }
img { max-width: 100%; }
"""


def markdown_to_html(md_text):
    """MarkdownをHTML文書に変換する（表・コードブロック対応）。"""
    body = md_lib.markdown(
        md_text,
        extensions=["tables", "fenced_code", "sane_lists", "nl2br"],
    )
    return (
        "<!DOCTYPE html>\n<html lang=\"ja\">\n<head>\n"
        "<meta charset=\"utf-8\">\n<title>document</title>\n"
        f"<style>{PDF_CSS}</style>\n</head>\n<body>\n{body}\n</body>\n</html>\n"
    )


def find_browser():
    """PDF出力に使う Edge / Chrome の実行ファイルを探す。無ければNone。"""
    roots = [
        os.environ.get("ProgramFiles", r"C:\Program Files"),
        os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
        os.environ.get("LOCALAPPDATA", ""),
    ]
    rel_paths = [
        r"Microsoft\Edge\Application\msedge.exe",
        r"Google\Chrome\Application\chrome.exe",
    ]
    for rel in rel_paths:
        for root in roots:
            if root:
                cand = os.path.join(root, rel)
                if os.path.isfile(cand):
                    return cand
    for name in ("msedge", "chrome", "google-chrome", "chromium"):
        found = shutil.which(name)
        if found:
            return found
    return None


def markdown_to_pdf(md_text, pdf_path):
    """
    MarkdownをPDFファイルとして保存する。
    HTMLに変換 → Edge/Chromeのヘッドレスモードで「PDFに印刷」する方式。
    日本語フォントや表の描画はブラウザ任せなので文字化けしにくい。
    失敗したら RuntimeError を投げる。
    """
    browser = find_browser()
    if browser is None:
        raise RuntimeError("Edge / Chrome が見つかりませんでした。")

    pdf_path = os.path.abspath(pdf_path)
    if os.path.exists(pdf_path):
        os.remove(pdf_path)  # 古いファイルが残って成功と誤判定しないように

    with tempfile.TemporaryDirectory() as tmp:
        html_file = Path(tmp) / "input.html"
        html_file.write_text(markdown_to_html(md_text), encoding="utf-8")
        profile_dir = Path(tmp) / "profile"  # 起動中のEdgeと干渉しないよう専用プロファイルを使う

        cmd = [
            browser,
            "--headless",
            "--disable-gpu",
            "--no-first-run",
            f"--user-data-dir={profile_dir}",
            "--no-pdf-header-footer",
            f"--print-to-pdf={pdf_path}",
            html_file.as_uri(),
        ]
        try:
            proc = subprocess.run(
                cmd, capture_output=True, timeout=90,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError("PDF作成がタイムアウトしました（90秒）。")

    if not os.path.isfile(pdf_path) or os.path.getsize(pdf_path) == 0:
        err = proc.stderr.decode("utf-8", errors="ignore").strip()[-300:]
        raise RuntimeError("PDFが作成されませんでした。\n" + err)


# =========================================================
# GUI
# =========================================================
class App:
    def __init__(self, root):
        self.root = root
        root.title("貼り付け → Markdown変換ツール")
        root.geometry("980x640")

        top = ttk.Frame(root, padding=8)
        top.pack(fill="x")

        ttk.Button(
            top, text="① クリップボードから取り込んで変換",
            command=self.convert_from_clipboard
        ).pack(side="left", padx=4)

        ttk.Button(
            top, text="② 入力欄のテキストを変換",
            command=self.convert_from_input
        ).pack(side="left", padx=4)

        ttk.Button(
            top, text="結果をコピー",
            command=self.copy_result
        ).pack(side="left", padx=4)

        ttk.Button(
            top, text=".mdで保存",
            command=self.save_result
        ).pack(side="left", padx=4)

        ttk.Button(
            top, text="PDFで保存",
            command=self.save_pdf
        ).pack(side="left", padx=4)

        self.status = ttk.Label(root, text=self._status_text(), foreground="#555")
        self.status.pack(fill="x", padx=8)

        paned = ttk.Panedwindow(root, orient="horizontal")
        paned.pack(fill="both", expand=True, padx=8, pady=8)

        left = ttk.Frame(paned)
        right = ttk.Frame(paned)
        paned.add(left, weight=1)
        paned.add(right, weight=1)

        ttk.Label(left, text="入力（プレーンテキストを貼り付け）").pack(anchor="w")
        self.input_box = tk.Text(left, wrap="word", undo=True)
        self.input_box.pack(fill="both", expand=True)

        ttk.Label(right, text="出力（Markdown）").pack(anchor="w")
        self.output_box = tk.Text(right, wrap="word", undo=True)
        self.output_box.pack(fill="both", expand=True)

    def _status_text(self):
        parts = []
        parts.append("pywin32: " + ("OK" if HAVE_WIN32 else "未インストール"))
        parts.append("markdownify: " + ("OK" if HAVE_MARKDOWNIFY else "未インストール（表付きHTML変換不可）"))
        parts.append("markdown(PDF用): " + ("OK" if HAVE_MARKDOWN_LIB else "未インストール"))
        parts.append("Edge/Chrome: " + ("OK" if find_browser() else "見つからない"))
        return " / ".join(parts)

    def convert_from_clipboard(self):
        if not HAVE_WIN32:
            messagebox.showerror(
                "エラー",
                "pywin32がインストールされていません。\n"
                "コマンドプロンプトで下記を実行してください。\n\n"
                "pip install pywin32 markdownify beautifulsoup4"
            )
            return

        html = read_clipboard_html()
        if html and HAVE_MARKDOWNIFY:
            try:
                md = html_block_to_markdown(html)
                self._show_result(md, source="HTML（表を含む可能性あり）")
                return
            except Exception as e:
                messagebox.showwarning(
                    "HTML変換に失敗", f"HTMLのMarkdown変換に失敗したためプレーンテキストで処理します。\n{e}"
                )

        text = read_clipboard_text()
        if text is None:
            messagebox.showinfo("情報", "クリップボードに変換できる内容が見つかりませんでした。")
            return
        md = plain_text_to_markdown(text)
        self._show_result(md, source="プレーンテキスト")

    def convert_from_input(self):
        text = self.input_box.get("1.0", "end-1c")
        if not text.strip():
            messagebox.showinfo("情報", "入力欄が空です。")
            return
        md = plain_text_to_markdown(text)
        self._show_result(md, source="入力欄（プレーンテキスト）")

    def _show_result(self, md, source):
        self.output_box.delete("1.0", "end")
        self.output_box.insert("1.0", md)
        self.status.config(text=self._status_text() + f"　｜ 直近の変換元: {source}")

    def copy_result(self):
        text = self.output_box.get("1.0", "end-1c")
        if not text.strip():
            messagebox.showinfo("情報", "コピーする結果がありません。")
            return
        if HAVE_WIN32:
            write_clipboard_text(text)
            messagebox.showinfo("完了", "クリップボードにコピーしました。")
        else:
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
            messagebox.showinfo("完了", "クリップボードにコピーしました。")

    def save_result(self):
        text = self.output_box.get("1.0", "end-1c")
        if not text.strip():
            messagebox.showinfo("情報", "保存する結果がありません。")
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".md",
            filetypes=[("Markdown", "*.md"), ("すべてのファイル", "*.*")]
        )
        if path:
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
            messagebox.showinfo("完了", f"保存しました:\n{path}")

    def save_pdf(self):
        text = self.output_box.get("1.0", "end-1c")
        if not text.strip():
            messagebox.showinfo("情報", "PDFにする結果がありません。")
            return
        if not HAVE_MARKDOWN_LIB:
            messagebox.showerror(
                "エラー",
                "markdownライブラリがインストールされていません。\n"
                "コマンドプロンプトで下記を実行してください。\n\n"
                "pip install markdown"
            )
            return
        if find_browser() is None:
            messagebox.showerror(
                "エラー",
                "PDF作成に使う Edge / Chrome が見つかりませんでした。\n"
                "「.mdで保存」で保存し、別のツールでPDF化してください。"
            )
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".pdf",
            filetypes=[("PDF", "*.pdf"), ("すべてのファイル", "*.*")]
        )
        if not path:
            return
        self.root.config(cursor="watch")
        self.root.update_idletasks()
        try:
            markdown_to_pdf(text, path)
        except Exception as e:
            messagebox.showerror("PDF作成に失敗", str(e))
        else:
            messagebox.showinfo("完了", f"PDFを保存しました:\n{path}")
        finally:
            self.root.config(cursor="")


if __name__ == "__main__":
    root = tk.Tk()
    app = App(root)
    root.mainloop()
