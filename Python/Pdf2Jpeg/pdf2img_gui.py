"""
PDF ⇄ 画像変換 GUIツール
  タブ1: PDF → 画像 (ページ範囲 / 奇数・偶数 / DPI 指定)
  タブ2: 画像 → PDF (並べ替え / ページサイズ指定)

必要: pip install pymupdf
任意: pip install tkinterdnd2   (ドラッグ&ドロップを有効にする)
      pip install pillow        (WebP画像を読み込めるようにする)
"""
import io
import os
import queue
import re
import sys
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

try:
    import pymupdf as fitz  # PyMuPDF (新しい版)
except ImportError:
    import fitz  # PyMuPDF (古い版)

try:
    from tkinterdnd2 import TkinterDnD, DND_FILES
    HAS_DND = True
except ImportError:
    HAS_DND = False

try:
    from PIL import Image, ImageOps
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

BaseTk = TkinterDnD.Tk if HAS_DND else tk.Tk

IMG_EXTS = (".png", ".jpg", ".jpeg", ".bmp", ".gif", ".tif", ".tiff", ".webp")
A4_PT = (595.276, 841.89)   # A4 縦 (pt)
PDF_MAX_PT = 14400          # PDFのページサイズ上限 (200インチ)


# ====================================================================
#  ロジック (GUIに依存しない部分)
# ====================================================================
def natural_key(s):
    """page2 < page10 になる自然順ソート用キー。"""
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


def parse_range(text, n):
    """'1-5,8,10-' のような文字列を、1始まりのページ番号リストにする。
    空欄なら全ページ。書式が不正なら ValueError。"""
    for a, b in (("，", ","), ("、", ","), ("－", "-"), ("ー", "-"), ("〜", "-"),
                 ("～", "-"), ("~", "-"), ("　", " ")):
        text = text.replace(a, b)
    # 全角数字 → 半角
    text = text.translate(str.maketrans("０１２３４５６７８９", "0123456789"))
    text = text.replace(" ", "")
    if not text:
        return list(range(1, n + 1))

    pages = set()
    for part in text.split(","):
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            s = int(a) if a else 1
            e = int(b) if b else n
        else:
            s = e = int(part)
        if s < 1 or s > e:
            raise ValueError(f"範囲が不正です: {part}")
        pages.update(range(s, min(e, n) + 1))
    return sorted(pages)


def select_pages(text, mode, n):
    """範囲指定 + 奇数/偶数フィルタを適用したページ番号リスト(1始まり)を返す。"""
    pages = parse_range(text, n)
    if mode == "odd":
        pages = [p for p in pages if p % 2 == 1]
    elif mode == "even":
        pages = [p for p in pages if p % 2 == 0]
    return pages


def _open_image_as_pdf(path):
    """画像を1枚もののPDFドキュメントとして開く。
    この経路だとJPEGは再圧縮されず、EXIFの向きも反映される。
    MuPDFが読めない形式(WebPなど)はPillow経由でPNGにして開く。"""
    try:
        with fitz.open(path) as img:
            return fitz.open("pdf", img.convert_to_pdf())
    except Exception:
        if not HAS_PIL:
            raise RuntimeError(
                "画像を読み込めません（WebP等は pip install pillow が必要です）")
        with Image.open(path) as im:
            im = ImageOps.exif_transpose(im)
            if im.mode not in ("RGB", "RGBA", "L"):
                im = im.convert("RGB")
            buf = io.BytesIO()
            im.save(buf, "PNG")
        with fitz.open("png", buf.getvalue()) as img:
            return fitz.open("pdf", img.convert_to_pdf())


def _image_pixel_size(src):
    """src[0] に入っている画像の、向き補正後のピクセル数 (幅, 高さ) を返す。"""
    page = src[0]
    r = page.rect
    imgs = page.get_images(full=True)
    if not imgs:  # 想定外: 96dpi 相当で逆算
        return r.width * 96 / 72, r.height * 96 / 72
    w, h = imgs[0][2], imgs[0][3]  # 画像そのもののピクセル数 (回転前)
    # ページ矩形の縦横と食い違っていれば、EXIFの向きで90度回っている
    if w != h and (w > h) != (r.width > r.height):
        w, h = h, w
    return w, h


