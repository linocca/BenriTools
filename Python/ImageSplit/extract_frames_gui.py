#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
extract_frames_gui.py

extract_frames.py のGUI版。動画から静止画(JPG)を抜き出すツール。
ffmpeg / ffprobe を使用（PATHが通っている必要あり。通ってなければ
下の FFMPEG_PATH / FFPROBE_PATH をフルパスに書き換えてください）。
"""

import os
import subprocess
import sys
import threading
import tkinter as tk
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


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("動画フレーム抽出ツール")
        self.geometry("640x560")
        self.resizable(False, False)
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
        frm_log = ttk.LabelFrame(self, text="ログ")
        frm_log.pack(fill="both", expand=True, **pad)
        self.log_text = tk.Text(frm_log, height=14, state="disabled", wrap="word")
        self.log_text.pack(fill="both", expand=True, padx=6, pady=6)

    def log(self, msg: str):
        def _append():
            self.log_text.configure(state="normal")
            self.log_text.insert("end", msg + "\n")
            self.log_text.see("end")
            self.log_text.configure(state="disabled")
        self.after(0, _append)

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


if __name__ == "__main__":
    app = App()
    app.mainloop()
