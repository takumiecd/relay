# LLM・手動修正・分岐後の採用経路 — 2026-11-02

対象：`feat/intervention-dataset`。`feat/genesis-llm`の`fbab4f2`を基点とする。
PR #4のLLM接続に続く変更として、採用経路のVLM-code出力と中断／一部採用プログラムの監査を追加した。

## 実装

- `selected_program_views`が採用経路の各プログラムを元のraw stepと照合する。
- 親分岐も含め、全実行stepが採用され、実行結果が完了・実行件数が一致し、成果物と開始観測があるコードを候補へ出す。
- 中断・エラー・部分的な採用・件数不一致・不足する成果物を除外理由付きで残す。
- 既存のstep単位VLA候補と出所は維持し、コード候補の件数・出力と監査ファイルのhashを採用manifestへ追加。
- 画面の採用済み経路に、コード候補件数を表示する。
- 全て`training_admitted=false`。コード完了はタスク成功の認定ではない。

## 自動テスト

- 新規テスト：**8 passed**（Genesis 1.4.5の実CPU物理を含みskipなし）。
- Genesis履歴・LLM制御と新規テストの組み合わせ：**32 passed**（最後の親経路／委譲プログラムの検査追加前）。
- 既存backend・hybrid・VLAと新規テスト：**70 passed, 1 skipped, 15 warnings**。
  skipはそのLIBERO環境にGenesis SDKを入れていないためで、上の専用環境では実物理も実行した。
- JS：キーボード・分岐図 **12 passed**、console.js構文検査、git diff空白検査。

途中引き継ぎは、長いグリッパーのツール呼出しを4step目で中断して確認した。
全実行を採用した中断プログラムと、完了コードの一部だけを採用したケースの両方を検査した。
元のraw未来と、先に固定した採用結果が、後の追記・別の採用で変化しないことも確認した。
不足するresult／開始観測、program_error、actions数の不一致、親と子の開始観測の取り違え、
委譲したTEST_VLAの出所の保持も検査した。

## 実モデル＋Genesisでの流れ

モデルは起動中のMLX HTTP APIを利用した実際の
`mlx-community/Qwen2.5-VL-7B-Instruct-4bit`。2回とも画像2枚・状態・操作指示からコードを生成した。
Genesis 1.4.5 / CPU / camera 128px / 20Hz。物理操作はowner thread、生成は別thread。
代理キー入力は全て`TEST_OPERATOR`で記録し、人間のデモと偽らない。

1. LLMが`open_gripper(steps=40)`→`close_gripper(steps=40)`のコードを生成。
2. 最初のツールの4step目で制御を引き継ぎ、program resultを`interrupted`として保存。
3. 代理キー操作で3step進め、元の経路を7stepまで記録。
4. step4へ巻き戻し、別方向の代理キー操作を2step行い、分岐2を作る。
5. 新しい観測から、LLMが`close_gripper(steps=5)`を生成。5step実行してコード完了。
6. 分岐2のstep11までを採用。

結果：

- rawは元の7step＋子の7step＝**14step**を保存。
- 採用経路は共有先頭のCODE 4step＋TEST_OPERATOR 2step＋CODE 5step＝**11step**。
- 採用しなかった元のキー操作3stepは、採用データから外れ、元のraw記録には残る。
- VLA候補は11件。VLM-code候補は完了した再開後のコード**1件**。
- 中断したコードは監査に残り、全実行済み4stepが選ばれても、コード全体の正解候補にはしない。
- 開始画像・状態・操作指示・モデル・コード／metadata／resultの参照とhashを保存。

実出力：[result.json](artifacts/intervention-dataset-20261102/result.json)。
元の全データは`output/genesis-intervention-qa-20261102/`、スクリーンショットは同directoryの`console.png`。
ブラウザで分岐図、採用した経路、「コード候補1件」、再開後の生成コードを確認。console errorは0。
QA用の8879サービスは保存して停止した。

## 利用用セッション

8878は利用者の試行2・0step・STOPPED・推論／プログラムなしを確認してから保存し、新実装へ更新した。
直前の100stepの試行1と、空の試行2の終了記録は`output/genesis-llm-user-20261102/`に保持。
新しい利用用セッションは`output/genesis-dataset-user-20261102/`、URLは同じ8878。
利用者の今後の手動入力はHUMANとして記録する。

## 残る範囲

- 実際の人による修正の品質評価、目標到達や把持の成功評価は行っていない。
- 自動で学習へ投入しない。完了コードも意味の誤りを含みうる。
- 手動やVLAの低レベルaction列から意味のあるコードを推測生成していない。
- Genesis用の学習済みVLA接続は別の作業。今回はLLM／手動の接続とデータの対応を扱う。
- 既存trajectory直下のraw候補ではなく、採用経路用の新しいVLM-code出力を対象とする。