def images_to_pdf(files, out_path, mode, dpi, margin_mm, on_progress=None):
    """画像のリストを1つのPDFにまとめる。
    mode: "image"=画像サイズに合わせる(dpiで物理サイズを決定) /
          "a4p"=A4縦 / "a4l"=A4横 / "a4auto"=A4で画像の向きに合わせる
    戻り値: (追加できたページ数, エラーメッセージのリスト)"""
    doc = fitz.open()
    errors = []
    added = 0
    m = margin_mm * 72 / 25.4
    try:
        for i, path in enumerate(files, 1):
            try:
                with _open_image_as_pdf(path) as src:
                    w, h = _image_pixel_size(src)
                    if mode == "image":
                        pw, ph = w * 72 / dpi, h * 72 / dpi
                        big = max(pw, ph)
                        if big > PDF_MAX_PT:  # PDFの上限を超えるなら縮小
                            pw, ph = pw * PDF_MAX_PT / big, ph * PDF_MAX_PT / big
                        rect = fitz.Rect(0, 0, pw, ph)
                    else:
                        if mode == "a4p":
                            pw, ph = A4_PT
                        elif mode == "a4l":
                            pw, ph = A4_PT[1], A4_PT[0]
                        else:
                            pw, ph = (A4_PT[1], A4_PT[0]) if w > h else A4_PT
                        rect = fitz.Rect(m, m, pw - m, ph - m)
                    page = doc.new_page(width=pw, height=ph)
                    # 縦横比を保ったまま rect の中央に収める
                    page.show_pdf_page(rect, src, 0)
                    added += 1
            except Exception as e:
                errors.append(f"{os.path.basename(path)}: {e}")
            if on_progress:
                on_progress(i, path)
        if added:
            doc.save(out_path, deflate=True, garbage=3)
    finally:
        doc.close()
    return added, errors


# 濃淡の判定基準 (グレースケール 0=黒 〜 255=白)
WHITE_MIN = 235   # この値以上のピクセルを「ほぼ白」とみなす
BLACK_MAX = 30    # この値以下のピクセルを「ほぼ黒」とみなす
_WHITE_TBL = bytes(1 if i >= WHITE_MIN else 0 for i in range(256))
_BLACK_TBL = bytes(1 if i <= BLACK_MAX else 0 for i in range(256))


def tone_ratios(page, check_dpi=72):
    """ページを低解像度のグレースケールで描画し、(白の割合, 黒の割合) を0〜1で返す。
    本番の解像度で描画する前の判定用なので、軽くなるよう低解像度で調べる。"""
    pix = page.get_pixmap(dpi=check_dpi, colorspace=fitz.csGRAY, alpha=False)
    s = pix.samples
    total = len(s)
    if total == 0:
        return 0.0, 0.0
    white = s.translate(_WHITE_TBL).count(1)
    black = s.translate(_BLACK_TBL).count(1)
    return white / total, black / total


def skip_reason(page, skip_white, skip_black, threshold_pct):
    """除外すべきページなら "white" / "black"、残すなら None を返す。
    threshold_pct: 画面のうち白(黒)が何%以上なら除外するか。"""
    if not (skip_white or skip_black):
        return None
    w, b = tone_ratios(page)
    limit = threshold_pct / 100.0
    if skip_white and w >= limit:
        return "white"
    if skip_black and b >= limit:
        return "black"
    return None


