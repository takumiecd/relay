# LLMと手動修正から採用経路の学習候補を作る

[GenesisのLLM操作](genesis-llm.md)に続き、生成コードの途中で引き継ぎ、
巻き戻して別の操作で続け、採用した経路を保存できる。
採用経路にはstep単位のVLA候補と、完了したプログラム単位のVLM-code候補を両方書き出す。

## 操作の流れ

1. LLMへの指示を入力し「指示を実行」。
2. Hまたは手動ボタンで引き継ぐ。停止だけならEsc。
3. Space＋WASD/RF等で操作する。
4. 必要なら記録のstepへ戻り、別の操作で続ける。元の未来は別経路に残る。
5. LLMへの新しい指示で再開する。生成時の画像・状態・指示を新しく取得する。
6. 経路を選び「この経路を採用」。採用した終点までが固定される。

入力欄の編集中はH/Lを文字として扱う。切り替えはモードのボタンを使うか、入力欄からフォーカスを外す。
実際の人のキー入力はHUMAN、代理操作の検証ではTEST_OPERATORとして区別する。
経路を選ぶことと、学習に投入してよいと認定することは別である。

## 保存先

各試行の`history/adoptions/<selection-id>/`に保存する。
`selection.json`のパスは、その試行のrootからの相対パス。
分岐を選んだ場合も、親経路の採用した先頭部分を含む。

| ファイル | 内容 |
|---|---|
| `route.jsonl` | 採用経路の全stepと、その元のtrajectory・step・出所 |
| `vla-candidates.jsonl` | 各stepの画像・状態・指示・実行したnative action・出所 |
| `vlm-code-candidates.jsonl` | 全実行stepを採用し、コード実行を完了したプログラムの画像・状態・指示・コード参照 |
| `program-coverage.jsonl` | 経路に含まれる各プログラムの採用範囲、実行結果、コード候補への出力／除外理由 |
| `selection.json` | 選択経路・終点・checkpoint・ファイルhash・コード候補件数 |

画面の「採用した経路」にコード候補の件数を表示する。
完了したプログラムがなければ0件になる。VLA候補はその場合も実行した各stepを含む。
新しいVLM-code出力は**採用経路用**であり、trajectory直下の既存のrawプログラム候補とは区別する。

## コード候補の条件

- `code.py`・`metadata.json`・`result.json`と、開始時の観測が存在する。
- そのプログラムが元のtrajectoryに記録した**全step**が、順序を保って採用経路に含まれる。
- 実行結果が`program_completed`である。
- プログラムの記録した実行actions数と、元のraw操作の件数が一致する。

中断コードの実行済み4stepを全部採用しても、コード全体を実行し終えたことにはならない。
また、完了した5stepのうち2stepだけ採用した場合、元の全コードをその2stepの正解として使わない。
これらは`program-coverage.jsonl`にそれぞれ`execution_not_completed`、
`partial_program_selected`等の理由を付けて残す。エラー・時間切れ・不足する成果物・件数の不一致も除外する。

親経路の完了プログラムも対象にし、対応する画像・観測・コードは元の記録を参照する。
`run_vla`による委譲も元のVLA／TEST_VLAの出所を保つ。
手動の軌跡を意味的なPythonコードへ推測変換する処理は追加していない。

`program_completed`はPythonの実行が終わったことを示す。
目標位置への到達、把持成功、指示に沿った操作や良い教師データであることは保証しない。
全出力は`training_admitted=false`、コード候補は`quality_review_required=true`で保存する。
後の操作・別の採用・分岐で、既に保存した採用経路のファイルと終点は書き換えない。

[実モデルとGenesisでの検証](verification/intervention-dataset-20261102.md)を参照。
