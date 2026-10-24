# -*- coding: utf-8 -*-
"""
pycapture.py  --  Python製 スクリーンショット常駐ツール (Windows 10/11)

IrfanViewの代わりに、キャプチャ〜連番保存を全部Python側で制御する。
- 範囲ドラッグ選択 / 全画面 / アクティブウィンドウ の3モード
- ファイル名は日時秒ベース（例 capture_20260720_143025.png）→ 常に別名で衝突なし
- 上書き確認は一切なし
- グローバルホットキー（他アプリ作業中でも押せば撮れる）

依存: Pillow だけ  ->  pip install pillow
それ以外(tkinter/ctypes/json/threading)は標準ライブラリ。

既定ホットキー:
    Ctrl + Alt + 1  … 範囲選択キャプチャ
    Ctrl + Alt + 2  … 全画面キャプチャ
    Ctrl + Alt + 3  … アクティブウィンドウキャプチャ
"""

import os
import sys
import json
import time
import queue
import ctypes
import threading
import subprocess
from ctypes import wintypes
from pathlib import Path
from datetime import datetime

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from PIL import ImageGrab

# ---------------------------------------------------------------------------
# DPI対応: tkinterの座標とImageGrabのピクセルを一致させるため、システムDPI認識にする。
# （高DPI/拡大表示のモニタで範囲がズレるのを防ぐ）
# ---------------------------------------------------------------------------
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(1)   # PROCESS_SYSTEM_DPI_AWARE
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

user32 = ctypes.windll.user32

# 仮想スクリーン(全モニタを囲む矩形)のメトリクス
SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN = 76, 77
SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 78, 79


def virtual_screen():
    gm = user32.GetSystemMetrics
    return (gm(SM_XVIRTUALSCREEN), gm(SM_YVIRTUALSCREEN),
            gm(SM_CXVIRTUALSCREEN), gm(SM_CYVIRTUALSCREEN))


# ---------------------------------------------------------------------------
# 設定の読み書き (連番もここに保存して永続化)
# ---------------------------------------------------------------------------
CONFIG_PATH = Path.home() / ".pycapture_config.json"
DEFAULT_CONFIG = {
    "save_dir": str(Path.home() / "Pictures" / "capture"),
    "prefix": "capture",
    "ext": "png",
}


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
        except Exception:
            pass
    return cfg


def save_config(cfg):
    try:
        CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2),
                               encoding="utf-8")
    except Exception as e:
        print("config save failed:", e)


# ---------------------------------------------------------------------------
# キャプチャ本体
# ---------------------------------------------------------------------------
def grab_bbox(bbox):
    """bbox=(x1,y1,x2,y2) を撮る。all_screens=True でマルチモニタの負座標にも対応。"""
    return ImageGrab.grab(bbox=bbox, all_screens=True)


def grab_fullscreen():
    vx, vy, vw, vh = virtual_screen()
    return grab_bbox((vx, vy, vx + vw, vy + vh))


def grab_active_window():
    """前面ウィンドウの矩形をDWMの実表示枠で取得して撮る（影ぶんの余白を除く）。"""
    hwnd = user32.GetForegroundWindow()

    class RECT(ctypes.Structure):
        _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                    ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

    rect = RECT()
    DWMWA_EXTENDED_FRAME_BOUNDS = 9
    ok = False
    try:
        res = ctypes.windll.dwmapi.DwmGetWindowAttribute(
            wintypes.HWND(hwnd), DWMWA_EXTENDED_FRAME_BOUNDS,
            ctypes.byref(rect), ctypes.sizeof(rect))
        ok = (res == 0)
    except Exception:
        ok = False
    if not ok:
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
    return grab_bbox((rect.left, rect.top, rect.right, rect.bottom))


