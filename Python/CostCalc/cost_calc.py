import tkinter as tk
from tkinter import ttk
from datetime import datetime


def calc_cost():
    """入力された分と円から1分あたりの価格を計算してログに出力する"""
    try:
        minutes = float(entry_minutes.get().replace(",", ""))
        yen = float(entry_yen.get().replace(",", ""))
    except ValueError:
        write_log("エラー: 分と円は数値で入力してください")
        return

    if minutes <= 0:
        write_log("エラー: 分は0より大きい値を入力してください")
        return

    per_min = yen / minutes
    write_log(f"{minutes:g}分 / {yen:g}円 → 1分あたり {per_min:.3f}円 (約{per_min:.1f}円/分)")


def write_log(text):
    """ログ画面に時刻付きで1行追加する"""
    now = datetime.now().strftime("%H:%M:%S")
    log.configure(state="normal")
    log.insert("end", f"[{now}] {text}\n")
    log.see("end")
    log.configure(state="disabled")


def clear_log():
    log.configure(state="normal")
    log.delete("1.0", "end")
    log.configure(state="disabled")


root = tk.Tk()
root.title("コスト計算")
root.geometry("460x320")

frame = ttk.Frame(root, padding=10)
frame.pack(fill="both", expand=True)

# 入力欄
entry_minutes = ttk.Entry(frame, width=12)
entry_minutes.grid(row=0, column=0, padx=(5, 0), pady=5)
ttk.Label(frame, text="分").grid(row=0, column=1, sticky="w", padx=(2, 10), pady=5)

entry_yen = ttk.Entry(frame, width=12)
entry_yen.grid(row=0, column=2, padx=(5, 0), pady=5)
ttk.Label(frame, text="円").grid(row=0, column=3, sticky="w", padx=(2, 10), pady=5)

# ボタン
ttk.Button(frame, text="コスト", command=calc_cost).grid(row=0, column=4, padx=5)
ttk.Button(frame, text="ログクリア", command=clear_log).grid(row=0, column=5, padx=5)

# ログ画面
log = tk.Text(frame, height=12, state="disabled", wrap="word")
log.grid(row=1, column=0, columnspan=6, sticky="nsew", pady=(10, 0))
scroll = ttk.Scrollbar(frame, command=log.yview)
scroll.grid(row=1, column=6, sticky="ns", pady=(10, 0))
log.configure(yscrollcommand=scroll.set)

frame.rowconfigure(1, weight=1)
frame.columnconfigure(5, weight=1)

# Enterキーでも計算
root.bind("<Return>", lambda e: calc_cost())

entry_minutes.focus()
root.mainloop()
