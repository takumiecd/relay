# 局所的な把持修正デモの探索（2026-11-02）

## 目的と採否

「VLAが対象へ近づく → 把持を失敗する → 短く修正する → VLAが残りを完了する」
という例を探す。最初から違う対象へ進む例、長い手動搬送が必要な例、返したVLAが
続けられない例は採用しない。これは公開用デモのケース選定であり、成功率の評価ではない。

PARCの過去の失敗記録は候補調査だけに使い、そのモデルと今回の公開モデルを同一視しない。
過去の教師データ・提出物・ラベルは変更しない。

## 固定した実行条件

- 公開 `openvla/openvla-7b-finetuned-libero-spatial`。
  weights revision: `962318cec55ac10993ff0f5f43eda9a270b4c873`。
  code revision: `47a0ec7fc4ec123775a391911046cf33cf9ed83f`。
- L4、BF16、SDPA、量子化なし。専用ワーカーのTorch 2.2.2+cu121。
- LIBERO-PROの標準 `libero_spatial`、MuJoCo 2.3.7、robosuite 1.4.1、
  OSC_POSE、20 Hz、256px。モデルへの画像はagentview一枚。
- モデル・前処理・タスクの指示を試行途中で変更しない。
- `output/grasp-screen-20261102/plan.json` を開始前に保存。
  task0/init0を陽性対照にし、task0/init1〜4、task5/init0、task4/init0、task3/init0の順。
  各経路180 action以内、修正試験は全体で最大2回。
- 一つの推論ワーカーを使い、場面の切り替えでは古いepochの指示を拒否する。
  操作者が制御を変えた場合は自動探索を中断し、操作を上書きしない。

## 失敗のスクリーニング

最初に触れた物体が対象の黒いボウルであること、対象付近で指を閉じたこと、
手先が2.5cm以上上がる一方で対象の上昇が1.5cm未満であることを候補条件にする。
これは接触・高さによる候補抽出であり、映像を確認せずに把持失敗と確定しない。
対象以外の物体について、開始位置からのL1変位が1mm以内かも記録する。

修正試験は開く3 action、XY合わせ5 action（各軸最大15mm、Zは合計5mm下げる）、
閉じる6 action、持ち上げる6 actionの最大20 action。運搬はしない。
**物体の位置情報を使う自動試験であり、人間のテレオペではない。**
生ログは `TEST_OPERATOR`、provenanceは `scripted_state_assisted_probe` として保存する。
その後に本物のVLA推論を再開する。

修正後のgoal成立、1mm guard、VLA再開後10 action以上、修正が経路の25%以下を
満たしたものだけを映像レビュー候補にする。全データは `training_admitted=false`。

## 再現性の確認

`scripts/verify_grasp_demo.py` で独立したシミュレータを作り、初期状態と
各action後の92要素の物理状態が記録と完全一致するかを確認する。
`sim.forward()` 前後でsuccess判定が変わる場合は別途記録し、rewardとgoalを混同しない。

陽性対照 task0/init0 はVLAのみ78 actionでreward=1、goal=true。
過去の対照と全78 actionが同一だった。独立再実行でも全ステップの物理状態が完全一致し、
最終reward=1、goal=true、guard=true、forward前後のsuccess差はなかった。

## 証拠の保存先

- 計画・進行・各ケース: `output/grasp-screen-20261102/`
- 陽性対照の再実行: `output/grasp-screen-20261102/positive-replay/verification.json`
- スクリーニング: `scripts/search_grasp_demo.py`
- 独立した再実行: `scripts/verify_grasp_demo.py`

モデルの推論結果・両カメラ・物理状態・操作元・分岐は保存する。
接続トークン、巨大な生記録、PARC由来の私的データは公開Gitに追加しない。

## 第1探索の結果

8配置、実推論840回。局所修正の採用例は0件で、修正試験も行っていない。
全配置で把持・搬送が進んだ。task3/init0では高さによる候補条件に一時的に該当したが、
VLAが自力でgoalに到達したため除外した。task0/init1は最終reward=0/goal=true、
task0/init4はreward=1/goal=falseだったため、確実な成功とも把持失敗とも数えない。
これらから公式ベンチマーク成功率を計算しない。

GPU job: `l4job-e91ed93ea8124272acd547b6baf79a51`。
取得済みのモデル情報・推論記録・検証receiptは
`output/grasp-screen-20261102/gpu-results/` に保存し、GPUは停止した。
探索中のソースも `output/grasp-screen-20261102/source/` に保存した。

## 第2探索の事前条件

`output/grasp-screen-20261102-v2/plan.json` に別の計画を保存した。
陽性対照の後に task0/init41、task5/init46、task4/init41、task7/init0、task9/init0 を試す。
前の記録・計画を上書きしない。

修正試験は接触付近の最下点へ戻し、開く6、位置合わせ5、閉じる8、上げる6の
最大25 actionにする。ボウル中心からの手先目標オフセットは `[0, -0.045, 0.035]` m。
1 actionの位置合わせは各軸3mm以内で、搬送はしない。これは成功例を参考にした
ボウル専用の状態情報を使う試験であり、汎用自動修復や人間の操作とは主張しない。
修正割合は経路の30%以内、再開後VLAは10 action以上、最終reward=1とgoal=trueを要求する。

