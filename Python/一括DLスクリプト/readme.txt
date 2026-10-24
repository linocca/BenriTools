python download_pdfs.py "https://school.js88.com/scl_dai/center_data/question?year=2020" -o D:\大事\大学共通試験過去問\令和2年\問題と解答
python download_pdfs.py "https://school.js88.com/scl_dai/center_data/question?year=2019" -o D:\大事\大学共通試験過去問\平成31年\問題と解答
python download_pdfs.py "https://school.js88.com/scl_dai/center_data/question?year=2018" -o D:\大事\大学共通試験過去問\平成30年\問題と解答
python download_pdfs.py "https://school.js88.com/scl_dai/center_data/question?year=2017" -o D:\大事\大学共通試験過去問\平成29年\問題と解答
python download_pdfs.py "https://school.js88.com/scl_dai/center_data/question?year=2016" -o D:\大事\大学共通試験過去問\平成28年\問題と解答


Get-ChildItem -Path "D:\大事\大学共通試験過去問\令和2年\問題と解答" -File | Where-Object { $_.Extension -eq "" } | Rename-Item -NewName { $_.Name + ".pdf" }
Get-ChildItem -Path "D:\大事\大学共通試験過去問\平成31年\問題と解答" -File | Where-Object { $_.Extension -eq "" } | Rename-Item -NewName { $_.Name + ".pdf" }
Get-ChildItem -Path "D:\大事\大学共通試験過去問\平成30年\問題と解答" -File | Where-Object { $_.Extension -eq "" } | Rename-Item -NewName { $_.Name + ".pdf" }
Get-ChildItem -Path "D:\大事\大学共通試験過去問\平成29年\問題と解答" -File | Where-Object { $_.Extension -eq "" } | Rename-Item -NewName { $_.Name + ".pdf" }
Get-ChildItem -Path "D:\大事\大学共通試験過去問\平成28年\問題と解答" -File | Where-Object { $_.Extension -eq "" } | Rename-Item -NewName { $_.Name + ".pdf" }


python count_print_pages.py "D:\大事\大学共通試験過去問\令和8年\問題"






# PDF（デフォルト）
python download_pdfs.py "URL" -o ./downloads

# 画像もまとめて
python download_pdfs.py "URL" -o ./images -e jpg,jpeg,png,gif

# 動画
python download_pdfs.py "URL" -o ./videos -e mp4,mov,webm

# 複数種類を一気に
python download_pdfs.py "URL" -o ./all -e pdf,jpg,png,mp4

# フォルダ内のファイルに拡張子を付与する
Get-ChildItem -Path "C:\フォルダのパス" -File | Where-Object { $_.Extension -eq "" } | Rename-Item -NewName { $_.Name + ".pdf" }

# フォルダ指定で、フォルダ内にあるpdfファイルを印刷するコマンド
Get-ChildItem -Path "C:\フォルダのパス" -Filter *.pdf | ForEach-Object {Start-Process -FilePath $_.FullName -Verb Print}

# 印刷枚数が事前にわかったりするコマンド
python count_pages.py ""

