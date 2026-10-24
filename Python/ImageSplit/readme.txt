# 動画全体から均等に5枚抜く（参考画像を見せる用途に最適）
python extract_frames.py input.mp4 --count 5

# 2秒ごとに1枚
python extract_frames.py input.mp4 --interval 2

# 毎秒1枚
python extract_frames.py input.mp4 --fps 1

