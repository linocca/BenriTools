# お買い物リスト (Android / Kotlin + Jetpack Compose)

## 使い方(アプリ)
- 右上の ＋ で入力画面が出る → 品名を入れて「追加」(キーボードのEnterでも追加できます)
- 追加した順にリストの下へ並ぶ
- 各行の「削除」でリストから消える
- リストは端末内に保存され、アプリを閉じても残る

## ビルド方法
1. Android Studio (Koala 以降推奨) で、このフォルダを「Open」する
2. Gradle Sync が終わるまで待つ(Gradle Wrapper は Android Studio が自動で用意します)
3. 実機(USBデバッグON)またはエミュレータを選んで ▶ Run

## 動作条件
- Android 8.0 (API 26) 以上
- JDK 17 (Android Studio 同梱のものでOK)