# ===== GUI =====
# ====================================================================
#  GUI
# ====================================================================
class FileListTab(ttk.Frame):
    """ファイルリスト(追加/削除/クリア)と実行バーを持つタブの共通部分。"""
    EXTS = ()
    LIST_TITLE = ""
    DIALOG_TITLE = ""
    DIALOG_TYPES = []
    EMPTY_HINT = ""

    def __init__(self, master):
        super().__init__(master)
        self.files = []
        self.running = False
        hint = self.EMPTY_HINT
        if not HAS_DND:
            hint += "（ドラッグ&ドロップは pip install tkinterdnd2 で有効になります）"
        self.empty_hint = hint
        self.status = tk.StringVar(value=hint)
        # 変換スレッドからの画面更新は、Tkを直接触らずキュー経由でメインスレッドに任せる
        self._q = queue.Queue()
        self.after(100, self._drain)
        self._build_list_area()

    def post(self, fn):
        """別スレッドから呼ぶ: 画面更新の処理(fn)をメインスレッドで実行してもらう。"""
        self._q.put(fn)

    def _drain(self):
        try:
            while True:
                self._q.get_nowait()()
        except queue.Empty:
            pass
        finally:
            self.after(100, self._drain)  # 途中で例外が出ても監視は止めない

    # ---------- 共通UI ----------
    def _build_list_area(self):
        title = self.LIST_TITLE + ("（ここにドラッグ&ドロップ可）" if HAS_DND else "")
        f1 = ttk.LabelFrame(self, text=title)
        f1.pack(fill="both", expand=True, padx=10, pady=5)

        self.listbox = tk.Listbox(f1, height=6, selectmode="extended")
        sb = ttk.Scrollbar(f1, orient="vertical", command=self.listbox.yview)
        self.listbox.configure(yscrollcommand=sb.set)
        self.listbox.pack(side="left", fill="both", expand=True, padx=(5, 0), pady=5)
        sb.pack(side="left", fill="y", pady=5)

        bf = ttk.Frame(f1)
        bf.pack(side="left", fill="y", padx=5, pady=5)
        ttk.Button(bf, text="追加...", command=self.add_files).pack(fill="x", pady=2)
        ttk.Button(bf, text="削除", command=self.remove_files).pack(fill="x", pady=2)
        ttk.Button(bf, text="クリア", command=self.clear_files).pack(fill="x", pady=2)
        self._extra_buttons(bf)

    def _extra_buttons(self, parent):
        pass  # サブクラスでボタンを足す

    def _build_run_area(self, button_text="変換開始"):
        f = ttk.Frame(self)
        f.pack(fill="x", padx=10, pady=5)
        self.run_btn = ttk.Button(f, text=button_text, command=self.start)
        self.run_btn.pack(side="left")
        self.progress = ttk.Progressbar(f, mode="determinate")
        self.progress.pack(side="left", fill="x", expand=True, padx=10)
        ttk.Label(self, textvariable=self.status).pack(fill="x", padx=10, pady=(0, 8))

    # ---------- ファイル操作 ----------
    def collect(self, paths):
        """パスのリスト(フォルダ含む)から、このタブで扱えるファイルだけを取り出す。"""
        found = []
        for p in paths:
            if os.path.isdir(p):
                try:
                    names = os.listdir(p)
                except OSError:
                    continue
                for n in names:
                    fp = os.path.join(p, n)
                    if n.lower().endswith(self.EXTS) and os.path.isfile(fp):
                        found.append(fp)
            elif p.lower().endswith(self.EXTS):
                found.append(p)
        return sorted(found, key=natural_key)

    def add_paths(self, paths):
        paths = list(paths)
        skipped = sum(1 for p in paths
                      if not os.path.isdir(p) and not p.lower().endswith(self.EXTS))
        for p in self.collect(paths):
            if p not in self.files:
                self.files.append(p)
                self.listbox.insert("end", p)
        msg = f"{len(self.files)} 件"
        if skipped:
            msg += f"（対象外の {skipped} 件は無視しました）"
        self.status.set(msg)

    def add_files(self):
        paths = filedialog.askopenfilenames(
            title=self.DIALOG_TITLE, filetypes=self.DIALOG_TYPES)
        self.add_paths(paths)

    def remove_files(self):
        for i in reversed(self.listbox.curselection()):
            self.listbox.delete(i)
            del self.files[i]
        self.status.set(f"{len(self.files)} 件" if self.files else self.empty_hint)

    def clear_files(self):
        self.listbox.delete(0, "end")
        self.files.clear()
        self.status.set(self.empty_hint)

    def start(self):
        raise NotImplementedError


