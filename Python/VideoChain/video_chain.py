#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
VideoChain - フォルダ内の動画をまとめて1本に連結する GUI ツール

必要なもの:
    - Python 3.8+ (tkinter 同梱)
    - ffmpeg / ffprobe に PATH が通っていること
      (通っていない場合は下の FFMPEG / FFPROBE にフルパスを書く)

使い方:
    python video_chain.py
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import queue
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

# ---------------------------------------------------------------------------
# 設定
# ---------------------------------------------------------------------------

FFMPEG = "ffmpeg"    # 例: r"C:\ffmpeg\bin\ffmpeg.exe"
FFPROBE = "ffprobe"  # 例: r"C:\ffmpeg\bin\ffprobe.exe"

SHOW_CONSOLE = False  # True=別途コンソール窓にも ffmpeg の生の出力を表示する


# ---------------------------------------------------------------------------
# コンソール
# ---------------------------------------------------------------------------

_console_lock = threading.Lock()
_console_inline_len = 0   # 直前に上書き表示した進捗行の長さ（0=通常行の後）


def setup_console():
    """コンソール窓を用意する（pythonw 起動などで無い場合は新規に開く）。"""
    if not SHOW_CONSOLE or os.name != "nt":
        return
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        if not k32.GetConsoleWindow():
            k32.AllocConsole()
            sys.stdout = open("CONOUT$", "w", encoding="utf-8",
                              errors="replace", buffering=1)
            sys.stderr = sys.stdout
        k32.SetConsoleTitleW("VideoChain - ffmpeg 出力")
    except Exception:
        pass


def console_write(line, inline=False):
    """コンソールへ出力。inline=True は同じ行を上書きする（ffmpeg の進捗行用）。"""
    global _console_inline_len
    if not SHOW_CONSOLE or sys.stdout is None:
        return
    with _console_lock:
        try:
            if inline:
                pad = max(0, _console_inline_len - len(line))
                sys.stdout.write("\r" + line + " " * pad)
                _console_inline_len = len(line)
            else:
                if _console_inline_len:
                    sys.stdout.write("\n")
                sys.stdout.write(line + "\n")
                _console_inline_len = 0
            sys.stdout.flush()
        except Exception:
            pass

VIDEO_EXTS = {
    ".mp4", ".mov", ".avi", ".mkv", ".m4v", ".wmv", ".mts", ".m2ts",
    ".flv", ".webm", ".mpg", ".mpeg", ".ts", ".3gp", ".asf",
}

CFR_FPS_CHOICES = ["自動(最多fps)", "23.976", "24", "25", "29.97", "30", "50", "59.94", "60"]

RES_CHOICES = [
    "自動(最多)",
    "3840x2160 (4K)",
    "2560x1440 (WQHD)",
    "1920x1080 (FHD)",
    "1280x720 (HD)",
    "854x480 (SD)",
]

# 中途半端な実測fpsを丸める先（VFR素材の平均fps対策）
STANDARD_FPS = [23.976, 24.0, 25.0, 29.97, 30.0, 47.952, 48.0,
                50.0, 59.94, 60.0, 119.88, 120.0]

# ffmpeg に渡す際は有理数で指定して誤差をなくす
FPS_RATIONAL = {
    23.976: "24000/1001", 29.97: "30000/1001", 47.952: "48000/1001",
    59.94: "60000/1001", 119.88: "120000/1001",
}


def snap_fps(value, tol=0.03):
    """実測fpsを近い標準fpsに丸める。離れていればそのまま。"""
    if not value:
        return 30.0
    best = min(STANDARD_FPS, key=lambda s: abs(s - value))
    return best if abs(best - value) / value <= tol else round(value, 3)


def fps_arg(fps):
    """ffmpeg 用のfps文字列（29.97 → '30000/1001'）。"""
    return FPS_RATIONAL.get(round(fps, 3), str(fps))


def _creationflags():
    """Windowsでコンソールウィンドウを出さない。"""
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


_FPS_MODE_OK = None


def cfr_option():
    """ffmpeg のバージョンに応じた CFR 指定オプションを返す。

    ffmpeg 5.0 以降: -fps_mode cfr / それ以前: -vsync cfr
    """
    global _FPS_MODE_OK
    if _FPS_MODE_OK is None:
        _FPS_MODE_OK = False
        try:
            res = subprocess.run([FFMPEG, "-hide_banner", "-h", "full"],
                                 capture_output=True, text=True,
                                 encoding="utf-8", errors="replace",
                                 creationflags=_creationflags())
            _FPS_MODE_OK = "fps_mode" in (res.stdout or "")
        except Exception:
            pass
    return ["-fps_mode", "cfr"] if _FPS_MODE_OK else ["-vsync", "cfr"]


# ---------------------------------------------------------------------------
# エンコーダ
# ---------------------------------------------------------------------------

ENCODERS = [
    ("libx264", "CPU (libx264)"),
    ("h264_amf", "GPU AMD (h264_amf)"),
    ("h264_nvenc", "GPU NVIDIA (h264_nvenc)"),
    ("h264_qsv", "GPU Intel (h264_qsv)"),
]

X264_PRESETS = ["ultrafast", "superfast", "veryfast", "faster",
                "fast", "medium", "slow"]


def compiled_encoders():
    """ffmpeg に組み込まれている GPU エンコーダ名（ハードの有無は問わない）。"""
    try:
        res = subprocess.run([FFMPEG, "-hide_banner", "-encoders"],
                             capture_output=True, text=True,
                             encoding="utf-8", errors="replace",
                             creationflags=_creationflags())
        out = res.stdout or ""
    except Exception:
        return []
    return [name for name, _ in ENCODERS[1:] if name in out]


def hw_encoder_works(name):
    """黒一色の極小動画を実際にエンコードして、そのGPUが使えるか確かめる。"""
    cmd = [FFMPEG, "-hide_banner", "-nostdin", "-loglevel", "error",
           "-f", "lavfi", "-i", "color=c=black:s=256x256:r=30:d=0.2",
           "-frames:v", "3", "-c:v", name, "-f", "null", "-"]
    try:
        res = subprocess.run(cmd, capture_output=True, timeout=20,
                             creationflags=_creationflags())
        return res.returncode == 0
    except Exception:
        return False


def available_encoders():
    """このPCで実際に使える H.264 エンコーダ。

    戻り値: (使えるラベルのリスト, ffmpegは対応しているがPCで使えないラベルのリスト)
    """
    usable = ["CPU (libx264)"]
    unusable = []
    labels = dict(ENCODERS)
    for name in compiled_encoders():
        if hw_encoder_works(name):
            usable.append(labels[name])
        else:
            unusable.append(labels[name])
    return usable, unusable


def encoder_name(label):
    for name, lab in ENCODERS:
        if lab == label:
            return name
    return "libx264"


