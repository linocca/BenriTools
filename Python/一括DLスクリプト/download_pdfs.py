#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
指定したWebページ内にあるPDFリンクを一括ダウンロードするスクリプト。

使い方:
    pip install requests beautifulsoup4
    python download_pdfs.py "https://example.com/some_page.html" -o ./downloads

オプション:
    -o, --output   保存先フォルダ（デフォルト: ./pdf_downloads）
    -d, --delay    ダウンロード間隔(秒) サーバー負荷軽減用（デフォルト: 0.5）
    --dry-run      実際にはダウンロードせず、見つかったPDF一覧だけ表示
"""

import argparse
import os
import re
import sys
import time
import unicodedata
from urllib.parse import urljoin, urlparse, parse_qs, unquote

import requests
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}


def sanitize_filename(name: str) -> str:
    """OSで使えない文字を除去し、ファイル名として安全な形にする"""
    name = unicodedata.normalize("NFC", name)
    name = re.sub(r'[\\/:*?"<>|]', "_", name)
    return name.strip() or "unnamed.pdf"


def guess_filename(file_url: str, extensions: list) -> str:
    """
    URLからファイル名を推測する。
    多くの大学入試センターのようなサイトは ?f=xxx.pdf&n=表示名.pdf のように
    クエリパラメータに「本来のファイル名」を持たせている場合があるので、
    n= があればそれを優先的に使う。
    """
    parsed = urlparse(file_url)
    qs = parse_qs(parsed.query)

    for key in ("n", "name", "filename"):
        if key in qs and qs[key]:
            candidate = unquote(qs[key][0])
            if any(candidate.lower().endswith(ext) for ext in extensions):
                return sanitize_filename(candidate)

    # クエリになければパスの最後の部分を使う
    base = os.path.basename(parsed.path)
    if base:
        return sanitize_filename(unquote(base))

    return "unnamed" + (extensions[0] if extensions else "")


def find_target_links(page_url: str, extensions: list) -> list:
    """
    指定ページを取得し、対象拡張子(例: .pdf, .jpg, .mp4 等)へのリンクを抽出する。
    <a href="..."> だけでなく、画像タグ <img src="..."> や
    動画タグ <video src="...">/<source src="..."> も拾う。
    """
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


def download_file(url: str, out_dir: str, delay: float, extensions: list) -> None:
    filename = guess_filename(url, extensions)
    out_path = os.path.join(out_dir, filename)

    # 同名ファイルがあれば連番を付ける
    root, ext = os.path.splitext(out_path)
    counter = 1
    while os.path.exists(out_path):
        out_path = f"{root}_{counter}{ext}"
        counter += 1

    print(f"  ダウンロード中: {filename}")
    resp = requests.get(url, headers=HEADERS, timeout=60)
    resp.raise_for_status()

    with open(out_path, "wb") as f:
        f.write(resp.content)

    time.sleep(delay)


def main():
    parser = argparse.ArgumentParser(description="ページ内のPDFを一括ダウンロード")
    parser.add_argument("url", help="対象ページのURL")
    parser.add_argument("-o", "--output", default="./downloads", help="保存先フォルダ")
    parser.add_argument("-d", "--delay", type=float, default=0.5, help="ダウンロード間隔(秒)")
    parser.add_argument(
        "-e", "--ext",
        default="pdf",
        help="対象拡張子をカンマ区切りで指定 (例: pdf / jpg,png,gif / mp4,mov)",
    )
    parser.add_argument("--dry-run", action="store_true", help="ダウンロードせず一覧のみ表示")
    args = parser.parse_args()

    extensions = ["." + e.strip().lstrip(".").lower() for e in args.ext.split(",") if e.strip()]
    print(f"対象拡張子: {', '.join(extensions)}")

    print(f"ページを解析中: {args.url}")
    try:
        links = find_target_links(args.url, extensions)
    except requests.RequestException as e:
        print(f"エラー: ページの取得に失敗しました -> {e}", file=sys.stderr)
        sys.exit(1)

    if not links:
        print("該当ファイルへのリンクが見つかりませんでした。")
        sys.exit(0)

    print(f"{len(links)} 件のリンクが見つかりました。")
    for i, link in enumerate(links, 1):
        print(f"  [{i}] {guess_filename(link, extensions)}")

    if args.dry_run:
        print("\n--dry-run のため、ダウンロードは行いません。")
        return

    os.makedirs(args.output, exist_ok=True)
    print(f"\n保存先: {os.path.abspath(args.output)}")

    ok, ng = 0, 0
    for link in links:
        try:
            download_file(link, args.output, args.delay, extensions)
            ok += 1
        except requests.RequestException as e:
            print(f"  失敗: {link} -> {e}")
            ng += 1

    print(f"\n完了: 成功 {ok} 件 / 失敗 {ng} 件")


if __name__ == "__main__":
    main()