# --------------------------------------------------------------------
#  タブ1: PDF → 画像
# --------------------------------------------------------------------
class PdfToImageTab(FileListTab):
    EXTS = (".pdf",)
    LIST_TITLE = "PDFファイル"
    DIALOG_TITLE = "PDFを選択"
    DIALOG_TYPES = [("PDFファイル", "*.pdf")]
    EMPTY_HINT = "PDFを追加してください"

    def __init__(self, master):
        super().__init__(master)
        self.out_dir = tk.StringVar()
        self.fmt = tk.StringVar(value="PNG")
        self.dpi = tk.IntVar(value=200)
        self.same_dir = tk.BooleanVar(value=True)
        self.range_text = tk.StringVar()
        self.page_mode = tk.StringVar(value="all")
        self.skip_white = tk.BooleanVar(value=False)
        self.skip_black = tk.BooleanVar(value=False)
        self.threshold = tk.IntVar(value=90)
        self._build_options()
        self._build_run_area("変換開始")

    def _build_options(self):
        pad = {"padx": 10, "pady": 5}

        # ページ指定
        f_pg = ttk.LabelFrame(self, text="ページ指定")
        f_pg.pack(fill="x", **pad)
        ttk.Label(f_pg, text="範囲:").grid(row=0, column=0, padx=5, pady=5, sticky="w")
        ttk.Entry(f_pg, textvariable=self.range_text, width=28).grid(
            row=0, column=1, padx=5, sticky="w")
        ttk.Label(f_pg, text="空欄=全ページ　例: 1-5,8,10-  （10- は10ページ以降）",
                  foreground="gray").grid(row=0, column=2, padx=5, sticky="w")
        ttk.Label(f_pg, text="対象:").grid(row=1, column=0, padx=5, pady=5, sticky="w")
        rf = ttk.Frame(f_pg)
        rf.grid(row=1, column=1, columnspan=2, sticky="w", padx=5)
        for text, val in (("すべて", "all"), ("奇数ページのみ", "odd"), ("偶数ページのみ", "even")):
            ttk.Radiobutton(rf, text=text, value=val, variable=self.page_mode).pack(
                side="left", padx=(0, 15))

        # 出力先
        f2 = ttk.LabelFrame(self, text="出力先")
        f2.pack(fill="x", **pad)
        ttk.Checkbutton(
            f2, text="PDFと同じ場所に「PDF名_images」フォルダを作成",
            variable=self.same_dir, command=self._toggle_out
        ).grid(row=0, column=0, columnspan=3, sticky="w", padx=5, pady=3)
        self.out_entry = ttk.Entry(f2, textvariable=self.out_dir, state="disabled")
        self.out_entry.grid(row=1, column=0, sticky="ew", padx=5, pady=3)
        self.out_btn = ttk.Button(f2, text="参照...", command=self.choose_out, state="disabled")
        self.out_btn.grid(row=1, column=1, padx=5, pady=3)
        f2.columnconfigure(0, weight=1)

        # 画像設定
        f3 = ttk.LabelFrame(self, text="画像設定")
        f3.pack(fill="x", **pad)
        ttk.Label(f3, text="形式:").grid(row=0, column=0, padx=5, pady=5)
        ttk.Combobox(
            f3, textvariable=self.fmt, values=["PNG", "JPEG"],
            state="readonly", width=8
        ).grid(row=0, column=1, padx=5)
        ttk.Label(f3, text="解像度(DPI):").grid(row=0, column=2, padx=(20, 5))
        ttk.Combobox(
            f3, textvariable=self.dpi, values=[72, 100, 150, 200, 300, 400, 600], width=8
        ).grid(row=0, column=3, padx=5)

        # 白/黒ページの除外
        f4 = ttk.LabelFrame(self, text="白・黒ページの除外（該当ページは画像を保存しません）")
        f4.pack(fill="x", **pad)
        ttk.Checkbutton(f4, text="ほぼ真っ白のページを除外", variable=self.skip_white,
                        command=self._toggle_skip).grid(row=0, column=0, padx=5, pady=3, sticky="w")
        ttk.Checkbutton(f4, text="ほぼ真っ黒のページを除外", variable=self.skip_black,
                        command=self._toggle_skip).grid(row=0, column=1, padx=15, pady=3, sticky="w")
        ttk.Label(f4, text="基準:").grid(row=1, column=0, padx=5, pady=3, sticky="e")
        sf = ttk.Frame(f4)
        sf.grid(row=1, column=1, padx=15, sticky="w")
        self.thr_box = ttk.Spinbox(sf, from_=50, to=100, increment=1, width=6,
                                   textvariable=self.threshold, state="disabled")
        self.thr_box.pack(side="left")
        ttk.Label(sf, text="% 以上が白（黒）なら除外").pack(side="left", padx=5)
        ttk.Label(f4, foreground="gray",
                  text="文字が少ないだけのページも消えることがあります。白紙だけ消したいときは 98〜99% 程度に。"
                  ).grid(row=2, column=0, columnspan=3, sticky="w", padx=5, pady=(0, 5))

    def _toggle_skip(self):
        on = self.skip_white.get() or self.skip_black.get()
        self.thr_box.config(state="normal" if on else "disabled")

    def _toggle_out(self):
        state = "disabled" if self.same_dir.get() else "normal"
        self.out_entry.config(state=state)
        self.out_btn.config(state=state)

    def choose_out(self):
        d = filedialog.askdirectory(title="出力フォルダを選択")
        if d:
            self.out_dir.set(d)

    # ---------- 変換 ----------
    def start(self):
        if self.running:
            return
        if not self.files:
            messagebox.showwarning("確認", "PDFを追加してください")
            return
        if not self.same_dir.get() and not self.out_dir.get():
            messagebox.showwarning("確認", "出力フォルダを指定してください")
            return
        try:
            dpi = int(self.dpi.get())
            if not 10 <= dpi <= 1200:
                raise ValueError
        except (ValueError, tk.TclError):
            messagebox.showwarning("確認", "DPIは10〜1200の数値で指定してください")
            return
        # ページ範囲の書式チェック
        try:
            parse_range(self.range_text.get(), 9999)
        except ValueError:
            messagebox.showwarning(
                "確認", "ページ範囲の書式が正しくありません。\n例: 1-5,8,10-")
            return
        # 白/黒除外の基準チェック
        skip_white, skip_black = self.skip_white.get(), self.skip_black.get()
        threshold = 90
        if skip_white or skip_black:
            try:
                threshold = int(self.threshold.get())
                if not 50 <= threshold <= 100:
                    raise ValueError
            except (ValueError, tk.TclError):
                messagebox.showwarning("確認", "除外の基準は50〜100(%)の数値で指定してください")
                return

        # スレッドからTkの変数を触らないよう、ここで値を確定させる
        opts = {
            "skip_white": skip_white,
            "skip_black": skip_black,
            "threshold": threshold,
            "dpi": dpi,
            "ext": "png" if self.fmt.get() == "PNG" else "jpg",
            "range": self.range_text.get(),
            "mode": self.page_mode.get(),
            "same_dir": self.same_dir.get(),
            "out_dir": self.out_dir.get(),
            "files": list(self.files),
        }
        self.running = True
        self.run_btn.config(state="disabled")
        threading.Thread(target=self.convert, args=(opts,), daemon=True).start()

    def convert(self, o):
        errors = []
        skipped = []  # (PDF名, ページ番号, "white"/"black")
        saved = 0
        try:
            # 先に各PDFの対象ページを決めて、総数を出す
            plan = []
            for path in o["files"]:
                try:
                    with fitz.open(path) as d:
                        n = len(d)
                    pages = select_pages(o["range"], o["mode"], n)
                    if not pages:
                        errors.append(f"{os.path.basename(path)}: 対象ページがありません（全{n}ページ）")
                        continue
                    plan.append((path, n, pages))
                except Exception as e:
                    errors.append(f"{os.path.basename(path)}: {e}")

            total = sum(len(p) for _, _, p in plan)
            self.post(lambda: self.progress.config(maximum=max(total, 1), value=0))

            done = 0
            for path, n, pages in plan:
                base = os.path.splitext(os.path.basename(path))[0]
                if o["same_dir"]:
                    out = os.path.join(os.path.dirname(path), f"{base}_images")
                elif len(o["files"]) > 1:
                    out = os.path.join(o["out_dir"], f"{base}_images")
                else:
                    out = o["out_dir"]

                try:
                    digits = max(3, len(str(n)))
                    with fitz.open(path) as doc:
                        for pno in pages:
                            page = doc[pno - 1]
                            reason = skip_reason(page, o["skip_white"], o["skip_black"],
                                                 o["threshold"])
                            if reason:
                                skipped.append((base, pno, reason))
                            else:
                                pix = page.get_pixmap(dpi=o["dpi"], alpha=False)
                                os.makedirs(out, exist_ok=True)  # 保存する時に初めて作る
                                pix.save(os.path.join(out, f"{base}_{pno:0{digits}d}.{o['ext']}"))
                                saved += 1
                            done += 1
                            self.post(lambda v=done, b=base, k=pno: (
                                self.progress.config(value=v),
                                self.status.set(f"変換中: {b} ({k}ページ目)")
                            ))
                except Exception as e:
                    errors.append(f"{os.path.basename(path)}: {e}")
        finally:
            self.post(lambda: self.finish(saved, errors, skipped))

    def finish(self, saved, errors, skipped=()):
        self.running = False
        self.run_btn.config(state="normal")
        msg = f"{saved} 枚の画像を保存しました"
        if skipped:
            nw = sum(1 for _, _, r in skipped if r == "white")
            nb = len(skipped) - nw
            parts = []
            if nw:
                parts.append(f"白 {nw}")
            if nb:
                parts.append(f"黒 {nb}")
            msg += f"\n除外したページ: {len(skipped)} 枚（{'、'.join(parts)}）"
            # どのページを除外したか確認できるよう一覧を出す（多い場合は先頭だけ）
            shown = [f"  {b} の {p}ページ（{'白' if r == 'white' else '黒'}）"
                     for b, p, r in skipped[:30]]
            if len(skipped) > 30:
                shown.append(f"  …ほか {len(skipped) - 30} 枚")
            msg += "\n" + "\n".join(shown)
        self.status.set("完了: " + msg.split("\n")[0]
                        + (f"（除外 {len(skipped)} 枚）" if skipped else ""))
        if errors:
            messagebox.showwarning("完了（一部問題あり）", msg + "\n\n" + "\n".join(errors))
        else:
            messagebox.showinfo("完了", msg)


