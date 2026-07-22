<p align="center">
  <strong>Language / 语言 / 言語</strong><br>
  <a href="README.md">简体中文</a> ·
  <a href="README.en.md">English</a> ·
  <strong>日本語</strong>
</p>

# LaTeX Word Review

LaTeX で書いた論文を Word で見てもらい、返ってきた修正を確認しながら新しい LaTeX コピーへ戻す Windows 用ツールです。

## こんな場面のために作りました

論文の著者は LaTeX を使っているのに、指導教員や共同執筆者は Word の変更履歴とコメントを使いたい。これは珍しくありません。

LaTeX から Word を作るだけなら方法はあります。困るのは、その Word が修正されて戻ってきた後です。手作業で転記すると変更を見落としやすく、Word 全文を LaTeX に変換し直すと、数式、引用、ラベル、マクロ、投稿用テンプレートを壊すおそれがあります。

LaTeX Word Review は、この戻り道を扱います。LaTeX を正本のまま保ち、Word は査読と共同編集のための受け渡しファイルとして使います。

## このプロジェクトでできること

- LaTeX プロジェクトから、編集可能な査読用 Word を作ります。
- 変更履歴とコメントを含む返却 Word を読み込み、変更前後の文、著者、時刻などを整理します。
- 変更を一件ずつ見て、採用、不採用、文言を直して採用、手動対応を選べます。
- 採用する変更を LaTeX に書く前に、ファイルごとの差分を確認できます。
- 確認後も元の LaTeX は変更せず、新しい LaTeX コピーだけを作ります。
- 修正済み LaTeX、見える差分、変更記録、監査用ファイルを同じタスクに残します。

これは、修正済み Word 全文を LaTeX に変換し直すツールではありません。安全な位置を確認できた変更だけを局所的に反映します。

## どんな人に向いているか

- 論文やレポートを LaTeX で管理している Windows ユーザー
- 指導教員、査読者、共同執筆者とのやり取りには Word を使いたい人
- 原稿を上書きせず、採用する変更を自分で決めたい人
- 誰が何を変え、どの判断をしたかを後から確認したい人

Word 上の見た目を LaTeX PDF と完全に同じにしたい場合や、複雑な Word 文書をワンクリックで完全な LaTeX に戻したい場合には向いていません。

## この方法の利点

| 利点 | 実際の動き |
|---|---|
| 元稿を守る | 元の LaTeX と返却 Word の原本には書き込みません |
| 自分で決める | Word の修正を一件ずつ採用または不採用にできます |
| 書き込み前に確認する | 承認と LaTeX への反映を別の操作にし、差分を先に表示します |
| 無理に推測しない | 位置を安全に特定できない変更は、記録に残して手動対応へ回します |
| 論文を外へ送らない | 通常のアプリ処理はローカルで動き、論文をクラウドへアップロードしません |
| 作業を再開できる | 最近のタスクから続けられ、不要なタスクは確認後に削除できます |

## 操作は 4 ステップです

1. **論文を選ぶ**
   Windows のファイル選択画面で、論文の主な `.tex` ファイルを選びます。ファイル名は `main.tex` でなくても構いません。
2. **査読用 Word を作り、返却稿を読み込む**
   作成された `review.docx` を相手へ渡します。相手には Word の「校閲」から変更履歴を有効にしてもらい、戻ってきた `.docx` をアプリで選びます。
3. **変更を一件ずつ判断する**
   変更前後の文、著者、時刻、文脈、安全性を見ながら、採用、不採用、修正して採用、手動対応を決めます。
4. **差分を確認して結果を作る**
   LaTeX に入る変更だけを差分で確認します。もう一度明示的に確認すると、新しい LaTeX コピーと関連資料が作られます。

相手は別の Windows PC で Word を使っても構いません。変更履歴を有効にしたまま編集し、「すべての変更を反映」や位置確認用ブックマークの削除は避けてもらってください。

## Windows ですぐ試す

> [!IMPORTANT]
> GitHub の説明は中国語、英語、日本語で読めますが、**現在のアプリ画面は簡体中国語です**。英語 UI と日本語 UI はまだありません。