class RegionSelector:
    """全モニタを覆う半透明オーバーレイを出して、ドラッグで範囲を選ばせる。"""

    def __init__(self, root, on_done):
        self.on_done = on_done
        vx, vy, vw, vh = virtual_screen()
        self.vx, self.vy = vx, vy

        self.top = tk.Toplevel(root)
        self.top.overrideredirect(True)
        self.top.geometry(f"{vw}x{vh}+{vx}+{vy}")
        self.top.attributes("-topmost", True)
        try:
            self.top.attributes("-alpha", 0.30)
        except Exception:
            pass
        self.top.configure(bg="black")
        self.top.config(cursor="crosshair")

        self.canvas = tk.Canvas(self.top, bg="black", highlightthickness=0,
                                cursor="crosshair")
        self.canvas.pack(fill="both", expand=True)

        self.start = None
        self.rect_id = None
        self.canvas.bind("<ButtonPress-1>", self._press)
        self.canvas.bind("<B1-Motion>", self._drag)
        self.canvas.bind("<ButtonRelease-1>", self._release)
        self.top.bind("<Escape>", lambda e: self._cancel())
        self.top.focus_force()

    def _press(self, e):
        self.start = (e.x_root, e.y_root)
        if self.rect_id:
            self.canvas.delete(self.rect_id)
        # ローカル座標で矩形を描く
        lx, ly = e.x_root - self.vx, e.y_root - self.vy
        self.rect_id = self.canvas.create_rectangle(
            lx, ly, lx, ly, outline="#39a0ff", width=2)

    def _drag(self, e):
        if not self.start:
            return
        sx, sy = self.start
        self.canvas.coords(self.rect_id,
                           sx - self.vx, sy - self.vy,
                           e.x_root - self.vx, e.y_root - self.vy)

    def _release(self, e):
        if not self.start:
            return self._cancel()
        x1, y1 = self.start
        x2, y2 = e.x_root, e.y_root
        bbox = (min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2))
        self.top.destroy()
        if bbox[2] - bbox[0] < 3 or bbox[3] - bbox[1] < 3:
            return  # ほぼ点 = キャンセル扱い
        # オーバーレイが確実に消えてから撮る
        self.top.update_idletasks()
        time.sleep(0.12)
        try:
            img = grab_bbox(bbox)
            self.on_done(img)
        except Exception as ex:
            messagebox.showerror("pycapture", f"キャプチャ失敗: {ex}")

    def _cancel(self):
        try:
            self.top.destroy()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# グローバルホットキー (ctypes RegisterHotKey + 専用スレッドのメッセージループ)
# ---------------------------------------------------------------------------
MOD_ALT, MOD_CONTROL, MOD_SHIFT = 0x0001, 0x0002, 0x0004
WM_HOTKEY = 0x0312


class HotkeyThread(threading.Thread):
    """(mod, vk, label) のリストを登録し、押されたらqueueにlabelを流す。"""

    def __init__(self, hotkeys, out_queue):
        super().__init__(daemon=True)
        self.hotkeys = hotkeys
        self.q = out_queue
        self.ok = {}

    def run(self):
        for i, (mod, vk, label) in enumerate(self.hotkeys, start=1):
            res = user32.RegisterHotKey(None, i, mod, vk)
            self.ok[label] = bool(res)
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
            if msg.message == WM_HOTKEY:
                idx = msg.wParam - 1
                if 0 <= idx < len(self.hotkeys):
                    self.q.put(self.hotkeys[idx][2])


