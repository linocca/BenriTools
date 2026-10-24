#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
extract_frames.py

動画から静止画(JPG)を抜き出すツール。ffmpeg / ffprobe を使用。

使い方の例:
    # 動画全体から均等に 5 枚抜く（参考画像を数枚見たいとき向け）
    python extract_frames.py input.mp4 --count 5

    # 2 秒ごとに 1 枚抜く
    python extract_frames.py input.mp4 --interval 2

    # 毎秒 1 枚（fps 指定）
    python extract_frames.py input.mp4 --fps 1

    # 出力先とJPG画質を指定
    python extract_frames.py input.mp4 --count 8 --outdir frames --quality 2

モードは --count / --interval / --fps のどれか1つ。
指定が無ければ --count 10（全体から10枚均等）で動きます。
"""

import argparse
import subprocess
import sys
from pathlib import Path


def get_duration(video_path: Path) -> float:
    """ffprobe で動画の長さ（秒）を取得する。"""
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(video_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe に失敗しました:\n{result.stderr}")
    try:
        return float(result.stdout.strip())
    except ValueError:
        raise RuntimeError("動画の長さを取得できませんでした（対応していない形式かもしれません）。")


def extract_by_count(video_path: Path, outdir: Path, count: int, quality: int) -> list[Path]:
    """動画全体から count 枚を均等な時刻で抜き出す（seek方式で高速）。"""
    duration = get_duration(video_path)
    saved = []
    # 端すぎる時刻（真っ黒フレーム等）を避けるため、区間の中央寄りに配置する
    for i in range(count):
        # (i + 0.5) / count で 0〜1 を均等分割した中点を取る
        t = duration * (i + 0.5) / count
        out = outdir / f"frame_{i + 1:03d}.jpg"
        cmd = [
            "ffmpeg", "-y",
            "-ss", f"{t:.3f}",      # -i の前に置くと高速seek
            "-i", str(video_path),
            "-frames:v", "1",
            "-q:v", str(quality),   # JPG画質（2=高品質, 31=低品質）
            str(out),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            print(f"  警告: {t:.2f}s のフレーム抽出に失敗しました", file=sys.stderr)
            continue
        saved.append(out)
        print(f"  抽出: {out.name}  (t={t:.2f}s)")
    return saved


def extract_by_rate(video_path: Path, outdir: Path, fps_expr: str, quality: int) -> list[Path]:
    """fps 指定で連番抽出する。fps_expr は '1' や '1/2'（2秒に1枚）など。"""
    pattern = outdir / "frame_%04d.jpg"
    cmd = [
        "ffmpeg", "-y",
        "-i", str(video_path),
        "-vf", f"fps={fps_expr}",
        "-q:v", str(quality),
        str(pattern),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg に失敗しました:\n{result.stderr}")
    saved = sorted(outdir.glob("frame_*.jpg"))
    for p in saved:
        print(f"  抽出: {p.name}")
    return saved


def main() -> int:
    parser = argparse.ArgumentParser(
        description="動画から静止画(JPG)を抜き出す（ffmpeg使用）。",
    )
    parser.add_argument("video", help="入力動画ファイル（mp4など）")
    parser.add_argument("--outdir", default="frames", help="出力フォルダ（既定: frames）")
    parser.add_argument("--quality", type=int, default=2,
                        help="JPG画質 2〜31。小さいほど高品質（既定: 2）")

    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--count", type=int,
                      help="動画全体から均等にこの枚数を抜く")
    mode.add_argument("--interval", type=float,
                      help="この秒数ごとに1枚抜く（例: 2 で2秒ごと）")
    mode.add_argument("--fps", type=float,
                      help="1秒あたりこの枚数を抜く（例: 1 で毎秒1枚）")

    args = parser.parse_args()

    video_path = Path(args.video)
    if not video_path.is_file():
        print(f"エラー: 動画が見つかりません: {video_path}", file=sys.stderr)
        return 1

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    print(f"入力  : {video_path}")
    print(f"出力先: {outdir.resolve()}")

    try:
        if args.interval is not None:
            print(f"モード: {args.interval}秒ごとに1枚")
            saved = extract_by_rate(video_path, outdir, f"1/{args.interval}", args.quality)
        elif args.fps is not None:
            print(f"モード: 毎秒{args.fps}枚")
            saved = extract_by_rate(video_path, outdir, str(args.fps), args.quality)
        else:
            count = args.count if args.count is not None else 10
            print(f"モード: 全体から{count}枚を均等抽出")
            saved = extract_by_count(video_path, outdir, count, args.quality)
    except RuntimeError as e:
        print(f"エラー: {e}", file=sys.stderr)
        return 1

    print(f"\n完了: {len(saved)}枚を保存しました → {outdir.resolve()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
