# VLA接続の開始点

2026-11-02に上流`capgym/cap-x`の`53e9966d7a8e2fa7494676772bccc35280f5c0ed`を確認。
これは接続調査の記録であり、VLAによる物理動作の実装・検証結果ではない。

## forkは必須ではない

GitHubに派生repoを作るforkと、手元にsourceを置くcloneは別である。
現在は上流をcloneし、ローカルの`codex/vla-integration`ブランチを用意した。
remoteは`upstream`。GitHub上のfork・公開・pushは行っていない。

CaP-Xの`capx.integrations.base_api.register_api(name, factory)`に外部APIを登録できる。
設定の`_target_`も外部packageをimportできるため、追加packageのtaskクラスで
VLA APIを登録してから既存taskの初期化へ渡す方式を第一候補にする。
workerごとに登録が必要なので、親launcherで一度だけ登録して済ませない。

上流のLLM接続、Pythonコード実行、知覚・動作API、動画・評価は再利用する。
VLAの推論runtimeは別process/環境に置き、GPUでホストしたコード生成LLMとも分ける。
VLA接続のために上流のtorchやsimulator依存を旧relay-teleop環境へ混ぜない。

## 目指す呼び出し

生成Pythonが、既存のCaP APIと短いVLA primitiveを組み合わせる。

```python
# 接続後の予定API。現時点では実行できない。
result = run_vla("grasp the black bowl", max_actions=20)
print(result)
# 最新の画像で把持の成否を確認してから、運搬・再試行を選ぶ。
```

VLAが返す値を実行するのはsimulator側だけ。GPUは観測からactionを提案する。
上限、停止・期限切れ、task終了、action形式、実行済みstepを確認して返す。
primitiveの正常終了と把持成功は区別する。人の操作や学習用教師入力とは別に記録する。
人の途中介入とMCP/skill公開は、このprimitive接続後に追加する。

## 先に解決する制御形式の違い

| 接続先 | 確認した形式 |
|---|---|
| 上流`FrankaLiberoEnv` | `JOINT_POSITION` controllerで作成。内部の関節移動は7関節とgripperの8要素を使う |
| 既存relay-teleopのOpenVLA | OSCによる手先変位6要素とgripperの7要素 |

7要素に値を足して8要素へ合わせても、関節と手先という意味の違いは解決しない。
同じLIBERO/Pandaでも、checkpoint・前処理・正規化・controller・座標系を合わせて確認する。

上流`FrankaLiberoEnv.step(action)`は現在actionを物理へ適用しないfallbackである。
HTTP要求の成功やこのstepの返却だけで「VLAが動いた」とは扱わない。
実際のcontroller適用、観測の変化、sim step数を確認する実行adapterが必要。

実装順は次とする。

1. 使うcheckpointのaction契約とCaP-X環境の契約を照合する。
2. 環境を二重作成せず、同じscene/stateでCaPとVLAを実行するadapterを用意する。
3. 実VLAの1要求・少数actionを、適用前後の状態・画像とともに検証する。
4. 生成コードから有限のVLA呼び出しを行い、既存CaPへ戻せることを確認する。

OSC環境への接続、関節指令への変換、controllerの切替は別の実装選択である。
未検証の変換を標準OpenVLAの評価条件と同一とは主張しない。
controllerの差を無視して先に動作コードやデモを作らない。

## 既存のVLA拡張も参照する

[Popc0rn7/cap-x-planner](https://github.com/Popc0rn7/cap-x-planner)に、
`VlaPrimitiveApi`、model/action adapter、HTTP transport、mock試験の実装がある。
独立したコミュニティforkであり、CaP-X本体の標準機能や実OpenVLA動作の保証とは扱わない。
短いprimitive、推論と制御の分離などを参照し、必要な部分の再利用を検討する。
コードを取り込む場合は版を固定し、MITの著作権表示を維持する。

## 今の確認範囲

- 上流sourceのclone、作業ブランチ作成、API登録と設定の拡張口を確認。
- 上流に標準VLA clientは見つからず、別forkに実装候補を確認。
- CUDA依存、simulator submodule、モデル重みはまだ導入していない。
- 実VLA推論、CaP-Xでの物理適用、人の割込は未検証。
- 既存relay-teleopとその記録・runtimeは変更していない。