# ---------------------------------------------------------------------------
# GUI (司令塔)
# ---------------------------------------------------------------------------
class App:
    def __init__(self, root):
        self.root = root
        self.cfg = load_config()
        self.q = queue.Queue()

        root.title("pycapture")
        root.attributes("-topmost", True)
        root.resizable(False, False)
        pad = {"padx": 8, "pady": 4}

        frm = ttk.Frame(root, padding=12)
        frm.grid()

        ttk.Label(frm, text="保存先:").grid(row=0, column=0, sticky="w", **pad)
        self.dir_var = tk.StringVar(value=self.cfg["save_dir"])
        ttk.Label(frm, textvariable=self.dir_var, width=42,
                  relief="sunken", anchor="w").grid(row=0, column=1, **pad)
        ttk.Button(frm, text="変更", command=self.choose_dir).grid(row=0, column=2, **pad)

        ttk.Label(frm, text="接頭辞:").grid(row=1, column=0, sticky="w", **pad)
        self.prefix_var = tk.StringVar(value=self.cfg["prefix"])
        ttk.Entry(frm, textvariable=self.prefix_var, width=20).grid(
            row=1, column=1, sticky="w", **pad)

        ttk.Label(frm, text="形式:").grid(row=2, column=0, sticky="w", **pad)
        self.ext_var = tk.StringVar(value=self.cfg.get("ext", "png"))
        ttk.Combobox(frm, textvariable=self.ext_var, width=8, state="readonly",
                     values=["png", "jpg", "bmp"]).grid(
            row=2, column=1, sticky="w", **pad)

        btns = ttk.Frame(frm)
        btns.grid(row=3, column=0, columnspan=3, pady=(10, 4))
        ttk.Button(btns, text="範囲選択  (Ctrl+Alt+1)",
                   command=self.capture_region).grid(row=0, column=0, padx=4)
        ttk.Button(btns, text="全画面  (Ctrl+Alt+2)",
                   command=self.capture_full).grid(row=0, column=1, padx=4)
        ttk.Button(btns, text="ｱｸﾃｨﾌﾞ窓  (Ctrl+Alt+3)",
                   command=self.capture_active).grid(row=0, column=2, padx=4)

        foot = ttk.Frame(frm)
        foot.grid(row=4, column=0, columnspan=3, pady=(4, 0), sticky="we")
        ttk.Button(foot, text="フォルダを開く",
                   command=self.open_folder).grid(row=0, column=0, padx=4)

        self.status = tk.StringVar(value="待機中")
        ttk.Label(frm, textvariable=self.status, foreground="#2a6",
                  anchor="w").grid(row=5, column=0, columnspan=3,
                                   sticky="we", pady=(8, 0))

        # ホットキー登録
        hotkeys = [
            (MOD_CONTROL | MOD_ALT, 0x31, "region"),  # Ctrl+Alt+1
            (MOD_CONTROL | MOD_ALT, 0x32, "full"),    # Ctrl+Alt+2
            (MOD_CONTROL | MOD_ALT, 0x33, "active"),  # Ctrl+Alt+3
        ]
        self.hk = HotkeyThread(hotkeys, self.q)
        self.hk.start()
        self.root.after(300, self._check_hotkeys)
        self.root.after(50, self._poll_queue)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    # ---- 設定系 ---------------------------------------------------------
    def choose_dir(self):
        d = filedialog.askdirectory(initialdir=self.dir_var.get())
        if d:
            self.dir_var.set(d)
            self._persist()

    def open_folder(self):
        d = self.dir_var.get()
        Path(d).mkdir(parents=True, exist_ok=True)
        try:
            os.startfile(d)
        except Exception:
            subprocess.Popen(["explorer", d])

    def _persist(self):
        self.cfg.update({
            "save_dir": self.dir_var.get(),
            "prefix": self.prefix_var.get(),
            "ext": self.ext_var.get(),
        })
        save_config(self.cfg)

    # ---- 保存 -----------------------------------------------------------
    def _next_path(self):
        """日時秒ベースのファイル名を返す。同じ秒に連射したらミリ秒を付けて回避。"""
        d = Path(self.dir_var.get())
        d.mkdir(parents=True, exist_ok=True)
        prefix = self.prefix_var.get() or "capture"
        ext = self.ext_var.get() or "png"
        now = datetime.now()
        stamp = now.strftime("%Y%m%d_%H%M%S")
        p = d / f"{prefix}_{stamp}.{ext}"
        if p.exists():
            p = d / f"{prefix}_{stamp}_{now.microsecond // 1000:03d}.{ext}"
        return p

    def _save(self, img):
        p = self._next_path()
        ext = p.suffix.lower()
        if ext in (".jpg", ".jpeg"):
            # JPGは非可逆。劣化を最小化: 最高品質＋色間引きオフ(4:4:4)
            if img.mode != "RGB":
                img = img.convert("RGB")
            img.save(p, quality=100, subsampling=0)
        else:
            # PNG/BMPは可逆(劣化なし)。そのまま保存
            img.save(p)
        self.status.set(f"保存: {p.name}")
        return p

    # ---- キャプチャ 起動口 (すべてメインスレッドで実行) -----------------
    def capture_region(self):
        # 自GUIが写り込まないよう一瞬隠す
        self.root.withdraw()
        self.root.update()
        time.sleep(0.12)

        def done(img):
            self.root.deiconify()
            self._save(img)

        try:
            RegionSelector(self.root, done)
        except Exception as e:
            self.root.deiconify()
            messagebox.showerror("pycapture", str(e))

    def capture_full(self):
        self.root.withdraw()
        self.root.update()
        time.sleep(0.15)
        try:
            img = grab_fullscreen()
            self._save(img)
        finally:
            self.root.deiconify()

    def capture_active(self):
        # アクティブ窓を撮るので自GUIは隠さない（隠すと前面が変わるため）
        time.sleep(0.05)
        try:
            img = grab_active_window()
            self._save(img)
        except Exception as e:
            messagebox.showerror("pycapture", str(e))

    # ---- ホットキー → メインスレッドへ橋渡し ---------------------------
    def _poll_queue(self):
        try:
            while True:
                label = self.q.get_nowait()
                if label == "region":
                    self.capture_region()
                elif label == "full":
                    self.capture_full()
                elif label == "active":
                    self.capture_active()
        except queue.Empty:
            pass
        self.root.after(50, self._poll_queue)

    def _check_hotkeys(self):
        failed = [k for k, v in self.hk.ok.items() if not v]
        if failed:
            self.status.set(
                "注意: 一部ホットキーを登録できませんでした"
                "(他アプリが使用中)。ボタンからは使えます。")

    def on_close(self):
        self._persist()
        self.root.destroy()


def main():
    if not sys.platform.startswith("win"):
        print("このツールはWindows専用です。")
        return
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