def video_opts(label, quality, preset):
    """選択されたエンコーダに応じた映像オプション。"""
    name = encoder_name(label)
    q = str(quality)
    if name == "h264_amf":
        return ["-c:v", "h264_amf", "-quality", "quality", "-rc", "cqp",
                "-qp_i", q, "-qp_p", q, "-qp_b", q]
    if name == "h264_nvenc":
        return ["-c:v", "h264_nvenc", "-preset", "p5", "-tune", "hq",
                "-rc", "constqp", "-qp", q]
    if name == "h264_qsv":
        return ["-c:v", "h264_qsv", "-preset", "medium", "-global_quality", q]
    return ["-c:v", "libx264", "-preset", preset, "-crf", q]


def natural_key(s):
    """1, 2, 10 の順に並ぶ自然順ソートキー。"""
    return [int(t) if t.isdigit() else t.lower()
            for t in re.split(r"(\d+)", s)]


def fmt_duration(sec):
    if sec is None:
        return "-"
    sec = int(round(sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def fmt_size(bytes_):
    """バイト数を MB / GB の読みやすい表記に。"""
    gb = bytes_ / (1024 ** 3)
    if gb >= 1:
        return f"{gb:.1f} GB"
    return f"{bytes_ / (1024 ** 2):.0f} MB"


def parse_rate(text):
    """'30000/1001' → 29.97"""
    if not text:
        return None
    try:
        if "/" in text:
            num, den = text.split("/")
            den = float(den)
            return float(num) / den if den else None
        return float(text)
    except (ValueError, ZeroDivisionError):
        return None


# ---------------------------------------------------------------------------
# ffprobe
# ---------------------------------------------------------------------------

def probe(path):
    """1ファイルのメタ情報を取得して dict で返す。"""
    cmd = [FFPROBE, "-v", "error", "-print_format", "json",
           "-show_format", "-show_streams", path]
    res = subprocess.run(cmd, capture_output=True, text=True,
                         encoding="utf-8", errors="replace",
                         creationflags=_creationflags())
    if res.returncode != 0:
        raise RuntimeError(res.stderr.strip() or "ffprobe に失敗しました")

    data = json.loads(res.stdout)
    vs = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
    as_ = next((s for s in data.get("streams", []) if s.get("codec_type") == "audio"), None)
    if vs is None:
        raise RuntimeError("映像ストリームが見つかりません")

    avg = parse_rate(vs.get("avg_frame_rate"))
    r = parse_rate(vs.get("r_frame_rate"))
    vfr = bool(avg and r and abs(r - avg) / avg > 0.01)
    interlaced = vs.get("field_order") in ("tt", "bb", "tb", "bt")

    sar = vs.get("sample_aspect_ratio") or "1:1"
    par = parse_rate(sar.replace(":", "/")) or 1.0
    anamorphic = abs(par - 1.0) > 0.01
    w, h = vs.get("width", 0), vs.get("height", 0)
    disp_w = int(round(w * par / 2)) * 2 if anamorphic else w

    dur = None
    for src in (data.get("format", {}), vs):
        try:
            dur = float(src.get("duration"))
            break
        except (TypeError, ValueError):
            continue

    return {
        "path": os.path.abspath(path),
        "name": os.path.basename(path),
        "vcodec": vs.get("codec_name", "?"),
        "width": vs.get("width", 0),
        "height": vs.get("height", 0),
        "pix_fmt": vs.get("pix_fmt", "?"),
        "fps": avg,
        "vfr": vfr,
        "interlaced": interlaced,
        "sar": sar,
        "anamorphic": anamorphic,
        "disp_w": disp_w,
        "has_audio": as_ is not None,
        "acodec": as_.get("codec_name", "?") if as_ else None,
        "sample_rate": as_.get("sample_rate") if as_ else None,
        "channels": as_.get("channels") if as_ else None,
        "duration": dur,
    }


# ---------------------------------------------------------------------------
# 互換性チェック
# ---------------------------------------------------------------------------

CHECK_KEYS = [
    ("vcodec", "映像コーデック"),
    ("res", "解像度"),
    ("pix_fmt", "ピクセルフォーマット"),
    ("fps_r", "フレームレート"),
    ("acodec", "音声コーデック"),
    ("sample_rate", "音声サンプリングレート"),
    ("channels", "音声チャンネル数"),
]

AUDIO_KEYS = {"acodec", "sample_rate", "channels"}


def _names(names, limit=4):
    head = "、".join(names[:limit])
    return head + (f" ほか{len(names) - limit}件" if len(names) > limit else "")


def diagnose(infos):
    """連結可否を判定する。

    戻り値 dict:
        ok          : そのまま無劣化コピーで連結できるか
        reasons     : NG の理由（表示用の文字列リスト）
        cfr_fixable : CFR化で解消できるか
    """
    if not infos:
        return {"ok": False, "reasons": ["動画リストが空です。"], "cfr_fixable": False}
    if len(infos) == 1:
        return {"ok": False,
                "reasons": ["動画が1本しかありません。2本以上リストに入れてください。"],
                "cfr_fixable": False}

    for i in infos:
        i["res"] = f"{i['width']}x{i['height']}"
        i["fps_r"] = round(i["fps"], 2) if i["fps"] else None

    reasons = []

    # 音声の有無が混在しているか
    with_audio = [i for i in infos if i["has_audio"]]
    if with_audio and len(with_audio) != len(infos):
        missing = [i["name"] for i in infos if not i["has_audio"]]
        reasons.append(f"音声がありません（{_names(missing)}）"
                       " → CFR化で無音トラックを付加します。")

    # 各パラメータの不一致（音声項目は音声を持つファイル同士で比較）
    for key, label in CHECK_KEYS:
        pool = with_audio if key in AUDIO_KEYS else infos
        values = {i[key] for i in pool if i[key] is not None}
        if len(values) > 1:
            shown = "、".join(str(v) for v in sorted(values, key=str))
            reasons.append(f"{label}が不一致です（{shown}）→ CFR化で統一します。")

    # 非正方形ピクセル（アナモルフィック）
    ana = [i["name"] for i in infos if i.get("anamorphic")]
    if ana:
        reasons.append(f"画素が正方形ではありません（{_names(ana)}）"
                       " → 再生ソフトによって縦横比が変わります。"
                       "CFRボタンを押してCFR化してください（正方形に直します）。")

    # インターレース
    inter = [i["name"] for i in infos if i.get("interlaced")]
    if inter:
        reasons.append(f"インターレース映像です（{_names(inter)}）"
                       " → 動きのある場面に横縞が出ます。"
                       "CFRボタンを押してCFR化してください（縞を解除します）。")

    # VFR
    vfr = [i["name"] for i in infos if i["vfr"]]
    if vfr:
        reasons.append(f"VFR(可変フレームレート)です（{_names(vfr)}）"
                       " → 音ズレの原因になります。CFRボタンを押してCFR化してください。")

    return {"ok": not reasons, "reasons": reasons, "cfr_fixable": bool(reasons)}


# ---------------------------------------------------------------------------
# ヘルプ / ツールチップ
# ---------------------------------------------------------------------------

CFR_HELP_TEXT = """\
■ CFR（シー・エフ・アール）とは？

Constant Frame Rate ＝「固定フレームレート」の略です。
1秒あたりのコマ数（fps）が、最初から最後までずっと一定の動画のことです。


■ なぜ必要？

スマホやカメラで撮った動画の多くは VFR（可変フレームレート）です。
撮影中の負荷に応じてコマ数が細かく揺れています。
（例：29.97fps のはずが、実際は 28〜30fps の間でふらふらしている）

VFR のまま連結すると…
　・つなぎ目で映像と音声がズレる（音ズレ）
　・再生ソフトによってはカクついたり、長さがおかしくなる

また、解像度や形式がバラバラな動画は、そもそもそのままでは連結できません。


■ [CFR] ボタンがやること

リストの全動画を、下記の通りそろえて作り直します。
　・コマ数を一定にする（これが本来の CFR 化）
　・解像度をそろえる（リサイズ指定があればそのサイズに）
　・映像形式を H.264 にそろえる
　・音声を AAC 48kHz ステレオにそろえる（音声が無ければ無音を付ける）

これで [連結チェック] が OK になり、[連結] ボタンが押せるようになります。


■ 知っておくこと

　・元の動画は変更しません。変換後の動画は「_cfr」フォルダに保存されます
　・全コマを作り直すので、時間がかかります（4K素材なら特に）
　・時間を短くしたいとき → エンコーダを GPU にする／preset を速くする
　・サイズを小さくしたいとき → リサイズで FHD 等にする／画質の数値を上げる


■ 使う順番（おさらい）

　① フォルダを選ぶ
　② [連結チェック]
　③ NG なら [CFR] → 自動で再チェック
　④ OK になったら [連結]
"""


class ToolTip:
    """ウィジェットにマウスを乗せると説明を表示する。無効状態のボタンでも出る。"""

    def __init__(self, widget, text, delay=400):
        self.widget = widget
        self.text = text
        self.delay = delay
        self._after = None
        self._tip = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _schedule(self, _event=None):
        self._cancel()
        self._after = self.widget.after(self.delay, self._show)

    def _cancel(self):
        if self._after:
            self.widget.after_cancel(self._after)
            self._after = None

    def _show(self):
        if self._tip:
            return
        x = self.widget.winfo_rootx() + 10
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
        self._tip = tw = tk.Toplevel(self.widget)
        tw.wm_overrideredirect(True)
        tw.wm_geometry(f"+{x}+{y}")
        tk.Label(tw, text=self.text, justify="left", background="#ffffe0",
                 relief="solid", borderwidth=1, padx=8, pady=5,
                 wraplength=420).pack()

    def _hide(self, _event=None):
        self._cancel()
        if self._tip:
            self._tip.destroy()
            self._tip = None


# ---------------------------------------------------------------------------
# GUI 本体
# ---------------------------------------------------------------------------

class VideoChainApp(ttk.Frame):

    def __init__(self, master):
        super().__init__(master, padding=10)
        self.pack(fill="both", expand=True)

        self.items = {}            # iid -> info dict
        self.proc = None           # 実行中の ffmpeg プロセス
        self.worker = None
        self.cancel_flag = False
        self.busy = False
        self.check_result = None   # 連結チェックの結果（None = 未チェック）
        self.settings = {}         # 実行開始時に控えた設定値
        self.msgq = queue.Queue()
        self._drag_iid = None

        self._build_ui()
        self._reset_check()
        self.after(100, self._pump)
        self._check_ffmpeg()

    # ---------------- UI 構築 ----------------

    def _build_ui(self):
        # --- フォルダ選択 ---
        top = ttk.LabelFrame(self, text="① 入力フォルダ", padding=8)
        top.pack(fill="x")

        self.folder_var = tk.StringVar()
        ttk.Entry(top, textvariable=self.folder_var).pack(side="left", fill="x", expand=True)
        ttk.Button(top, text="参照...", command=self.choose_folder, width=10).pack(side="left", padx=(6, 0))
        self.recursive_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(top, text="サブフォルダも含む", variable=self.recursive_var,
                        command=self.reload_folder).pack(side="left", padx=(10, 0))
        ttk.Button(top, text="再読込", command=self.reload_folder, width=8).pack(side="left", padx=(6, 0))

        # --- リスト ---
        mid = ttk.LabelFrame(self, text="② 連結する動画（上から順に連結／ドラッグで入れ替え）", padding=8)

        cols = ("no", "name", "res", "fps", "vcodec", "acodec", "dur")
        headers = {
            "no": ("#", 40), "name": ("ファイル名", 300), "res": ("解像度", 100),
            "fps": ("fps", 70), "vcodec": ("映像", 80), "acodec": ("音声", 80),
            "dur": ("長さ", 80),
        }
        wrap = ttk.Frame(mid)
        wrap.pack(side="left", fill="both", expand=True)

        self.tree = ttk.Treeview(wrap, columns=cols, show="headings", selectmode="extended", height=8)
        for c in cols:
            text, width = headers[c]
            self.tree.heading(c, text=text)
            anchor = "w" if c == "name" else "center"
            self.tree.column(c, width=width, anchor=anchor,
                             stretch=(c == "name"))
        vsb = ttk.Scrollbar(wrap, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="left", fill="y")

        self.tree.tag_configure("vfr", foreground="#b35c00")
        self.tree.bind("<ButtonPress-1>", self._drag_start)
        self.tree.bind("<B1-Motion>", self._drag_motion)
        self.tree.bind("<ButtonRelease-1>", self._drag_end)

        side = ttk.Frame(mid)
        side.pack(side="left", fill="y", padx=(8, 0))
        for text, cmd in (
            ("▲ 上へ", lambda: self.move_selected(-1)),
            ("▼ 下へ", lambda: self.move_selected(1)),
            ("先頭へ", lambda: self.move_edge(True)),
            ("末尾へ", lambda: self.move_edge(False)),
            ("名前順", self.sort_by_name),
            ("逆順", self.reverse_order),
            ("除外", self.remove_selected),
            ("全消去", self.clear_list),
        ):
            ttk.Button(side, text=text, command=cmd, width=10).pack(pady=2)

        # --- 実行 ---
        bottom = ttk.LabelFrame(self, text="③ 実行", padding=8)

        row1 = ttk.Frame(bottom)
        row1.pack(fill="x")
        ttk.Label(row1, text="出力ファイル:").pack(side="left")
        self.output_var = tk.StringVar()
        ttk.Entry(row1, textvariable=self.output_var).pack(side="left", fill="x", expand=True, padx=6)
        ttk.Button(row1, text="参照...", command=self.choose_output, width=10).pack(side="left")

        row2 = ttk.Frame(bottom)
        row2.pack(fill="x", pady=(8, 0))

        self.btn_concat = ttk.Button(row2, text="連　結", command=self.do_concat,
                                     width=14, state="disabled")
        self.btn_concat.pack(side="left")
        self.btn_check = ttk.Button(row2, text="連結チェック", command=self.do_check,
                                    width=14, state="disabled")
        self.btn_check.pack(side="left", padx=6)
        self.btn_cfr = ttk.Button(row2, text="C F R", command=self.do_cfr,
                                  width=14, state="disabled")
        self.btn_cfr.pack(side="left")
        self.btn_help = ttk.Button(row2, text="？", command=self.show_cfr_help, width=3)
        self.btn_help.pack(side="left", padx=(2, 0))

        ttk.Label(row2, text="CFR fps:").pack(side="left", padx=(12, 4))
        self.cfr_fps_var = tk.StringVar(value=CFR_FPS_CHOICES[0])
        ttk.Combobox(row2, textvariable=self.cfr_fps_var, values=CFR_FPS_CHOICES,
                     state="readonly", width=12).pack(side="left")

        ttk.Label(row2, text="リサイズ:").pack(side="left", padx=(12, 4))
        self.res_var = tk.StringVar(value=RES_CHOICES[0])
        ttk.Combobox(row2, textvariable=self.res_var, values=RES_CHOICES,
                     state="readonly", width=16).pack(side="left")

        self.btn_cancel = ttk.Button(row2, text="中止", command=self.cancel, width=8, state="disabled")
        self.btn_cancel.pack(side="right")

        # --- エンコード設定 ---
        row3 = ttk.Frame(bottom)
        row3.pack(fill="x", pady=(8, 0))

        ttk.Label(row3, text="エンコーダ:").pack(side="left", padx=(0, 4))
        self.encoder_var = tk.StringVar(value="CPU (libx264)")
        self.encoder_cb = ttk.Combobox(row3, textvariable=self.encoder_var,
                                       values=["CPU (libx264)"],
                                       state="readonly", width=22)
        self.encoder_cb.pack(side="left")
        self.encoder_cb.bind("<<ComboboxSelected>>", self._on_encoder_change)

        ttk.Label(row3, text="画質(CRF/QP):").pack(side="left", padx=(12, 4))
        self.crf_var = tk.StringVar(value="18")
        ttk.Combobox(row3, textvariable=self.crf_var,
                     values=["14", "16", "18", "20", "23", "26"],
                     state="readonly", width=5).pack(side="left")

        ttk.Label(row3, text="速度(preset):").pack(side="left", padx=(12, 4))
        self.preset_var = tk.StringVar(value="veryfast")
        self.preset_cb = ttk.Combobox(row3, textvariable=self.preset_var,
                                      values=X264_PRESETS,
                                      state="readonly", width=12)
        self.preset_cb.pack(side="left")

        ttk.Label(row3, text="サイズ警告:").pack(side="left", padx=(12, 4))
        self.warn_gb_var = tk.StringVar(value="10 GB超")
        ttk.Combobox(row3, textvariable=self.warn_gb_var,
                     values=["2 GB超", "5 GB超", "10 GB超", "20 GB超",
                             "50 GB超", "警告しない"],
                     state="readonly", width=10).pack(side="left")

        self.status_var = tk.StringVar(value="フォルダを選択してください")
        self.status_lbl = tk.Label(bottom, textvariable=self.status_var, anchor="w",
                                   fg="#666666", font=("", 10, "bold"))
        self.status_lbl.pack(fill="x", pady=(8, 0))

        # --- 進捗 & ログ ---
        self.progress = ttk.Progressbar(self, mode="determinate", maximum=1000)

        logf = ttk.LabelFrame(self, text="ログ", padding=4)
        self.log = tk.Text(logf, height=10, wrap="none", state="disabled",
                           font=("Consolas", 9) if os.name == "nt" else None)
        lsb = ttk.Scrollbar(logf, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=lsb.set)
        self.log.pack(side="left", fill="both", expand=True)
        lsb.pack(side="left", fill="y")

        # --- 配置 ---
        # 下側（ログ・進捗・実行）を先に確保し、残りを動画リストに割り当てる。
        # ウィンドウが狭くても、縮むのはリストのほうになる。
        logf.pack(side="bottom", fill="both")
        self.progress.pack(side="bottom", fill="x", pady=(8, 4))
        bottom.pack(side="bottom", fill="x", pady=(8, 0))
        mid.pack(side="top", fill="both", expand=True, pady=(8, 0))

        self._setup_tooltips()

    def _setup_tooltips(self):
        tips = {
            self.btn_check:
                "リストの動画がそのまま連結できるか調べます。\n"
                "OK → [連結] が押せるようになります。\n"
                "NG → 理由をログに表示し、直せるなら [CFR] が押せるようになります。",
            self.btn_concat:
                "リストの順番通りに1本の動画に連結します。\n"
                "再エンコードしない無劣化コピーなので、速くて画質も落ちません。\n"
                "（[連結チェック] が OK のときだけ押せます）",
            self.btn_cfr:
                "動画のコマ数(fps)を一定にして、解像度・形式・音声もそろえます。\n"
                "連結チェックで NG になった動画を、連結できる状態に作り直します。\n"
                "元の動画はそのまま。変換後は「_cfr」フォルダに保存されます。\n"
                "詳しくは右の「？」ボタン。",
            self.btn_help: "CFR とは何か、なぜ必要かの説明を表示します。",
        }
        for widget, text in tips.items():
            ToolTip(widget, text)

    def show_cfr_help(self):
        win = tk.Toplevel(self)
        win.title("CFR とは？")
        win.geometry("620x640")
        win.transient(self.winfo_toplevel())
        txt = tk.Text(win, wrap="word", padx=14, pady=12,
                      font=("", 10), relief="flat")
        sb = ttk.Scrollbar(win, orient="vertical", command=txt.yview)
        txt.configure(yscrollcommand=sb.set)
        txt.insert("1.0", CFR_HELP_TEXT)
        txt.configure(state="disabled")
        ttk.Button(win, text="閉じる", command=win.destroy).pack(side="bottom", pady=8)
        sb.pack(side="right", fill="y")
        txt.pack(side="left", fill="both", expand=True)

    # ---------------- ログ / スレッド間通信 ----------------

    def _log(self, text):
        console_write(text)
        self.msgq.put(("log", text))

    def _pump(self):
        try:
            while True:
                kind, payload = self.msgq.get_nowait()
                if kind == "log":
                    self.log.configure(state="normal")
                    self.log.insert("end", payload + "\n")
                    self.log.see("end")
                    self.log.configure(state="disabled")
                elif kind == "progress":
                    self.progress["value"] = payload
                elif kind == "done":
                    self._set_busy(False)
                    self.progress["value"] = 0
                elif kind == "reload":
                    self.reload_folder()
                elif kind == "info":
                    messagebox.showinfo("VideoChain", payload)
                elif kind == "error":
                    messagebox.showerror("VideoChain", payload)
        except queue.Empty:
            pass
        self.after(100, self._pump)

    def _set_busy(self, busy):
        self.busy = busy
        self.btn_cancel.configure(state="normal" if busy else "disabled")
        self._apply_button_state()

    def _apply_button_state(self):
        """① 最初は [連結チェック] のみ ② OK なら [連結] ③ CFRで直るなら [CFR]"""
        if self.busy:
            for b in (self.btn_concat, self.btn_check, self.btn_cfr):
                b.configure(state="disabled")
            return
        has_items = bool(self.tree.get_children())
        r = self.check_result
        self.btn_check.configure(state="normal" if has_items else "disabled")
        self.btn_concat.configure(
            state="normal" if (r and r["ok"]) else "disabled")
        self.btn_cfr.configure(
            state="normal" if (r and not r["ok"] and r["cfr_fixable"]) else "disabled")

    def _set_status(self, text, color="#666666"):
        self.status_var.set(text)
        self.status_lbl.configure(fg=color)

    def _reset_check(self):
        """リストの中身が変わったらチェック結果を破棄して①の状態に戻す。"""
        self.check_result = None
        if self.tree.get_children():
            self._set_status("未チェック － [連結チェック] を押してください")
        else:
            self._set_status("フォルダを選択してください")
        self._apply_button_state()

    def _on_encoder_change(self, _event=None):
        """CPU以外を選んだら preset を無効化する。"""
        is_cpu = encoder_name(self.encoder_var.get()) == "libx264"
        self.preset_cb.configure(state="readonly" if is_cpu else "disabled")

    def _check_ffmpeg(self):
        if shutil.which(FFMPEG) is None and not os.path.isfile(FFMPEG):
            self._log("!! ffmpeg が見つかりません。PATH を通すか、スクリプト冒頭の "
                      "FFMPEG / FFPROBE にフルパスを設定してください。")
            return
        self._log("ffmpeg OK。")
        self._log("")
        self._log("【使う順番】")
        self._log("  ① フォルダを選ぶ → ② [連結チェック]")
        self._log("  ③ NG なら [CFR] → 自動で再チェック → ④ OK なら [連結]")
        self._log("  ※ ボタンにマウスを乗せると説明が出ます。CFR の意味は「？」ボタン。")
        self._log("")
        self._log("GPU エンコーダを確認中...")
        self.encoder_cb.configure(state="disabled")
        threading.Thread(target=self._detect_encoders, daemon=True).start()

    def _detect_encoders(self):
        usable, unusable = available_encoders()
        self.after(0, lambda: self._apply_encoders(usable, unusable))

    def _apply_encoders(self, usable, unusable):
        self.encoder_cb.configure(values=usable, state="readonly")
        if self.encoder_var.get() not in usable:
            self.encoder_var.set(usable[0])
        gpus = [u for u in usable if u.startswith("GPU")]
        if gpus:
            self._log("使える GPU エンコーダ: " + " / ".join(gpus))
            self._log("  CPUを空けられます。GPU選択時、画質欄はQP値です(18〜24が目安)。")
        else:
            self._log("このPCで使える GPU エンコーダはありません。CPU で変換します。")
        if unusable:
            self._log("  （ffmpeg は対応しているが、このPCでは使えない: "
                      + " / ".join(u.replace("GPU ", "") for u in unusable) + "）")
        self._on_encoder_change()

    # ---------------- フォルダ / リスト操作 ----------------

    def choose_folder(self):
        d = filedialog.askdirectory(title="動画フォルダを選択")
        if d:
            self.folder_var.set(d)
            self.reload_folder()

    def reload_folder(self):
        folder = self.folder_var.get().strip()
        if not folder or not os.path.isdir(folder):
            return
        if self.worker and self.worker.is_alive():
            return

        paths = []
        if self.recursive_var.get():
            for root, _, files in os.walk(folder):
                for f in files:
                    if os.path.splitext(f)[1].lower() in VIDEO_EXTS:
                        paths.append(os.path.join(root, f))
        else:
            for f in os.listdir(folder):
                p = os.path.join(folder, f)
                if os.path.isfile(p) and os.path.splitext(f)[1].lower() in VIDEO_EXTS:
                    paths.append(p)

        paths.sort(key=lambda p: natural_key(os.path.relpath(p, folder)))
        if not paths:
            self._log("動画ファイルが見つかりませんでした。")
            self.clear_list()
            return

        # 出力先の初期値
        if not self.output_var.get().strip():
            base = os.path.basename(os.path.normpath(folder)) or "output"
            self.output_var.set(os.path.join(folder, f"{base}_concat.mp4"))

        self._set_busy(True)
        self._log(f"--- {len(paths)} 件を解析中 ---")
        self.worker = threading.Thread(target=self._load_worker, args=(paths,), daemon=True)
        self.worker.start()

    def _load_worker(self, paths):
        infos = []
        for p in paths:
            try:
                infos.append(probe(p))
            except Exception as e:
                self._log(f"  スキップ: {os.path.basename(p)} ({e})")
        self.after(0, lambda: self._fill_tree(infos))

    def _fill_tree(self, infos, keep_check=False):
        self.tree.delete(*self.tree.get_children())
        self.items.clear()
        for info in infos:
            self._insert(info)
        self._renumber()
        total = sum(i["duration"] or 0 for i in infos)
        self._log(f"読み込み完了: {len(infos)} 件 / 合計 {fmt_duration(total)}")
        if not keep_check:
            self._reset_check()
        self._set_busy(False)

    def _insert(self, info, index="end"):
        res = f"{info['width']}x{info['height']}"
        if info.get("anamorphic"):
            res += f"→{info['disp_w']}"
        vals = (
            "", info["name"], res,
            f"{info['fps']:.2f}" + ("*" if info["vfr"] else "") if info["fps"] else "-",
            info["vcodec"],
            info["acodec"] or "なし",
            fmt_duration(info["duration"]),
        )
        iid = self.tree.insert("", index, values=vals, tags=("vfr",) if info["vfr"] else ())
        self.items[iid] = info
        return iid

    def _renumber(self):
        for n, iid in enumerate(self.tree.get_children(), 1):
            self.tree.set(iid, "no", str(n))

    def ordered_infos(self):
        return [self.items[iid] for iid in self.tree.get_children()]

    def move_selected(self, delta):
        sel = list(self.tree.selection())
        if not sel:
            return
        children = list(self.tree.get_children())
        sel.sort(key=children.index, reverse=(delta > 0))
        for iid in sel:
            idx = self.tree.index(iid)
            new = idx + delta
            if 0 <= new < len(children):
                self.tree.move(iid, "", new)
        self._renumber()

    def move_edge(self, to_top):
        sel = list(self.tree.selection())
        if not sel:
            return
        children = list(self.tree.get_children())
        sel.sort(key=children.index, reverse=not to_top)
        for i, iid in enumerate(sel):
            self.tree.move(iid, "", i if to_top else "end")
        self._renumber()

    def sort_by_name(self):
        infos = sorted(self.ordered_infos(), key=lambda i: natural_key(i["name"]))
        self._fill_tree(infos, keep_check=True)

    def reverse_order(self):
        self._fill_tree(list(reversed(self.ordered_infos())), keep_check=True)

    def remove_selected(self):
        for iid in self.tree.selection():
            self.items.pop(iid, None)
            self.tree.delete(iid)
        self._renumber()
        self._reset_check()

    def clear_list(self):
        self.tree.delete(*self.tree.get_children())
        self.items.clear()
        self._reset_check()

    # ---------------- ドラッグ＆ドロップ並べ替え ----------------

    def _drag_start(self, event):
        self._drag_iid = self.tree.identify_row(event.y)

    def _drag_motion(self, event):
        if not self._drag_iid:
            return
        target = self.tree.identify_row(event.y)
        if target and target != self._drag_iid:
            self.tree.move(self._drag_iid, "", self.tree.index(target))

    def _drag_end(self, event):
        if self._drag_iid:
            self._renumber()
        self._drag_iid = None

    # ---------------- 出力先 ----------------

    def choose_output(self):
        initial = self.output_var.get().strip()
        path = filedialog.asksaveasfilename(
            title="出力ファイル",
            defaultextension=".mp4",
            initialdir=os.path.dirname(initial) if initial else None,
            initialfile=os.path.basename(initial) if initial else "output.mp4",
            filetypes=[("MP4", "*.mp4"), ("MKV", "*.mkv"), ("MOV", "*.mov"), ("すべて", "*.*")],
        )
        if path:
            self.output_var.set(path)

    # ---------------- ffmpeg 実行 ----------------

    def _run_ffmpeg(self, args, total_sec=None, label="", quiet=False):
        """ffmpeg を実行しログと進捗を流す。成功なら True。"""
        cmd = [FFMPEG, "-hide_banner", "-nostdin", "-y"] + args
        if not quiet:
            self._log(f"$ {' '.join(cmd)}")
        self.proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
            bufsize=1, creationflags=_creationflags())

        tail = []
        time_re = re.compile(r"time=(\d+):(\d+):([\d.]+)")
        for line in self.proc.stdout:
            line = line.rstrip()
            if not line:
                continue
            m = time_re.search(line)
            if not quiet:
                console_write(line, inline=bool(m))
            if m and total_sec and not quiet:
                cur = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
                self.msgq.put(("progress", min(1000, cur / total_sec * 1000)))
            elif not m:
                tail.append(line)
                if len(tail) > 60:
                    tail.pop(0)

        code = self.proc.wait()
        self.proc = None
        if code != 0 and not self.cancel_flag and not quiet:
            self._log(f"!! ffmpeg エラー (code={code}) {label}")
            for l in tail[-15:]:
                self._log("   " + l)
            return False
        return code == 0

    def _ask_main(self, title, msg):
        """ワーカースレッドから確認ダイアログを出し、回答を待つ。"""
        done = threading.Event()
        box = {}

        def ask():
            box["answer"] = messagebox.askyesno(title, msg, icon="warning")
            done.set()

        self.after(0, ask)
        done.wait()
        return box.get("answer", False)

    # ---------------- サイズ見積り ----------------

    def _estimate_bps(self, info, vf, vopt, cfr_opt, rate, sample=5.0):
        """先頭付近を実際に短時間エンコードして、1秒あたりのバイト数を測る。"""
        dur = info["duration"] or 0
        ss = max(0.0, dur / 2 - sample / 2) if dur > sample * 2.5 else 0.0
        tmp = os.path.join(tempfile.gettempdir(), "_videochain_probe.mp4")
        args = (["-ss", f"{ss:.2f}", "-i", info["path"], "-t", str(sample), "-an",
                 "-vf", vf] + cfr_opt + ["-r", rate] + vopt + [tmp])
        try:
            if not self._run_ffmpeg(args, quiet=True):
                return None
            size = os.path.getsize(tmp)
            if size <= 0:
                return None
            return size / sample + 24000  # 映像 + 音声192kbps分
        except Exception:
            return None
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass

    def _warn_threshold(self):
        text = self.settings.get("warn", "警告しない")
        if text.startswith("警告しない"):
            return None
        return float(re.sub(r"[^\d.]", "", text)) * (1024 ** 3)

    def _size_check(self, infos, vf, vopt, cfr_opt, rate):
        """見積りサイズを出し、大きければ続行可否を尋ねる。続行なら True。"""
        total_sec = sum(i["duration"] or 0 for i in infos)
        src_total = sum(os.path.getsize(i["path"]) for i in infos
                        if os.path.exists(i["path"]))
        self._log("  サイズ見積り中（5秒テストエンコード）...")
        bps = self._estimate_bps(infos[0], vf, vopt, cfr_opt, rate)
        if bps is None:
            self._log("  見積りできませんでした。そのまま続行します。")
            return True

        est = bps * total_sec
        try:
            free = shutil.disk_usage(os.path.dirname(infos[0]["path"])).free
        except Exception:
            free = None

        self._log(f"  見積り: 約 {fmt_size(est)}（元ファイル合計 {fmt_size(src_total)}）")
        if free is not None:
            self._log(f"  空き容量: {fmt_size(free)}")

        threshold = self._warn_threshold()
        too_big = threshold is not None and est > threshold
        no_space = free is not None and est > free * 0.9
        if not (too_big or no_space):
            return True

        msg = [f"変換後のサイズが大きくなる見込みです。", ""]
        msg.append(f"　見積りサイズ : 約 {fmt_size(est)}")
        msg.append(f"　元ファイル合計 : {fmt_size(src_total)}")
        if free is not None:
            msg.append(f"　ドライブ空き容量 : {fmt_size(free)}")
        msg.append("")
        if no_space:
            msg.append("※ 空き容量が不足する可能性があります。")
        msg.append("小さくしたい場合は、いったん中止して")
        msg.append("画質の数値を上げる（20〜23）か、presetを medium 寄りにしてください。")
        msg.append("")
        msg.append("このまま続行しますか？")

        if self._ask_main("サイズ警告", "\n".join(msg)):
            return True
        self._log("  中止しました（サイズ警告）。")
        return False

    def cancel(self):
        self.cancel_flag = True
        if self.proc:
            try:
                self.proc.kill()
            except Exception:
                pass
        self._log("中止しました。")

    def _snapshot_settings(self):
        """実行開始時に設定値を控える。

        tkinter のウィジェットは別スレッドから触れないため、
        処理を始める前にメインスレッドでまとめて読んでおく。
        """
        self.settings = {
            "quality": self.crf_var.get(),
            "encoder": self.encoder_var.get(),
            "preset": self.preset_var.get(),
            "warn": self.warn_gb_var.get(),
            "fps_choice": self.cfr_fps_var.get(),
            "res_choice": self.res_var.get(),
        }
        return self.settings

    def _start(self, target, *args):
        if self.worker and self.worker.is_alive():
            messagebox.showwarning("VideoChain", "処理中です。")
            return
        infos = self.ordered_infos()
        if not infos:
            messagebox.showwarning("VideoChain", "動画リストが空です。")
            return
        self._snapshot_settings()
        self.cancel_flag = False
        self._set_busy(True)
        self.worker = threading.Thread(target=target, args=(infos,) + args, daemon=True)
        self.worker.start()

    # ---------------- ① 連結チェック ----------------

    def do_check(self):
        self._start(self._check_worker)

    def _check_worker(self, infos):
        self._log("")
        self._log("========== 連結チェック ==========")
        total = sum(i["duration"] or 0 for i in infos)
        self._log(f"対象 {len(infos)} 件 / 合計 {fmt_duration(total)}")
        result = diagnose(infos)

        if result["ok"]:
            self._log("【 OK 】全ファイルの規格が一致しています。")
            self._log("        無劣化コピー(-c copy)でそのまま連結できます。")
            self._log("        → [連結] ボタンが押せるようになりました。")
        else:
            self._log("【 NG 】そのままでは連結できません。理由:")
            for n, reason in enumerate(result["reasons"], 1):
                self._log(f"   {n}. {reason}")
            if result["cfr_fixable"]:
                self._log("        → [CFR] を実行すれば解消できます。")
                self._log("          （実行後、自動でもう一度チェックします）")
                self._log("        ※ CFR化 = コマ数(fps)を一定にし、解像度・形式・音声もそろえて")
                self._log("          連結できる状態に作り直すこと。詳しくは [CFR] 横の「？」")
            else:
                self._log("        → リストの内容を見直してください。")
        self._log("==================================")
        self.after(0, lambda: self._apply_check_result(result))

    def _apply_check_result(self, result):
        self.check_result = result
        if result["ok"]:
            self._set_status("【 OK 】連結できます（無劣化コピー）", "#0a7a28")
        else:
            head = result["reasons"][0].split(" →")[0]
            more = (f"（ほか{len(result['reasons']) - 1}件 ログ参照）"
                    if len(result["reasons"]) > 1 else "")
            tail = " → [CFR] で解消できます" if result["cfr_fixable"] else ""
            self._set_status(f"【 NG 】{head}{more}{tail}", "#c00000")
        self._set_busy(False)

    # ---------------- ② CFR 変換 ----------------

    def do_cfr(self):
        infos = self.ordered_infos()
        if not infos:
            return
        self._snapshot_settings()
        fps = self._target_fps(infos)
        W, H = self._target_size(infos)
        msg = (f"{len(infos)} 件を下記の規格に統一します。\n\n"
               f"　解像度 : {W} x {H}\n"
               f"　fps    : {fps}（固定／CFR）\n"
               f"　映像   : H.264 yuv420p 画質 {self.settings['quality']}\n"
               f"　音声   : AAC 48kHz ステレオ（無い場合は無音を付加）\n\n"
               f"出力先: 各フォルダの _cfr サブフォルダ\n"
               f"完了後、リストを変換後ファイルに差し替えて再チェックします。")
        if not messagebox.askokcancel("CFR化", msg):
            return
        self._start(self._cfr_worker, W, H, fps)

    def _target_fps(self, infos):
        choice = self.settings.get("fps_choice", self.cfr_fps_var.get())
        if choice.startswith("自動"):
            rates = [round(i["fps"], 3) for i in infos if i["fps"]]
            if not rates:
                return 30.0
            raw = max(set(rates), key=rates.count)
            fps = snap_fps(raw)
            if abs(fps - raw) > 0.001:
                self._log(f"  実測 {raw} fps → 標準の {fps} fps に丸めました。")
            return fps
        return float(choice)

    def _target_size(self, infos):
        choice = self.settings.get("res_choice", self.res_var.get())
        m = re.match(r"(\d+)x(\d+)", choice)
        if m:
            return int(m.group(1)), int(m.group(2))
        sizes = [(i.get("disp_w") or i["width"], i["height"])
                 for i in infos if i["width"]]
        if not sizes:
            return 1920, 1080
        return max(set(sizes), key=sizes.count)

    def _cfr_worker(self, infos, W, H, fps):
        quality = self.settings["quality"]
        label = self.settings["encoder"]
        vopt = video_opts(label, quality, self.settings["preset"])
        rate = fps_arg(fps)
        cfr_opt = cfr_option()
        vf_base = (f"scale={W}:{H}:force_original_aspect_ratio=decrease:flags=lanczos,"
                   f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2,setsar=1,"
                   f"fps={rate},format=yuv420p")
        vf = vf_base
        self._log("")
        self._log(f"===== CFR化 ({W}x{H} / {fps}fps) =====")
        self._log(f"  エンコーダ: {label} / 画質 {quality}")
        self._log(f"  CFR指定: {' '.join(cfr_opt)} / -r {rate}")
        if any(i.get("interlaced") for i in infos):
            self._log("  インターレース素材を検出 → yadif で縞を解除します。")

        if not self._size_check(infos, vf, vopt, cfr_opt, rate):
            self.msgq.put(("done", None))
            return
        if self.cancel_flag:
            self.msgq.put(("done", None))
            return

        mapping = {}
        failed = 0
        for n, info in enumerate(infos, 1):
            if self.cancel_flag:
                break
            outdir = os.path.join(os.path.dirname(info["path"]), "_cfr")
            os.makedirs(outdir, exist_ok=True)
            out = os.path.join(outdir, os.path.splitext(info["name"])[0] + "_cfr.mp4")
            self._log(f"[{n}/{len(infos)}] {info['name']}")

            args = ["-i", info["path"]]
            if info["has_audio"]:
                args += ["-map", "0:v:0", "-map", "0:a:0"]
            else:
                args += ["-f", "lavfi", "-i",
                         "anullsrc=channel_layout=stereo:sample_rate=48000",
                         "-map", "0:v:0", "-map", "1:a:0", "-shortest"]
            pre = "yadif=0:-1:0," if info.get("interlaced") else ""
            if info.get("anamorphic"):
                # 非正方形ピクセル（HDV/AVCHD の 1440x1080 など）は
                # 先に正方形ピクセルへ引き伸ばしてから、目的のサイズに合わせる
                pre += "scale=trunc(iw*sar/2)*2:ih:flags=lanczos,setsar=1,"
            args += ["-vf", pre + vf_base] + cfr_opt + ["-r", rate] + vopt + [
                "-video_track_timescale", "90000",
                "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
                "-movflags", "+faststart", out,
            ]
            if self._run_ffmpeg(args, info["duration"], info["name"]):
                mapping[info["path"]] = out
            else:
                failed += 1
            self.msgq.put(("progress", 0))

        if failed and encoder_name(label) != "libx264":
            self._log("  ヒント: ハードウェアエンコードが失敗する場合は "
                      "エンコーダを CPU (libx264) に戻してください。")

        if mapping and not self.cancel_flag:
            self._log(f"CFR化 完了: {len(mapping)} 件")
            self.after(0, lambda: self._swap_to_cfr(mapping))
        else:
            self.msgq.put(("done", None))

    def _swap_to_cfr(self, mapping):
        """リストの該当行を変換済みファイルに差し替え、自動で再チェックする。"""
        new_infos = []
        for info in self.ordered_infos():
            newpath = mapping.get(info["path"])
            if newpath:
                try:
                    new_infos.append(probe(newpath))
                    continue
                except Exception:
                    pass
            new_infos.append(info)
        self._fill_tree(new_infos)
        self._log("リストを CFR 化後のファイルに差し替えました。再チェックします。")
        self.do_check()

    # ---------------- ③ 連結 ----------------

    def do_concat(self):
        if not (self.check_result and self.check_result["ok"]):
            messagebox.showwarning("VideoChain", "先に [連結チェック] を実行してください。")
            return
        out = self.output_var.get().strip()
        if not out:
            messagebox.showwarning("VideoChain", "出力ファイルを指定してください。")
            return
        infos = self.ordered_infos()
        if any(os.path.abspath(i["path"]) == os.path.abspath(out) for i in infos):
            messagebox.showerror("VideoChain", "出力先が入力ファイルと同じです。")
            return
        if os.path.exists(out) and not messagebox.askokcancel(
                "上書き確認", f"既に存在します。上書きしますか？\n{out}"):
            return
        self._start(self._concat_worker, out, True)

    def _concat_worker(self, infos, out, use_copy):
        total = sum(i["duration"] or 0 for i in infos)
        self._log("")
        self._log(f"=== 連結 ({'無劣化コピー' if use_copy else '再エンコード'}) "
                  f"{len(infos)} 件 / {fmt_duration(total)} ===")
        os.makedirs(os.path.dirname(os.path.abspath(out)) or ".", exist_ok=True)

        success = (self._concat_copy(infos, out, total) if use_copy
                   else self._concat_encode(infos, out, total))

        if success and not self.cancel_flag:
            size = os.path.getsize(out) / (1024 ** 2)
            self._log(f"○ 完了: {out} ({size:.1f} MB)")
            self.msgq.put(("info", f"連結が完了しました。\n\n{out}"))
        self.msgq.put(("done", None))

    def _concat_copy(self, infos, out, total):
        src_total = sum(os.path.getsize(i["path"]) for i in infos
                        if os.path.exists(i["path"]))
        self._log(f"  出力サイズ見込み: 約 {fmt_size(src_total)}（コピーのため元と同等）")
        try:
            free = shutil.disk_usage(os.path.dirname(os.path.abspath(out))).free
            if src_total > free * 0.95:
                if not self._ask_main(
                        "空き容量の警告",
                        f"出力先の空き容量が不足する可能性があります。\n\n"
                        f"　必要サイズ : 約 {fmt_size(src_total)}\n"
                        f"　空き容量   : {fmt_size(free)}\n\n"
                        f"このまま続行しますか？"):
                    self._log("  中止しました（空き容量）。")
                    return False
        except Exception:
            pass

        listfile = os.path.join(os.path.dirname(os.path.abspath(out)), "_concat_list.txt")
        with open(listfile, "w", encoding="utf-8") as f:
            for i in infos:
                p = i["path"].replace("\\", "/").replace("'", r"'\''")
                f.write(f"file '{p}'\n")
        try:
            args = ["-f", "concat", "-safe", "0", "-i", listfile, "-c", "copy"]
            if out.lower().endswith(".mp4"):
                args += ["-movflags", "+faststart"]
            args.append(out)
            return self._run_ffmpeg(args, total, "concat copy")
        finally:
            try:
                os.remove(listfile)
            except OSError:
                pass

    def _concat_encode(self, infos, out, total):
        """解像度・fps を先頭ファイルに揃えて再エンコード連結。"""
        base = infos[0]
        W, H = base["width"], base["height"]
        fps = self._target_fps(infos)
        rate = fps_arg(fps)
        crf = self.settings["quality"]
        need_silence = not all(i["has_audio"] for i in infos)

        args = []
        for i in infos:
            args += ["-i", i["path"]]

        silent_idx = {}
        if need_silence:
            for n, i in enumerate(infos):
                if not i["has_audio"]:
                    dur = i["duration"] or 1.0
                    silent_idx[n] = len(infos) + len(silent_idx)
                    args += ["-f", "lavfi", "-t", f"{dur:.3f}",
                             "-i", "anullsrc=channel_layout=stereo:sample_rate=48000"]

        chains, refs = [], []
        for n, i in enumerate(infos):
            chains.append(
                f"[{n}:v]scale={W}:{H}:force_original_aspect_ratio=decrease:flags=lanczos,"
                f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={rate},format=yuv420p[v{n}]")
            aidx = silent_idx.get(n, n)
            chains.append(
                f"[{aidx}:a]aresample=48000,"
                f"aformat=sample_fmts=fltp:channel_layouts=stereo[a{n}]")
            refs.append(f"[v{n}][a{n}]")

        filt = ";".join(chains) + ";" + "".join(refs) + \
               f"concat=n={len(infos)}:v=1:a=1[outv][outa]"

        args += ["-filter_complex", filt, "-map", "[outv]", "-map", "[outa]"]
        args += video_opts(self.settings["encoder"], crf,
                           self.settings["preset"])
        args += ["-c:a", "aac", "-b:a", "192k"]
        if out.lower().endswith(".mp4"):
            args += ["-movflags", "+faststart"]
        args.append(out)

        self._log(f"  基準: {W}x{H} / {fps} fps / CRF {crf}")
        return self._run_ffmpeg(args, total, "concat encode")


def main():
    setup_console()
    console_write("VideoChain 起動。ffmpeg の出力はこの窓に表示されます。")
    console_write("（この窓を閉じるとアプリも終了します）")
    console_write("")
    root = tk.Tk()
    root.title("VideoChain - 動画連結ツール(MTS対応版)")
    root.geometry("1060x880")
    root.minsize(900, 720)
    try:
        ttk.Style().theme_use("vista" if os.name == "nt" else "clam")
    except tk.TclError:
        pass
    VideoChainApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
