# Genesis — portable VLA / LLM / operator control — 2026-11-02

対象：`feat/genesis-vla`、`feat/intervention-dataset`の`f27ecfa`を基点とする。

## 実装と契約

- `PolicyActions`をVLAの実行と、コードからの`run_vla`委譲の両方へ接続。
- native passthroughを既定値として維持。明示したWORLD delta契約だけを、実行直前の姿勢からbackendへ変換。
- chunk全体の形・有限値・並進／回転量・gripperを実行前に検査。
- モデル出力とbackend出力のaction_contractを別々に照合する。
- source policy action・変換規約・診断・その操作時点のモデル情報・実行したnative actionを対応させる。
- OpenVLAのカメラ・画像規約・出力契約を設定可能にし、OSC scaleとmotion gainを明示して復号。
- 指示欄をVLAとLLMで共有し、VLAへ実際に渡した指示も各操作へ保存。
- workerの正常終了／失敗時にdetachしてreadyを解除し、VLAモードを停止する。
  worker IDで、古いworkerの遅いdetach/errorが再接続したworkerを無効にしないよう照合する。
  異常な通信断は既存timeoutで扱う。heartbeatによる即時検知の実装ではない。

## 自動検査

- Genesis専用環境：policy conversion・Genesis backend・history・LLM・intervention **76 passed**、skipなし。
- LIBERO用環境：既存backend・boundary・VLA・hybridと新規検査 **98 passed, 2 skipped, 15 warnings**。
  skipは専用Genesis SDKが必要なケースで、上の実環境で実行した。
- 指示欄と各操作のモデル出所の追加後：policy／LLM検査 **33 passed, 2 skipped**。
- 最終版：Genesis専用環境 **78 passed, no skips**（指示・出所・切断時の残りchunk破棄も含む）。
- 最終版のLIBERO回帰：**100 passed, 2 skipped, 15 warnings**。
- JS：キーボード・分岐図 **12 passed**、console.js構文検査、notebookの全Pythonセル構文検査。
- worker ID追加後：新規policy検査 **28 passed**（Genesis実物理・実HTTP worker往復を含む）。
- 配布ZIPの全source hashと設定JSONの同梱、connection/tokenファイルを含まないことを確認。

検査内容：相対chunkを現在位置へ順次積算、WORLD回転の左合成、変換だけでは状態が進まないこと、
不正な後続actionを含むchunkの全体拒否、異なる契約の拒否、owner以外の変換拒否、
Pモードと`run_vla`の同じ変換、元の出力・native出力・指示・モデル情報の対応、
遅い返答の手動切替後の破棄、detachと再接続、実Genesisの移動と状態復元。

## 実OpenVLAの閉ループ接続

GPU job：`l4job-4ba9f575a09b4b2aa495b035ebe08632`。
NVIDIA L4 24GB上の専用Python 3.10.19 / Torch 2.2.2 / Transformers 4.40.1 / TensorFlow CPU 2.15.0。
poolホスト側Python 3.13・Torch 2.11と、モデルを動かした専用venvは別である。

モデルは`openvla/openvla-7b-finetuned-libero-spatial`、重みrevision
`962318cec55ac10993ff0f5f43eda9a270b4c873`、code revision
`47a0ec7fc4ec123775a391911046cf33cf9ed83f`。BF16・SDPA・seed7。
カメラはGenesisのoverview、`rgb_top_left`、center crop area 0.9。
[設定JSON](../../configs/relay/openvla-genesis.json)のscaleとgain0.2を使用。

GPU workerは、一時トンネル経由の認証付きPolicyBridgeへ接続した。
物理環境は手元のGenesis 1.4.5 / CPU / 20Hz / physics dt 0.01 / camera 128px。
毎回その時点の観測を送信し、**3回の推論と3回の物理実行**を確認した。
事前に保存した返答の再生や固定fixtureによるVLA動作ではない。

続けて同じsceneで、TEST_OPERATORとしてキー操作2stepを行い、
Mac上の実MLX Qwen2.5-VL-7B 4bitが生成した`close_gripper(steps=5)`を5step実行した。
合計**VLA 3＋TEST_OPERATOR 2＋CODE 5＝10step**の経路を採用し、コード候補1件を書き出した。
その後、初期checkpointへ戻り、`physics_state()`のベクトルが初期値とbitwise一致することを確認した。
カメラ画像のbitwise一致や、この試験単独で全内部SDK配列の一致を主張するものではない。

VLAの最初の出力は、正規化された先頭のXYZが
`[0.90780047,0.34228993,-0.09066177]`、物理deltaが
`[0.00907800,0.00342290,-0.00090662] m`。
このdeltaを、その時点の測定姿勢からGenesisの8D絶対目標へ変換して実行した。
残りの出力と実際のnative指令も全て保存している。

- [結果JSON](artifacts/genesis-vla-20261102/result.json)：実モデル情報、元の出力、変位、native指令、出所、GPU job source digest。
- `output/genesis-vla-qa-20261102/`：raw観測・物理状態・step・採用経路・生成コード・スクリーンショット。
- pool job directory：固定source、GPU実行結果、専用環境freeze、予測ログ、hash検証済みreceipt。

巻き戻すとliveのtrace/cursorは0になるため、検証JSONのtraceは不変のraw steps.jsonlから読み出した。
10stepの実行記録と、復元後の現在位置を混同しない。
`policy_model`を各操作に追加する変更と指示欄の追加は、この初回ライブ試験後に自動テストで確認した。
ライブ試験時のモデル出所は保存したpolicy.jsonと、各stepのpolicy_diagnosticsで確認できる。

## 終了と範囲

GPU workerはbridgeのcloseを受けて終了し、結果を回収してL4の停止を確認済み。
一時的な推論トンネル・QAのownerサービス・接続情報も終了／無効化した。
常設の操作画面で再度VLAを使う場合は、ノートブックからworkerを接続し直す。
LLMと手動は独立して使用できる。

この試験は**異なる制御形式の接続と三者切替**を確認するもので、
Genesisへのpolicy適応・箱の把持・言語指示の達成・タスク成功率・性能測定ではない。
LIBEROで学習したcheckpointを、異なる画像・物理・制御gainで使っている。
全候補は`training_admitted=false`、代理キー操作はHUMANにしない。
Genesis SDKの再起動をまたいだ復元や、関節指令を出す別VLAの復号は引き続き別作業である。

## 利用用画面

8878の0step・STOPPED・推論／コード実行なしを確認し、前セッションを保存して更新した。
利用用は`output/genesis-three-way-user-20261102-02/`、VLA bridgeはloopbackの8898。
WORLD deltaの接続を待ち受け、LLMは既存のMac上MLX APIへ接続する。
GPU workerは試験後に停止しているため、現在のVLAボタンは接続待ちである。
実VLAを再開するときは`notebooks/vla_worker.ipynb`のGenesis設定で接続する。
利用者の手動入力はHUMAN、QAの代理キー入力はTEST_OPERATORのまま区別する。

## マージ前レビュー（2026-11-02）

worker交代時の結果の競合を追加修正した。旧workerのpollをworker IDで拒否し、
error時は保留中の要求／結果を破棄する。新しいpolicy metadataが届いたときにVLA実行中または委譲中なら停止し、
旧workerの返答が既に受信済みでも実行しない。
新規の再接続・poll検査と既存のGenesis統合を合わせて **82 passed, no skips**。
