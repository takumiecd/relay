# 三者切替と共通記録の確認（2026-11-02）

固定値は[hybrid-switching-20261102.json](hybrid-switching-20261102.json)。
実モデルの推論確認とは分けた、制御と記録の結合試験である。

## 検証した経路

- 同じOSC環境の唯一のstep入口で、mode・epoch・source・program IDを検査する。
- VLAの遅延中に手動へ移り、遅れて返ったactionを適用しない。
- action chunkの未実行部分を切替で捨てる。
- 古い入力sequence、切替前のepoch、別clientからの入力を無効にする。
- 入力の期限切れ・deadman解除で停止し、HUMANモードは維持する。
- CaP-Xの実行器を子processで使い、APIだけをsim作成threadへ戻す。
- 無限loopになったPython processを停止して、手動操作へ引き継げる。
- コードの`run_vla`が同じ環境を動かし、コードへ戻る。
- 実シミュレータでTEST_VLA・TEST_OPERATOR・CODEの移動と出所を保存する。
- mode変更後の観測contextとstepのcontextが一致する。
- HTTPのOrigin/token検査と、HTTP threadがsimulatorへ触らないことを確認する。
- LLMのHTTP clientは、APIを模したserverへ指示・状態・両画像を渡し、Pythonを受け取る。

## ブラウザ確認

`output/hybrid-ui-02/`で、キーボード入力・切替・両カメラの表示を確認した。
Cuaによる操作なのでsourceはTEST_OPERATORとし、人の教師データは作っていない。

| source | 実行step |
|---|---:|
| TEST_OPERATOR | 40 |
| CODE（固定の試験provider） | 7 |
| TEST_VLA（固定OSC action chunk） | 55 |
| 合計 | 102 |

コードprocessは正常終了した。試験入力を実LLM生成や実VLA推論とは扱わない。
全stepの観測contextと実行contextを照合した。
102 actionを100+2の有限コードへ分割して再実行し、全103時点のfull simulator stateと
全stepの手先位置の差は0だった。結果は`output/hybrid-replay-01/verification.json`。
これは数値再生の確認であり、task成功や汎用policyを得たという意味ではない。

記録は両画像・状態・出所・切替event・実行コードを残す。
VLA候補と実行コード候補を分け、すべてtraining_admitted=falseのままである。
raw output・録画・screenshot・venvはGitに含めない。

## 操作中の待ち時間

PNG保存を測ると、10 stepのうち0.493秒が画像のencodeにかかっていた。
PNGの圧縮levelを6から1へ変え、losslessのままencode負荷を減らした。
同じ診断で全10 stepは0.878秒から0.462秒、画像encodeは約0.097秒になった。

ブラウザでSpace+Rを押した短い確認区間では、step間隔の中央値が
130.56588172912598 msから57.355642318725586 msへ変わった。
このMacでの短い逐次試験の値であり、hardwareや環境を変えた性能保証ではない。

## 限界と次の確認

- 実LLMをホストして推論する試験、実VLA＋実LLMの同時接続は今回行っていない。
- 実OpenVLA単独接続の以前の結果は[vla-osc-20261102.md](vla-osc-20261102.md)を参照。
- 実ゲームパッドは未接続。標準mappingの入力処理とキーボードは実装した。
- 人の実操作による修正デモは未収集。
- 巻き戻し／採用branch、意味のあるコードへの要約、学習は未実装。
- code processは強制終了できるが、任意OSアクセスを防ぐsandboxではない。

## 再現するテスト

```bash
CAPX_LIBERO_SOURCE=capx/third_party/LIBERO-PRO \
  .local/openvla-sim/bin/python -m pytest \
  tests/test_vla_extension.py tests/test_hybrid_control.py -q --disable-warnings
```

32 passed。既存の単独VLA/OSCと数値コード再生のテストも含む。
静的なPythonエラーチェック、JavaScript構文、Git diffの空白も確認した。