この探索はPARC公式評価ではないため、第2探索では1mm guardを採用の必須条件から外し、
変位量とguardの合否を引き続き保存する。非対象物が動いた結果をcleanと呼ばない。
映像と独立した再実行による検証を経るまでは、どの候補も採用済みとはしない。

## 採用できる例を確認

task0/init41で、ボウル付近まで進んでから指を早く閉じ、空の手で皿へ向かう例を確認した。
180 actionでgoalなし、対象への接触0回、対象の最大上昇は約0.032mm。
全180 actionを独立したシミュレータで再実行し、全ての92要素の物理状態が完全一致した。
対象に触れない空振りだったため、当初の「対象への接触」を必須にする条件では検出できなかった。
画面レビューで候補を選び、その後に検出条件も修正した。
修正後はこの空振りを検出し、陽性対照とtask5/init46の成功例は検出しないことを実記録で確認した。

追加探索は候補発見後に停止した。task4/init41の途中までの記録は手動中断であり、
モデルの失敗として扱わない。第2探索で自動修正プローブは実行していない。
別の校正試験では25 actionのプローブで上昇量が約2.5mmに留まり、
この自動プローブが修復に有効だとは確認できなかった。

専用の単一Runtimeでtask0/init41を新たに実行し、179 actionまでの推論結果と
各物理状態が、先の180 actionの記録の対応部分と完全一致することを確認した。
そこからstep38へ戻して、キーボードと同じ入力APIで局所修正した。

| 区間 | action数 | 確認した動き |
| --- | ---: | --- |
| VLAの共通部分 | 38 | 対象付近への接近 |
| エージェントによる修正 | 38 | 開く、位置合わせ、閉じる、約4.4cm持ち上げる |
| VLAの再開後 | 30 | 対象を約13.7cm移動し、皿でLIBERO goal判定に到達 |

採用経路は106 action、VLA計68、TEST_OPERATOR計38。
最終reward=1、goal=true、guard=true。
全106 actionの92要素の物理状態を独立した再実行で照合し、完全一致した。
`sim.forward()` 前後のsuccess判定差もなかった。
これは特定の1例を選んだデモであり、成功率向上の統計的評価ではない。

**修正はエージェントが状態情報も参考にして入力した試験であり、人間の操作実演ではない。**
生ログはTEST_OPERATORとして保持し、操作者・使用した入力・対応範囲を別途保存した。
最初の入力ツールが1 actionで中断した経路も削除せずに残し、採用経路には含めていない。
全データは `training_admitted=false`。
終了はLIBEROのgoal条件で判定しており、グリッパー解放後の安定性を独立検証したとは主張しない。

測定値の定義：接触0回は制御step末尾で記録したグリッパーと対象物の接触で、
物理substep全ての接触を監視した値ではない。約13.7cmはVLA再開時と終点の
ボウル位置の3次元距離であり、移動軌跡の弧長ではない。1mm guardは記録した
非対象3物体の位置のL1変位であり、回転角の制約ではない。
task0のgoalは`On(akita_black_bowl_1, plate_1)`で、SDKの接触・高さ・水平距離による配置条件。
グリッパー開放はgoal条件に含まれない。

検証要約: [grasp-repair-20261102.json](grasp-repair-20261102.json)。
記録と63秒の比較動画: `output/grasp-miss-repair-20261102/`。
GPU job: `l4job-55e44490b3e1439fa9a05132ad615c56`。
モデル情報・実推論209回の記録・検証receiptを取得し、GPU停止を確認した。

## 専用画面で同じ配置を開く

リポジトリのシミュレータ用Python環境から、未使用のoutputを指定する。
モデルワーカーの接続手順は既存のVLA接続ドキュメントと同じ。

```bash
PYTHONPATH=scripts python -m relay.server \
  --backend search_grasp_demo:create_probe_backend \
  --backend-config configs/relay/grasp-demo.json \
  --output output/grasp-demo-new \
  --port 8895 --vla-port 8903
```

人が操作する上のコマンドでは `--test-operator` を付けない。
今回のエージェントによる試験では、このフラグを付けて操作元を区別した。
公開モデルの同一出力を保証する一般的な条件とはせず、モデルrevision・seed・
前処理・SDKバージョン・実行ログも合わせて確認する。

## 探索画面の切り替え修正

探索ツールがConsole.runnerだけを差し替えると、GETの状態表示とPOSTの指示先が
別のRuntimeになっていた。ConsoleのHTTP handlerは生成時のRuntimeへ結び付くため、
ケースごとにConsoleも作り直すよう修正した。切り替え後は画面の再読み込みが必要。
同じportで二つのConsoleを順に起動し、現在のRuntimeへの操作・停止を確認した。
古いtokenが拒否されることも確認した。通常の単一RuntimeのConsoleは変更していない。
