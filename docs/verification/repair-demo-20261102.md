# 実OpenVLA・代理キー操作・巻き戻し・採用経路の実演

2026-11-02、`feat/repair-demo`。RuntimeのsourceはPR #10の`3a0b6c5`。
既存ユーザーsessionへ介入せず、専用LIBERO scene・console 8891・bridge 8899で実施した。
元の提出物・動画・タグは上書きしない。

## 何を撮影したか

VLAが操作し、途中へ戻ってキーボードで引き継ぎ、VLAへ返し、その経路をデータ候補として採用した。
最初の自律試行は成功したため、**AIの失敗からの回復を実証した映像ではない**。
全キー入力はagentがブラウザを操作したもので、`TEST_OPERATOR`。実ユーザーの教師データとは扱わない。

| 経路 | 実行 | 結果 |
|---|---|---|
| 元の経路 0001 | 実VLA 78step | reward=1、終了時task_completed=true |
| 採用経路 0002 | 元の51step＋TEST_OPERATOR 73step＋実VLA 22step | 146step、reward=1・terminated=true、再計算後task_completed=false |
| 別試行 0003 | 元の51step＋TEST_OPERATOR 33step＋実VLA 27step | 111step、同じ終了時の判定差。未採用で保持 |

分岐図のstep 51をクリックして復元した。step 50のmarkerは表示されておらず、当初のクリック要求は成立しなかった。
実際の介入地点は51。採用経路の手動区間は速度10%、Space＋Rで上昇、Space＋Wで移動。
手先の始点終点の距離は約6.06cm。元の78stepの経路と別試行は保持した。
LLM・固定TEST_VLA provider・生成Pythonコードはこの撮影では使用していない。

## モデルとGPU

- NVIDIA L4。共有pool job `l4job-ac4a5610d111408e9e9ca62eaf60f84c`。
- `openvla/openvla-7b-finetuned-libero-spatial`、重みrevision `962318cec55ac10993ff0f5f43eda9a270b4c873`。
- code revision `47a0ec7fc4ec123775a391911046cf33cf9ed83f`。
- BF16、SDPA、量子化なし、seed7、Torch 2.2.2+cu121の専用環境。
- `libero_spatial` task 0、init 0、20Hz、相対7D OSC、256pxの全体・手首画像。
- 3経路を通じて実推論127回。採用経路に含む実VLA操作は73step。

poolホストのTorch 2.11／Python 3.13と、実モデルの専用venvは異なる。
source snapshotと回収結果はpoolのhash検証付きreceiptで確認した。
workerの終了・回収後、所有L4 slotのstoppedを確認。一時トンネルと撮影用consoleも停止した。

## 再実行と接触判定の差

親のstep 51と、分岐初期状態の92要素physics_stateがarray-equal。
さらに、採用経路の146指令を新しいLIBERO環境で再実行し、各指令後の92要素の状態が全てarray-equalになった。
SDKの全内部配列やcamera画像のbitwise一致を主張するものではない。

介入経路の最後は環境stepがreward=1・terminated=trueを返す一方、
backendの観測更新で`sim.forward()`した直後の成功判定はfalse。
同じ差を再実行でも確認し、before/after forwardで以下を記録した。

```text
success: true → false
qpos: array-equal
接触数: 75 → 73
```

この診断は、成功判定に使われる接触が再計算で変わることを示す。
安定した配置の成立や、上流SDK全般の不具合を証明するものではない。
元の結果やsuccessラベルを都合よく書き換えず、映像・説明・JSONへ差を残した。
全候補は`training_admitted=false`。把持・配置の品質確認と学習投入は別工程である。

再現用scriptは専用の出力先で動かす。今回と同じSDK・task 0／init 0の記録を前提とする。

```bash
.local/openvla-sim/bin/python scripts/verify_repair_demo.py \
  --capture output/relay-repair-demo-20261102 \
  --output output/repair-replay-fresh
```

## 動画と保存物

- ローカル動画：`output/relay-repair-demo-20261102/deliverables/Relay_rewind_and_repair.mp4`。
- 93秒、1920×1080、H.264、12fps、無音。10区間の代表frameを目視確認した。
- 実行観測の画像再生と実UI screenshotを編集。推論待ち省略・物理時間基準の再生倍率を各区間に明記。
- 分岐比較は同じ相対stepにそろえ、短い経路の末尾は静止表示。wall-clockの同時実行映像ではない。
- raw、UI画像、storyboard、GPU結果、全素材hash、ffprobe、再実行診断は同じoutputへ保持。
- [集計・hash](artifacts/repair-demo-20261102/result.json)。YouTubeへのアップロードや提出済み資料の差し替えは未実施。

```bash
.local/openvla-sim/bin/python scripts/build_repair_demo.py \
  --capture output/relay-repair-demo-20261102 \
  --output output/repair-video-fresh/Relay_rewind_and_repair.mp4
```

buildはローカルの実行記録・UI screenshot・`story.json`・`replay-verification.json`が必要。
Git cloneだけで撮影素材を再生成するものではなく、モデルやシミュレータを動かす処理も含まない。
元のraw記録、採用経路hash、手動移動量、復元状態、VLA復帰、再実行結果を検査してから書き出す。

今回追加したコードの確認は実データでのvideo build・ffprobe・公開CLI版の全146step再実行、
Python構文とE9/F63/F7/F82 lint、Markdownリンク確認。Runtime本体は変更していない。
