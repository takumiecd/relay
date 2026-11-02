# Genesis状態復元・分岐の確認（2026-11-02）

PR #2をmainへマージした`eed9b4b`から、`feat/genesis-history`で実装。
起動・操作手順は[Genesisガイド](../genesis.md)、結果とソースSHA-256は[JSON記録](genesis-history-20261102.json)を参照。

## 保存と復元

Genesis 1.4.5／macOS Apple Silicon／CPUの同じ稼働中scene/model/reset内を対象とする。
SDKのSceneCheckpoint／SimulatorCheckpointを使い、210個のRigidSolver配列とstep数を保存。
制御入力・接触／拘束のwarm start・物理パラメータ・導出済み姿勢を含む。
Relayの関節目標・手先位置／姿勢目標・開閉、NumPy／Python／Torch CPUの乱数状態も保存する。

保存ファイルは数値配列＋JSONのNPZで、dtypeを維持し、pickleは使わない。
SDKのscene description・静的configオブジェクトは現在のbackend内に保持し、ファイルからPythonオブジェクトを生成しない。
SDK版・scene digest・model/reset識別子・solver名／配列名／shape／dtype・目標値を検証してから復元する。

## テスト結果

```bash
RELAY_TEST_GENESIS=1 .local/genesis-sim/bin/python -m pytest \
  tests/test_genesis_backend.py tests/test_genesis_history.py -q --disable-warnings

CAPX_LIBERO_SOURCE=capx/third_party/LIBERO-PRO \
  .local/openvla-sim/bin/python -m pytest \
  tests/test_backend_runtime.py tests/test_backend_boundary.py \
  tests/test_vla_extension.py tests/test_hybrid_control.py \
  tests/test_genesis_backend.py tests/test_genesis_history.py -q --disable-warnings
```

専用Genesis環境は33件成功、skipなし。既存LIBERO環境の回帰は83件成功、警告15件。
後者ではGenesis実物理18件をskipし、専用環境で別実行した。
JavaScriptのキーボード・分岐図は12件成功。

- 保存ファイルから復元後、全solver配列・関節／手先目標・物理状態・両カメラ画素が完全一致。
- 同じ入力を続けた8操作すべてで、指令・物理状態・solver配列・両カメラ画素が完全一致。
- RNGの復元、resetが元の初期状態へ戻ること、reset前のcheckpointを拒否することを確認。
- 破損したSDK版・model・配列・kind・step・制御目標・乱数状態と、不正なRelay counterを物理状態変更前に拒否。
- 巻き戻し後の最初の同一入力で元の次stepを再現し、別経路を作成。元の未来とraw記録を維持。
- 採用後の追記で採用終点が延長されず、新しい試行でも以前の採用ファイルを維持。

## ブラウザ検証

8878のTEST_OPERATORセッションで、16 step操作→分岐図のstep 3をクリック→A方向へ操作し分岐2を作成。
分岐2のstep 13までを採用した後、step 18まで延長しても採用終点は13のままだった。
元の経路の末尾step 16にも戻り、両カメラと手先位置の復元を確認した。
元のsteps.jsonlのSHA-256と採用routeのSHA-256は維持され、ブラウザのJavaScriptエラーはなかった。
全31操作（元16＋分岐後15）はTEST_OPERATORと記録し、HUMANデータには混ぜない。

既存の利用者セッションは353 stepを保存して終了し、8877に別の新セッションを起動。
旧記録は`output/genesis-manual-20261102-112900`に残し、新版は`output/genesis-history-manual-20261102-115446`を使う。
新版は初期step 0・停止状態から開始し、試験操作を混ぜずに利用者の入力をHUMANとして記録する。

## 残る範囲

再起動後の保存セッション再開、別scene／resetへの復元、他SDK版、CUDA／Metal、実VLA／LLM接続は未検証・未対応。
`state_replay`はfalseのまま。採用経路も品質確認前の候補であり、自動で学習へ投入しない。