1. [GitHub のソース ZIP](https://github.com/JIE-jiee/latex-word-review/archive/refs/heads/main.zip) をダウンロードします。
2. エクスプローラーで ZIP を右クリックし、「すべて展開」を選びます。ZIP の中から直接起動しないでください。
3. 展開したフォルダーの `Start-Latex-Word-Review.cmd` をダブルクリックします。
4. 初回だけインターネットへ接続し、準備が終わるまで待ちます。準備後、既定のブラウザーでローカル画面が開きます。

Python、Git、uv を事前に用意する必要はありません。管理者権限も不要です。初回起動では、固定された Python と必要な依存関係を展開フォルダー内へ準備します。準備済みの環境が残っていれば、次回からは同じファイルをダブルクリックして起動できます。

現在公開しているものは、ソース ZIP から初回準備を行う起動方法です。署名済みインストーラーや正式な GitHub Release ではありません。Windows が警告を表示した場合は、ダウンロード元がこのリポジトリであることを確認してください。安全機能を無効にして第三者配布の同名ファイルを実行しないでください。

詳しい画面操作は[中国語 Windows 完全ガイド](docs/guide.zh-CN.md)を参照してください。英語の手順は[Windows Quick Start](docs/quick-start-windows.md)にあります。

## 始める前に知っておいてほしいこと

- 対応している OS は Windows です。Linux と macOS はサポート対象ではありません。
- 査読する相手には、変更履歴を記録できる Microsoft Word for Windows が必要です。
- 論文に `SEQ`、`REF`、`PAGEREF` などの Word フィールドがある場合、アプリを動かす PC にも Microsoft Word が必要です。
- 最終 PDF の生成には、その論文をコンパイルできる LaTeX 環境が必要です。変更表示 PDF には `latexdiff` も必要です。プログラムは Word や LaTeX 宏パッケージを黙ってインストールしません。
- 査読用 Word は編集しやすい受け渡し稿です。LaTeX PDF や投稿先テンプレートの見た目をそのまま複製するものではありません。
- 数式、引用、ラベル、構造変更、図表、移動、書式変更、複雑なコメントは記録できますが、自動反映ではなく手動対応になる場合があります。
- 返却 Word の変更を正しく認識できても、LaTeX 側の位置を安全に証明できなければ、自動反映できる件数が 0 になることがあります。その場合も変更は記録に残ります。
- 元の LaTeX を直接変更することはありません。結果は新しいコピーにだけ書きます。
- 現在は beta です。未知の互換性、レイアウト、性能上の問題が残っている可能性があります。実際の論文では必ずバックアップを保ち、生成結果を確認してください。

## 作成される主な結果

| 結果 | 用途 |
|---|---|
| `review.docx` | 指導教員や共同執筆者へ渡す査読用 Word |
| `revised-clean/` | 承認した安全な変更を入れた新しい LaTeX コピー |
| `latexdiff.tex` と `latexdiff.pdf` | 実際に入った LaTeX の変更を見える形で確認する派生資料 |
| `ledger.json` と `ledger.html` | Word の変更、判断、処理結果をまとめた記録 |
| `audit.zip` | 再確認や共有のためにまとめた監査資料 |

必要な外部ツールが見つからない場合でも、安全に作成できた LaTeX コピーや記録は残し、PDF など未生成の結果を明示します。見かけだけの成功にはしません。

## Vibe Coding から生まれたプロジェクトです

このプロジェクトは、実際の LaTeX と Word の共同作業で生じた困りごとを出発点に、メンテナー主導の Vibe Coding と OpenAI Codex との協働で作られました。

メンテナーが要件、方向、安全境界、公開の判断を担い、AI コーディング Agent が調査、実装、テスト、文書作成を支援しています。この開発方法を公開しているのは、AI を使ったことを品質保証の代わりにしないためです。現在、独立した第三者監査を受けた安定製品であるとは表明していません。

経緯と分担は[開発経緯と Vibe Coding の記録](docs/development-provenance.md)にまとめています。

## 不具合報告と参加

このプロジェクトは更新を続けています。再現可能な不具合は、個人情報や未公開論文を取り除いたうえで [GitHub Issues](https://github.com/JIE-jiee/latex-word-review/issues) へ投稿してください。コード、テスト、文書、互換性、安全性の改善は [Pull Requests](https://github.com/JIE-jiee/latex-word-review/pulls) で受け付けています。

参加前に [CONTRIBUTING.md](CONTRIBUTING.md) と [SECURITY.md](SECURITY.md) を確認してください。通常の Issue に実際の論文、返却 Word、変更記録を添付しないでください。

<details>
<summary><strong>開発者、監査担当者向けの技術資料</strong></summary>

### 現在の公開範囲

公開しているのはソースコード、テスト、ビルド手順、Windows CI の証拠です。ローカルではインストーラーとポータブル ZIP の候補を検証していますが、Windows 向け lxml ネイティブ依存のライセンス資料と再リンク手段の確認が完了していないため、バイナリは公開していません。詳細は [Windows バイナリライセンス監査](docs/reviews/windows-binary-license-audit-2026-07.md)を参照してください。

[![CI](https://github.com/JIE-jiee/latex-word-review/actions/workflows/ci.yml/badge.svg)](https://github.com/JIE-jiee/latex-word-review/actions/workflows/ci.yml)
[![Double-click bootstrap](https://github.com/JIE-jiee/latex-word-review/actions/workflows/windows-source-bootstrap.yml/badge.svg)](https://github.com/JIE-jiee/latex-word-review/actions/workflows/windows-source-bootstrap.yml)
[![Windows](https://img.shields.io/badge/platform-Windows-0078D4)](docs/compat/platform-support.md)
[![Python 3.12 | 3.13](https://img.shields.io/badge/python-3.12%20%7C%203.13-3776AB)](pyproject.toml)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)

### 技術資料

- [文書インデックス](docs/README.md)
- [高度な CLI と実行ディレクトリ](docs/reference/cli.md)
- [公開 E0 チュートリアル](docs/tutorial-public-e0.md)
- [脅威モデル](docs/security/threat-model.md)
- [対応プラットフォーム](docs/compat/platform-support.md)
- [上流プロジェクトの採用方針](docs/adr/0001-upstream-strategy.md)
- [Windows 製品設計](docs/adr/0003-windows-product-experience.md)
- [第三者ライセンス](THIRD_PARTY_NOTICES.md)

中核は独立した Python ライブラリと CLI です。Codex Skill は同じ CLI を呼び出す薄い案内役であり、利用者の承認や二回目の確認を代行しません。変換には既存の `tex2word`、PDF ページのプレビューには pypdfium2 と Pillow を利用し、このプロジェクトは変更記録、位置対応、項目別承認、局所反映、検証を担当します。

コードは [Apache License 2.0](LICENSE) で公開しています。

</details>