# --------------------------------------------------------------------
#  タブ2: 画像 → PDF
# --------------------------------------------------------------------
class ImageToPdfTab(FileListTab):
    EXTS = IMG_EXTS
    LIST_TITLE = "画像ファイル（上から順にPDFのページになります）"
    DIALOG_TITLE = "画像を選択"
    DIALOG_TYPES = [("画像ファイル", "*.png *.jpg *.jpeg *.bmp *.gif *.tif *.tiff *.webp"),
                    ("すべてのファイル", "*.*")]
    EMPTY_HINT = "画像を追加してください"

    SIZE_MODES = (
        ("画像のサイズに合わせる", "image"),
        ("A4 縦", "a4p"),
        ("A4 横", "a4l"),
        ("A4（画像の向きに合わせる）", "a4auto"),
    )

    def __init__(self, master):
        super().__init__(master)
        self.size_mode = tk.StringVar(value="image")
        self.dpi = tk.IntVar(value=200)
        self.margin = tk.DoubleVar(value=10.0)
        self.out_path = tk.StringVar()
        self._build_options()
        self._on_mode()
        self._build_run_area("PDFを作成")

    # ---------- UI ----------
    def _extra_buttons(self, parent):
        ttk.Separator(parent, orient="horizontal").pack(fill="x", pady=4)
        ttk.Button(parent, text="↑ 上へ", command=lambda: self.move(-1)).pack(fill="x", pady=2)
        ttk.Button(parent, text="↓ 下へ", command=lambda: self.move(1)).pack(fill="x", pady=2)
        ttk.Button(parent, text="名前順", command=self.sort_by_name).pack(fill="x", pady=2)

    def _build_options(self):
        pad = {"padx": 10, "pady": 5}

        f1 = ttk.LabelFrame(self, text="ページサイズ")
        f1.pack(fill="x", **pad)
        rf = ttk.Frame(f1)
        rf.grid(row=0, column=0, columnspan=4, sticky="w", padx=5, pady=3)
        for text, val in self.SIZE_MODES:
            ttk.Radiobutton(rf, text=text, value=val, variable=self.size_mode,
                            command=self._on_mode).pack(side="left", padx=(0, 12))

        ttk.Label(f1, text="解像度(DPI):").grid(row=1, column=0, padx=5, pady=5, sticky="e")
        self.dpi_box = ttk.Combobox(
            f1, textvariable=self.dpi, values=[72, 100, 150, 200, 300, 400, 600], width=8)
        self.dpi_box.grid(row=1, column=1, padx=5, sticky="w")
        ttk.Label(f1, text="余白(mm):").grid(row=1, column=2, padx=(20, 5), sticky="e")
        self.margin_box = ttk.Entry(f1, textvariable=self.margin, width=8)
        self.margin_box.grid(row=1, column=3, padx=5, sticky="w")
        ttk.Label(f1, foreground="gray",
                  text="DPI: 「画像のサイズに合わせる」のときだけ有効（PDF→画像と同じDPIなら元の大きさに戻ります）\n"
                       "余白: A4のときだけ有効（画像は縦横比を保って余白の内側に収まります）"
                  ).grid(row=2, column=0, columnspan=4, sticky="w", padx=5, pady=(0, 5))

        f2 = ttk.LabelFrame(self, text="出力するPDF")
        f2.pack(fill="x", **pad)
        ttk.Entry(f2, textvariable=self.out_path).grid(
            row=0, column=0, sticky="ew", padx=5, pady=5)
        ttk.Button(f2, text="参照...", command=self.choose_out).grid(
            row=0, column=1, padx=5, pady=5)
        ttk.Label(f2, text="空欄のままなら、作成時に保存先を聞きます",
                  foreground="gray").grid(row=1, column=0, columnspan=2, sticky="w", padx=5)
        f2.columnconfigure(0, weight=1)

    def _on_mode(self):
        is_image = self.size_mode.get() == "image"
        self.dpi_box.config(state="normal" if is_image else "disabled")
        self.margin_box.config(state="disabled" if is_image else "normal")

    def choose_out(self):
        initdir = os.path.dirname(self.files[0]) if self.files else None
        p = filedialog.asksaveasfilename(
            title="PDFの保存先", defaultextension=".pdf",
            filetypes=[("PDFファイル", "*.pdf")], initialdir=initdir,
            initialfile="images.pdf")
        if p:
            self.out_path.set(p)

    # ---------- 並べ替え ----------
    def _refresh_list(self, select=()):
        self.listbox.delete(0, "end")
        for p in self.files:
            self.listbox.insert("end", p)
        for i in select:
            self.listbox.selection_set(i)

    def move(self, delta):
        sel = list(self.listbox.curselection())
        if not sel:
            return
        if delta < 0:
            if sel[0] == 0:
                return
            order = sel
        else:
            if sel[-1] == len(self.files) - 1:
                return
            order = list(reversed(sel))
        for i in order:
            j = i + delta
            self.files[i], self.files[j] = self.files[j], self.files[i]
        self._refresh_list([i + delta for i in sel])
        self.listbox.see(sel[0] + delta)

    def sort_by_name(self):
        self.files.sort(key=lambda p: natural_key(os.path.basename(p)))
        self._refresh_list()

    # ---------- 変換 ----------
    def start(self):
        if self.running:
            return
        if not self.files:
            messagebox.showwarning("確認", "画像を追加してください")
            return

        mode = self.size_mode.get()
        dpi, margin = 200, 0.0
        try:
            if mode == "image":
                dpi = int(self.dpi.get())
                if not 10 <= dpi <= 1200:
                    raise ValueError
            else:
                margin = float(self.margin.get())
                if not 0 <= margin <= 50:
                    raise ValueError
        except (ValueError, tk.TclError):
            if mode == "image":
                messagebox.showwarning("確認", "DPIは10〜1200の数値で指定してください")
            else:
                messagebox.showwarning("確認", "余白は0〜50(mm)の数値で指定してください")
            return

        out = self.out_path.get().strip()
        if not out:
            out = filedialog.asksaveasfilename(
                title="PDFの保存先", defaultextension=".pdf",
                filetypes=[("PDFファイル", "*.pdf")],
                initialdir=os.path.dirname(self.files[0]), initialfile="images.pdf")
            if not out:
                return  # キャンセル (上書き確認は保存ダイアログが行う)
        else:
            if not out.lower().endswith(".pdf"):
                out += ".pdf"
            d = os.path.dirname(out)
            if d and not os.path.isdir(d):
                messagebox.showwarning("確認", f"保存先のフォルダが存在しません:\n{d}")
                return
            if os.path.exists(out) and not messagebox.askyesno(
                    "確認", f"{os.path.basename(out)} は既に存在します。上書きしますか？"):
                return
        self.out_path.set(out)

        opts = {"files": list(self.files), "out": out, "mode": mode,
                "dpi": dpi, "margin": margin}
        self.running = True
        self.run_btn.config(state="disabled")
        threading.Thread(target=self.convert, args=(opts,), daemon=True).start()

    def convert(self, o):
        total = len(o["files"])
        self.post(lambda: self.progress.config(maximum=max(total, 1), value=0))

        def on_progress(i, path):
            self.post(lambda: (
                self.progress.config(value=i),
                self.status.set(f"処理中: {os.path.basename(path)} ({i}/{total})")))

        try:
            added, errors = images_to_pdf(
                o["files"], o["out"], o["mode"], o["dpi"], o["margin"], on_progress)
        except Exception as e:
            added, errors = 0, [str(e)]
        self.post(lambda: self.finish(added, errors, o["out"]))

    def finish(self, added, errors, out):
        self.running = False
        self.run_btn.config(state="normal")
        if added:
            self.status.set(f"完了: {added} ページのPDFを保存しました")
            if errors:
                messagebox.showwarning(
                    "完了（一部問題あり）",
                    f"{added} ページを保存しました。\n{out}\n\n読み込めなかった画像:\n" + "\n".join(errors))
            else:
                messagebox.showinfo("完了", f"{added} ページのPDFを保存しました。\n{out}")
        else:
            self.status.set("PDFを作成できませんでした")
            messagebox.showerror("エラー", "PDFを作成できませんでした。\n\n" + "\n".join(errors))


