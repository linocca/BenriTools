#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
extract_frames_gui.py

extract_frames.py のGUI版。タブで2つの機能を切り替えます。
  ・動画→画像 : 動画から静止画(JPG)を抜き出す
  ・画像→動画 : フォルダ内の連番画像をつなげて動画(mp4)にする
ffmpeg / ffprobe を使用（PATHが通っている必要あり。通ってなければ
下の FFMPEG_PATH / FFPROBE_PATH をフルパスに書き換えてください）。
"""

import os
import re
import subprocess
import sys
import tempfile
import threading
import tkinter as tk
from fractions import Fraction
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

# PATHが通ってない場合はここをフルパスに書き換える（例: r"C:\ffmpeg\bin\ffmpeg.exe"）
FFMPEG_PATH = "ffmpeg"
FFPROBE_PATH = "ffprobe"

# Windows(cp932)環境でffmpeg/ffprobeがUTF-8以外のバイト列を吐いた際の
# UnicodeDecodeError（デコード失敗）を避けるため、明示的にUTF-8＋置換を指定する。
_RUN_KW = dict(capture_output=True, text=True, encoding="utf-8", errors="replace")


class ExtractError(Exception):
    pass


def get_duration(video_path: Path) -> float:
    cmd = [
        FFPROBE_PATH, "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(video_path),
    ]
    try:
        result = subprocess.run(cmd, **_RUN_KW)
    except FileNotFoundError:
        raise ExtractError("ffprobeが見つかりません。PATHを確認するか、\nスクリプト先頭のFFPROBE_PATHを書き換えてください。")
    if result.returncode != 0:
        raise ExtractError(f"ffprobeに失敗しました:\n{result.stderr}")
    try:
        return float(result.stdout.strip())
    except ValueError:
        raise ExtractError("動画の長さを取得できませんでした（対応していない形式かもしれません）。")


def resolve_range(video_path: Path, start, end):
    duration = get_duration(video_path)
    s = 0.0 if start is None else max(0.0, start)
    e = duration if end is None else min(duration, end)
    if s >= e:
        raise ExtractError(
            f"指定範囲が不正です（開始={s:.2f}s, 終了={e:.2f}s, 動画長={duration:.2f}s）。"
            "開始は終了より前にしてください。"
        )
    return s, e


def extract_by_count(video_path, outdir, count, quality, start, end, log):
    range_start, range_end = resolve_range(video_path, start, end)
    span = range_end - range_start
    saved = []
    for i in range(count):
        t = range_start + span * (i + 0.5) / count
        out = outdir / f"frame_{i + 1:03d}.jpg"
        cmd = [FFMPEG_PATH, "-y", "-ss", f"{t:.3f}", "-i", str(video_path),
               "-frames:v", "1", "-q:v", str(quality), str(out)]
        try:
            result = subprocess.run(cmd, **_RUN_KW)
        except FileNotFoundError:
            raise ExtractError("ffmpegが見つかりません。PATHを確認するか、\nスクリプト先頭のFFMPEG_PATHを書き換えてください。")
        if result.returncode != 0:
            log(f"  警告: {t:.2f}s のフレーム抽出に失敗しました")
            continue
        saved.append(out)
        log(f"  抽出: {out.name}  (t={t:.2f}s)")
    return saved


def extract_by_rate(video_path, outdir, fps_expr, quality, start, end, log):
    pattern = outdir / "frame_%04d.jpg"
    cmd = [FFMPEG_PATH, "-y"]
    if start is not None:
        cmd += ["-ss", str(start)]
    cmd += ["-i", str(video_path)]
    if end is not None:
        duration = end - (start if start is not None else 0.0)
        cmd += ["-t", str(max(0.0, duration))]
    cmd += ["-vf", f"fps={fps_expr}", "-q:v", str(quality), str(pattern)]
    try:
        result = subprocess.run(cmd, **_RUN_KW)
    except FileNotFoundError:
        raise ExtractError("ffmpegが見つかりません。PATHを確認するか、\nスクリプト先頭のFFMPEG_PATHを書き換えてください。")
    if result.returncode != 0:
        raise ExtractError(f"ffmpegに失敗しました:\n{result.stderr}")
    saved = sorted(outdir.glob("frame_*.jpg"))
    for p in saved:
        log(f"  抽出: {p.name}")
    return saved


def extract_all(video_path, outdir, quality, start, end, log):
    pattern = outdir / "frame_%05d.jpg"
    cmd = [FFMPEG_PATH, "-y"]
    if start is not None:
        cmd += ["-ss", str(start)]
    cmd += ["-i", str(video_path)]
    if end is not None:
        duration = end - (start if start is not None else 0.0)
        cmd += ["-t", str(max(0.0, duration))]
    cmd += ["-vsync", "0", "-q:v", str(quality), str(pattern)]
    try:
        result = subprocess.run(cmd, **_RUN_KW)
    except FileNotFoundError:
        raise ExtractError("ffmpegが見つかりません。PATHを確認するか、\nスクリプト先頭のFFMPEG_PATHを書き換えてください。")
    if result.returncode != 0:
        raise ExtractError(f"ffmpegに失敗しました:\n{result.stderr}")
    saved = sorted(outdir.glob("frame_*.jpg"))
    for p in saved:
        log(f"  抽出: {p.name}")
    return saved


# ----------------------------------------------------------------------
# 画像 → 動画
# ----------------------------------------------------------------------
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".webp"}

# 29.97 などの入力を、ffmpegに渡す正確な分数(30000/1001など)に直す
_FPS_SNAP = {
    23.976: "24000/1001",
    29.97: "30000/1001",
    47.952: "48000/1001",
    59.94: "60000/1001",
    119.88: "120000/1001",
}


def _natural_key(p: Path):
    # frame_2.jpg が frame_10.jpg より前に来るように、数字部分を数値として比較する
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", p.name)]


def list_images(folder: Path):
    files = [p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_EXTS]
    return sorted(files, key=_natural_key)


def parse_fps(text: str) -> Fraction:
    s = text.strip()
    try:
        val = float(s)
    except ValueError:
        raise ExtractError(f"フレームレートには数値を入力してください（入力値: {s!r}）")
    if val <= 0:
        raise ExtractError("フレームレートは0より大きくしてください。")
    for approx, expr in _FPS_SNAP.items():
        if abs(val - approx) < 0.005:
            return Fraction(expr)
    return Fraction(s)


def get_image_size(path: Path):
    cmd = [
        FFPROBE_PATH, "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height",
        "-of", "csv=s=x:p=0", str(path),
    ]
    try:
        result = subprocess.run(cmd, **_RUN_KW)
    except FileNotFoundError:
        raise ExtractError("ffprobeが見つかりません。PATHを確認するか、\nスクリプト先頭のFFPROBE_PATHを書き換えてください。")
    try:
        w, h = result.stdout.strip().split("x")[:2]
        return int(w), int(h)
    except ValueError:
        raise ExtractError(f"画像サイズを取得できませんでした: {path.name}\n{result.stderr}")


# 拡張子 → ffmpegに渡すデコーダ名（画像をパイプで流し込むため種類を指定する必要がある）
_IMAGE_CODEC = {
    ".jpg": "mjpeg", ".jpeg": "mjpeg", ".png": "png", ".bmp": "bmp",
    ".tif": "tiff", ".tiff": "tiff", ".webp": "webp",
}


def images_to_video(images, out_path: Path, fps_text, crf, log):
    fr = parse_fps(fps_text)
    fps_expr = f"{fr.numerator}/{fr.denominator}"
    codecs = {_IMAGE_CODEC[p.suffix.lower()] for p in images}
    if len(codecs) != 1:
        raise ExtractError("フォルダ内に種類の違う画像（jpgとpngなど）が混在しています。\n"
                           "同じ種類の画像だけにしてください。")
    codec = codecs.pop()
    w, h = get_image_size(images[0])
    w -= w % 2   # H.264は縦横とも偶数が必要
    h -= h % 2
    if w <= 0 or h <= 0:
        raise ExtractError("画像サイズが小さすぎます。")
    log(f"画像  : {len(images)}枚  ({images[0].name} 〜 {images[-1].name})")
    log(f"動画  : {w}x{h}  {float(fr):.3f}fps  CRF={crf}")
    log(f"長さ  : 約{len(images) / float(fr):.2f}秒")

    # 画像ファイルを順番にffmpegの標準入力へ流し込む（時間は -framerate で1枚=1コマに固定）。
    # サイズの違う画像が混ざっていても、1枚目のサイズに収める（余白は黒）
    vf = (f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
          f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1,format=yuv420p")
    cmd = [FFMPEG_PATH, "-y", "-v", "error",
           "-f", "image2pipe", "-framerate", fps_expr, "-c:v", codec, "-i", "-",
           "-vf", vf, "-c:v", "libx264", "-crf", str(crf), "-preset", "medium",
           "-pix_fmt", "yuv420p"]
    if out_path.suffix.lower() in (".mp4", ".mov", ".m4v"):
        cmd += ["-movflags", "+faststart"]
    cmd.append(str(out_path))

    log("ffmpeg実行中...")
    err = tempfile.TemporaryFile()
    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=err)
    except FileNotFoundError:
        err.close()
        raise ExtractError("ffmpegが見つかりません。PATHを確認するか、\nスクリプト先頭のFFMPEG_PATHを書き換えてください。")
    try:
        try:
            for i, img in enumerate(images, 1):
                proc.stdin.write(img.read_bytes())
                if i % 100 == 0:
                    log(f"  {i}/{len(images)}枚")
        except (BrokenPipeError, OSError):
            pass   # ffmpegが途中で終了した（原因は下のstderrで報告）
        finally:
            try:
                proc.stdin.close()
            except OSError:
                pass
        rc = proc.wait()
        if rc != 0:
            err.seek(0)
            msg = err.read().decode("utf-8", errors="replace")[-1500:]
            raise ExtractError(f"ffmpegに失敗しました:\n{msg}")
    finally:
        err.close()
    return out_path


class BaseTab(ttk.Frame):
    """各タブ共通：ログ欄とログ出力。"""

    def _build_log(self):
        frm_log = ttk.LabelFrame(self, text="ログ")
        frm_log.pack(fill="both", expand=True, padx=8, pady=4)
        self.log_text = tk.Text(frm_log, height=14, state="disabled", wrap="word")
        self.log_text.pack(fill="both", expand=True, padx=6, pady=6)

    def log(self, msg: str):
        def _append():
            self.log_text.configure(state="normal")
            self.log_text.insert("end", msg + "\n")
            self.log_text.see("end")
            self.log_text.configure(state="disabled")
        self.after(0, _append)


class ExtractTab(BaseTab):
    """動画 → 画像"""

    def __init__(self, master):
        super().__init__(master)
        self._build_ui()

    def _build_ui(self):
        pad = {"padx": 8, "pady": 4}

        # --- 動画ファイル ---
        frm_video = ttk.LabelFrame(self, text="① 動画ファイル")
        frm_video.pack(fill="x", **pad)
        self.video_var = tk.StringVar()
        ttk.Entry(frm_video, textvariable=self.video_var, width=60).pack(side="left", padx=6, pady=6)
        ttk.Button(frm_video, text="参照...", command=self.browse_video).pack(side="left", padx=4)

        # --- 出力先 ---
        frm_out = ttk.LabelFrame(self, text="② 出力フォルダ")
        frm_out.pack(fill="x", **pad)
        self.outdir_var = tk.StringVar()
        ttk.Entry(frm_out, textvariable=self.outdir_var, width=60).pack(side="left", padx=6, pady=6)
        ttk.Button(frm_out, text="参照...", command=self.browse_outdir).pack(side="left", padx=4)

        # --- 範囲指定 ---
        frm_range = ttk.LabelFrame(self, text="③ 抽出範囲（秒）※空欄なら動画の先頭/末尾")
        frm_range.pack(fill="x", **pad)
        ttk.Label(frm_range, text="開始秒:").pack(side="left", padx=(8, 2))
        self.start_var = tk.StringVar()
        ttk.Entry(frm_range, textvariable=self.start_var, width=10).pack(side="left")
        ttk.Label(frm_range, text="終了秒:").pack(side="left", padx=(16, 2))
        self.end_var = tk.StringVar()
        ttk.Entry(frm_range, textvariable=self.end_var, width=10).pack(side="left")

        # --- モード ---
        frm_mode = ttk.LabelFrame(self, text="④ 抽出モード")
        frm_mode.pack(fill="x", **pad)
        self.mode_var = tk.StringVar(value="count")

        row1 = ttk.Frame(frm_mode)
        row1.pack(fill="x", pady=2)
        ttk.Radiobutton(row1, text="範囲内から均等に", variable=self.mode_var, value="count",
                        width=16).pack(side="left")
        self.count_var = tk.StringVar(value="10")
        ttk.Entry(row1, textvariable=self.count_var, width=8).pack(side="left")
        ttk.Label(row1, text="枚").pack(side="left")

        row2 = ttk.Frame(frm_mode)
        row2.pack(fill="x", pady=2)
        ttk.Radiobutton(row2, text="この秒数ごとに1枚", variable=self.mode_var, value="interval",
                        width=16).pack(side="left")
        self.interval_var = tk.StringVar(value="2")
        ttk.Entry(row2, textvariable=self.interval_var, width=8).pack(side="left")
        ttk.Label(row2, text="秒").pack(side="left")

        row3 = ttk.Frame(frm_mode)
        row3.pack(fill="x", pady=2)
        ttk.Radiobutton(row3, text="1秒あたりこの枚数", variable=self.mode_var, value="fps",
                        width=16).pack(side="left")
        self.fps_var = tk.StringVar(value="1")
        ttk.Entry(row3, textvariable=self.fps_var, width=8).pack(side="left")
        ttk.Label(row3, text="枚/秒").pack(side="left")

        row4 = ttk.Frame(frm_mode)
        row4.pack(fill="x", pady=2)
        ttk.Radiobutton(row4, text="範囲内の全フレーム（間引きなし・枚数注意）",
                        variable=self.mode_var, value="all").pack(side="left")

        # --- 画質 ---
        frm_q = ttk.LabelFrame(self, text="⑤ JPG画質（2=最高品質 〜 31=最低品質）")
        frm_q.pack(fill="x", **pad)
        self.quality_var = tk.IntVar(value=2)
        ttk.Spinbox(frm_q, from_=2, to=31, textvariable=self.quality_var, width=8).pack(
            side="left", padx=8, pady=6)

        # --- 実行ボタン ---
        self.run_btn = ttk.Button(self, text="抽出開始", command=self.on_run)
        self.run_btn.pack(pady=8)

        # --- ログ表示 ---
        self._build_log()

    def browse_video(self):
        path = filedialog.askopenfilename(
            title="動画ファイルを選択",
            filetypes=[("動画ファイル", "*.mp4 *.mov *.avi *.mkv *.wmv *.m4v"), ("すべてのファイル", "*.*")],
        )
        if path:
            self.video_var.set(path)
            if not self.outdir_var.get():
                p = Path(path)
                self.outdir_var.set(str(p.parent / f"frames_{p.stem}"))

    def browse_outdir(self):
        path = filedialog.askdirectory(title="出力フォルダを選択")
        if path:
            self.outdir_var.set(path)

    def _parse_float_or_none(self, s: str, field_name: str):
        s = s.strip()
        if not s:
            return None
        try:
            return float(s)
        except ValueError:
            raise ExtractError(f"{field_name}には数値を入力してください（入力値: {s!r}）")

    def on_run(self):
        video_str = self.video_var.get().strip()
        outdir_str = self.outdir_var.get().strip()
        if not video_str:
            messagebox.showerror("エラー", "動画ファイルを選択してください。")
            return
        video_path = Path(video_str)
        if not video_path.is_file():
            messagebox.showerror("エラー", f"動画が見つかりません:\n{video_path}")
            return
        if not outdir_str:
            messagebox.showerror("エラー", "出力フォルダを指定してください。")
            return
        outdir = Path(outdir_str)

        try:
            start = self._parse_float_or_none(self.start_var.get(), "開始秒")
            end = self._parse_float_or_none(self.end_var.get(), "終了秒")
            if start is not None and end is not None and start >= end:
                raise ExtractError(f"開始秒({start})は終了秒({end})より前にしてください。")
            quality = int(self.quality_var.get())
            mode = self.mode_var.get()
            if mode == "count":
                count = int(self.count_var.get())
                if count <= 0:
                    raise ExtractError("枚数は1以上にしてください。")
            elif mode == "interval":
                interval = float(self.interval_var.get())
                if interval <= 0:
                    raise ExtractError("秒数は0より大きくしてください。")
            elif mode == "fps":
                fps = float(self.fps_var.get())
                if fps <= 0:
                    raise ExtractError("枚/秒は0より大きくしてください。")
        except ExtractError as e:
            messagebox.showerror("入力エラー", str(e))
            return
        except ValueError:
            messagebox.showerror("入力エラー", "数値の入力に誤りがあります。")
            return

        outdir.mkdir(parents=True, exist_ok=True)

        self.run_btn.configure(state="disabled")
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

        self.log(f"入力  : {video_path}")
        self.log(f"出力先: {outdir.resolve()}")
        if start is not None or end is not None:
            s = "先頭" if start is None else f"{start}秒"
            e = "末尾" if end is None else f"{end}秒"
            self.log(f"範囲  : {s} 〜 {e}")

        def worker():
            try:
                if mode == "all":
                    self.log("モード: 全フレームを抽出（間引きなし）")
                    saved = extract_all(video_path, outdir, quality, start, end, self.log)
                elif mode == "interval":
                    self.log(f"モード: {interval}秒ごとに1枚")
                    saved = extract_by_rate(video_path, outdir, f"1/{interval}", quality,
                                             start, end, self.log)
                elif mode == "fps":
                    self.log(f"モード: 毎秒{fps}枚")
                    saved = extract_by_rate(video_path, outdir, str(fps), quality,
                                             start, end, self.log)
                else:
                    self.log(f"モード: 範囲内から{count}枚を均等抽出")
                    saved = extract_by_count(video_path, outdir, count, quality,
                                              start, end, self.log)
                self.log(f"\n完了: {len(saved)}枚を保存しました → {outdir.resolve()}")
                self.after(0, lambda: messagebox.showinfo(
                    "完了", f"{len(saved)}枚を保存しました。\n\n{outdir.resolve()}"))
            except ExtractError as e:
                self.log(f"エラー: {e}")
                self.after(0, lambda: messagebox.showerror("エラー", str(e)))
            finally:
                self.after(0, lambda: self.run_btn.configure(state="normal"))

        threading.Thread(target=worker, daemon=True).start()


class ImagesToVideoTab(BaseTab):
    """画像 → 動画"""

    FPS_CHOICES = ["24", "23.976", "25", "29.97", "30", "50", "59.94", "60"]
    CUSTOM_FPS = "指定fps"

    def __init__(self, master):
        super().__init__(master)
        self._last_auto_out = ""
        self._build_ui()

    def _build_ui(self):
        pad = {"padx": 8, "pady": 4}

        # --- 画像フォルダ ---
        frm_in = ttk.LabelFrame(self, text="① 画像フォルダ（名前順＝連番順につなぎます）")
        frm_in.pack(fill="x", **pad)
        self.indir_var = tk.StringVar()
        ttk.Entry(frm_in, textvariable=self.indir_var, width=60).pack(side="left", padx=6, pady=6)
        ttk.Button(frm_in, text="参照...", command=self.browse_indir).pack(side="left", padx=4)
        self.count_label = ttk.Label(self, text="")
        self.count_label.pack(anchor="w", padx=14)

        # --- 出力動画 ---
        frm_out = ttk.LabelFrame(self, text="② 出力する動画ファイル")
        frm_out.pack(fill="x", **pad)
        self.outfile_var = tk.StringVar()
        ttk.Entry(frm_out, textvariable=self.outfile_var, width=60).pack(side="left", padx=6, pady=6)
        ttk.Button(frm_out, text="参照...", command=self.browse_outfile).pack(side="left", padx=4)

        # --- フレームレート ---
        frm_fps = ttk.LabelFrame(self, text="③ フレームレート（1秒あたりの枚数）")
        frm_fps.pack(fill="x", **pad)
        self.fps_var = tk.StringVar(value="30")
        self.fps_combo = ttk.Combobox(frm_fps, textvariable=self.fps_var, state="readonly",
                                      values=self.FPS_CHOICES + [self.CUSTOM_FPS], width=10)
        self.fps_combo.pack(side="left", padx=8, pady=6)
        self.fps_combo.bind("<<ComboboxSelected>>", self._on_fps_selected)
        ttk.Label(frm_fps, text="fps　　指定fps:").pack(side="left")
        self.custom_fps_var = tk.StringVar(value="12")
        self.custom_fps_entry = ttk.Entry(frm_fps, textvariable=self.custom_fps_var,
                                          width=8, state="disabled")
        self.custom_fps_entry.pack(side="left", padx=4)
        ttk.Label(frm_fps, text="fps（小数可。例: 12 / 5 / 0.5）").pack(side="left")

        # --- 画質 ---
        frm_q = ttk.LabelFrame(self, text="④ 画質 CRF（0=ほぼ劣化なし 〜 51=最低画質／18前後が高画質）")
        frm_q.pack(fill="x", **pad)
        self.crf_var = tk.IntVar(value=18)
        ttk.Spinbox(frm_q, from_=0, to=51, textvariable=self.crf_var, width=8).pack(
            side="left", padx=8, pady=6)

        # --- 実行ボタン ---
        self.run_btn = ttk.Button(self, text="動画を作成", command=self.on_run)
        self.run_btn.pack(pady=8)

        self._build_log()

    def _on_fps_selected(self, _event=None):
        # 「指定fps」を選んだときだけ、自分でfpsを入力できるようにする
        if self.fps_var.get() == self.CUSTOM_FPS:
            self.custom_fps_entry.configure(state="normal")
            self.custom_fps_entry.focus_set()
            self.custom_fps_entry.selection_range(0, "end")
        else:
            self.custom_fps_entry.configure(state="disabled")

    def _fps_text(self) -> str:
        if self.fps_var.get() == self.CUSTOM_FPS:
            return self.custom_fps_var.get()
        return self.fps_var.get()

    def browse_indir(self):
        path = filedialog.askdirectory(title="画像フォルダを選択")
        if not path:
            return
        self.indir_var.set(path)
        folder = Path(path)
        try:
            n = len(list_images(folder))
        except OSError:
            n = 0
        self.count_label.configure(text=f"　画像: {n}枚" if n else "　画像が見つかりません")
        auto_out = str(folder.parent / f"{folder.name}.mp4")
        cur = self.outfile_var.get()
        if not cur or cur == self._last_auto_out:
            self.outfile_var.set(auto_out)
            self._last_auto_out = auto_out

    def browse_outfile(self):
        path = filedialog.asksaveasfilename(
            title="出力する動画ファイル",
            defaultextension=".mp4",
            filetypes=[("MP4動画", "*.mp4"), ("MKV動画", "*.mkv"), ("すべてのファイル", "*.*")],
        )
        if path:
            self.outfile_var.set(path)

    def on_run(self):
        indir_str = self.indir_var.get().strip()
        out_str = self.outfile_var.get().strip()
        if not indir_str:
            messagebox.showerror("エラー", "画像フォルダを選択してください。")
            return
        indir = Path(indir_str)
        if not indir.is_dir():
            messagebox.showerror("エラー", f"フォルダが見つかりません:\n{indir}")
            return
        if not out_str:
            messagebox.showerror("エラー", "出力する動画ファイルを指定してください。")
            return
        out_path = Path(out_str)
        if not out_path.suffix:
            out_path = out_path.with_suffix(".mp4")

        try:
            images = list_images(indir)
            if not images:
                raise ExtractError("フォルダ内に画像（jpg/png/bmp/tif/webp）が見つかりません。")
            parse_fps(self._fps_text())
            crf = int(self.crf_var.get())
            if not 0 <= crf <= 51:
                raise ExtractError("CRFは0〜51の範囲で指定してください。")
        except ExtractError as e:
            messagebox.showerror("入力エラー", str(e))
            return
        except (ValueError, tk.TclError):
            messagebox.showerror("入力エラー", "数値の入力に誤りがあります。")
            return

        if out_path.exists() and not messagebox.askyesno(
                "確認", f"既に存在します。上書きしますか？\n\n{out_path}"):
            return
        out_path.parent.mkdir(parents=True, exist_ok=True)

        fps_text = self._fps_text()
        self.run_btn.configure(state="disabled")
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")
        self.log(f"入力  : {indir}")
        self.log(f"出力  : {out_path}")

        def worker():
            try:
                images_to_video(images, out_path, fps_text, crf, self.log)
                size_mb = out_path.stat().st_size / (1024 * 1024)
                self.log(f"\n完了: {out_path}  ({size_mb:.1f}MB)")
                self.after(0, lambda: messagebox.showinfo(
                    "完了", f"動画を作成しました。\n\n{out_path}"))
            except ExtractError as e:
                self.log(f"エラー: {e}")
                self.after(0, lambda: messagebox.showerror("エラー", str(e)))
            finally:
                self.after(0, lambda: self.run_btn.configure(state="normal"))

        threading.Thread(target=worker, daemon=True).start()


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("フレーム画像ツール（動画⇄画像）")
        self.geometry("660x680")
        self.resizable(False, False)

        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=4, pady=4)
        nb.add(ExtractTab(nb), text="  動画 → 画像  ")
        nb.add(ImagesToVideoTab(nb), text="  画像 → 動画  ")


if __name__ == "__main__":
    app = App()
    app.mainloop()
