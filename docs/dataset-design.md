# 目的：VLA・Code as Policy・人の操作から、共通の学習素材を作る

2026-11-02の方針。目的は制御主体を切り替えられることだけでなく、修正と再実行を通して
**観測・実行済み操作・プログラム・出所を対応付けた記録を残すこと**。
最初の実環境はCaP-XのLIBERO-PRO/OSC。モデルの接続はVLA共通adapterで行う。

## 通常のデータとの関係

[DROID](https://droid-dataset.github.io/)は外部画像・手首画像・言語指示・actionなどを
stepごとに対応付ける。[OpenVLA](https://openvla.github.io/)は画像と言語からactionを出す。
このような実行済みの時系列をまず保存することが、今回のVLA向けの基礎になる。

[Code as Policies](https://code-as-policies.github.io/)では、言語と知覚情報を使って
操作APIを呼ぶプログラムを生成する。[CaP-X](https://capgym.github.io/)もコードを実行して
ロボットを操作する。コード生成用の教師には、観測／指示と対応するプログラムが必要。
単に数値actionをPython配列へ書いたものを、意味のあるプログラムと同じ教師にはしない。

今回は一つの元記録から、以下の用途別の候補を派生させる。

| 用途 | 入力 | 教師／ラベル |
|---|---|---|
| VLA | 画像・指示・必要なロボット状態 | 実際に適用されたaction／action chunk |
| コード生成VLM/LLM | 画像／認識結果・指示・API仕様・実行履歴 | 実行したプログラム、または再実行して確認した要約プログラム |
| 判断・説明用VLM | 画像・指示・過去の操作 | 人が確認した説明、成功／失敗、修正理由 |

最後の二つに必要な説明や意味は、数値軌道だけでは一意に決まらない。
LLM/VLMで候補を付ける場合も、その由来と検証結果を元データから分離する。

## 二つのコード化

### 数値再生：現在の実装

実行済みaction列を`replay_actions(...)`という有限のCaP-Xコードへ変換できる。
同じ制御契約と初期simulator stateを確認してから再実行する。
VLAの出力でも、人の操作でも、同じnative actionを記録できれば同じ方式を使える。
再生は`CODE_REPLAY`、元の主体は`provenance.original_source`として保存する。
生成・再生した入力を新しい人間の教師操作とは扱わない。

これは特定の初期状態での動きの再現であり、対象物の位置が変わっても動く
汎用的なpolicyを発見する処理ではない。1回の`replay_actions`は最大100 actionで、長い記録は複数の有限呼出しへ分割する。

### 意味のあるプログラム：次段階

たとえば「器へ近づく→つかむ→持ち上げる」という区間と目標を推定し、
観測API・手先移動・gripper・`run_vla`を使ったコード候補へする。
数値再生だけの記録とは別に保存し、元軌道と対応する区間、生成モデル、
生成prompt、実行結果を持つ。成功例での再実行と、初期条件を変えた試験を分ける。
この要約／プログラム推定とVLM学習そのものはまだ実装していない。

## 実行済みの共通記録：現在の実装

`TrajectoryRecorder`を環境へ接続すると、以下を保存する。

- `episode.json`：schema、指示、環境・controller、モデル等のmetadata、採用前フラグ。
- `observations/`：両カメラのraw RGB画像、関節・手先・gripper状態、観測ID、sim状態。
- `steps.jsonl`：前後の観測、適用action、出所、epoch、報酬、終了情報、出所の補足。
- `vla-candidates.jsonl`：画像・指示・状態とactionを対応付けた候補。
- `replay.py`と`code-candidate.json`：数値再生用コードと、その種類・由来。

exportは学習formatを固定する前の共通JSONであり、RLDSやLeRobot用の完成datasetではない。
画像pathは記録directory基準。元の画像conventionとaction契約を保存し、モデル用の
回転・crop・正規化は学習adapterで行う。報酬／成功フラグと、人による品質の採用判断は別である。

記録する場合：

```bash
.local/openvla-sim/bin/python -m capx.extensions.vla.demo \
  --record-dataset --max-actions 30 --output output/recorded-vla
```

実行済みの古いaction traceから再生とコード化を確認する場合：

```bash
.local/openvla-sim/bin/python -m capx.extensions.vla.replay \
  --source output/vla-live-02/actions.json --output output/vla-to-code
```

古いtraceに元の画像やfull simulator stateが無ければ、それらが検証済みとは扱わない。
再構成した観測は`reconstructed_by_numerical_replay`と明記する。
full state付きの記録を再生し、各stepの物理状態も照合する場合：

```bash
.local/openvla-sim/bin/python -m capx.extensions.vla.replay --trajectory \
  --source output/vla-to-code/trajectory --output output/verified-code-replay
```

常に`training_admitted=false`で書き出す。データの採用・品質チェック・episode単位のsplit・
LeRobot/RLDSへの変換は次段階であり、この段階の不完全な接続試験を教師データとして採用しない。

## 切替と巻き戻し

[hybrid-console.md](hybrid-console.md)のconsoleで切替・停止と出所の記録を実装した。
step指定での巻き戻し・前送り・分岐記録も実装した。採用の判断は次段階。

同じ物理環境の唯一のstep入口へ、VLA・生成コード・人の操作を接続する。
主体の切替ではepochを更新し、前の主体の推論結果と残ったaction chunkを捨てる。
`HUMAN`は実際の人の入力だけに使い、TEST、VLA、CODE、CODE_REPLAYと区別する。

巻き戻しは既存の記録を上書きせず、checkpointから新しいbranchを作る。
parent branchと復帰stepを保存する。採用区間・介入理由・採用者を指定する機能は今後追加する。
人の修正区間だけを取り出す場合も、その直前の観測・引継ぎ元・採用判断を保持する。
branchを選んで途中の物理状態から再開できる。採用と、人の実操作による修正デモは次の段階である。
キーボードUIと標準gamepad mappingは実装したが、実ゲームパッドはまだ未検証。

## 実装する順番

1. VLA共通接続＋最初の実モデルの物理動作（確認済み）。
2. 実行済み記録と数値コード化、出所を保持した再生確認（今回追加）。
3. LLMコード・VLA・人の切替と停止、キーボード入力（実装・試験済み）。実モデル同時接続・実ゲームパッドは次の検証。
4. 巻き戻しと分岐記録（実装・試験済み）。採用branchと実際の人による修正デモは次の検証。
5. 品質を確認した採用datasetとformat変換、意味のあるコード候補の生成・検証。