# --------------------------------------------------------------------
#  アプリ本体
# --------------------------------------------------------------------
class App(BaseTk):
    def __init__(self):
        super().__init__()
        self.title("PDF ⇄ 画像変換")
        self.geometry("760x780")
        self.minsize(700, 740)
        self._set_icon()

        self.nb = ttk.Notebook(self)
        self.nb.pack(fill="both", expand=True, padx=6, pady=6)
        self.tabs = [PdfToImageTab(self.nb), ImageToPdfTab(self.nb)]
        self.nb.add(self.tabs[0], text="  PDF → 画像  ")
        self.nb.add(self.tabs[1], text="  画像 → PDF  ")

        if HAS_DND:
            self._register_dnd(self)

    # ---------- アイコン ----------
    def _set_icon(self):
        # PyInstallerで固めた場合(_MEIPASS)にも対応
        base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
        try:
            if sys.platform.startswith("win"):
                self.iconbitmap(os.path.join(base, "pdf2img_icon.ico"))
            else:
                img = tk.PhotoImage(file=os.path.join(base, "pdf2img_icon.png"))
                self.iconphoto(True, img)
                self._icon_ref = img  # GC対策
        except Exception:
            pass  # アイコンが無くても動作は継続

    # ---------- ドラッグ&ドロップ ----------
    def _register_dnd(self, widget):
        try:
            widget.drop_target_register(DND_FILES)
            widget.dnd_bind("<<Drop>>", self.on_drop)
        except Exception:
            pass
        for child in widget.winfo_children():
            self._register_dnd(child)

    def on_drop(self, event):
        """今開いているタブで扱えるファイルならそのタブに追加。
        扱えず、別のタブで扱えるファイルなら、そのタブに切り替えて追加する。"""
        paths = self.tk.splitlist(event.data)
        cur = self.nb.index(self.nb.select())
        order = [cur] + [i for i in range(len(self.tabs)) if i != cur]
        for i in order:
            if self.tabs[i].collect(paths):
                if i != cur:
                    self.nb.select(i)
                self.tabs[i].add_paths(paths)
                return event.action
        self.tabs[cur].add_paths(paths)  # 全部対象外 → その旨をステータスに表示
        return event.action


if __name__ == "__main__":
    App().mainloop()
