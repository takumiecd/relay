# Runtime分離の確認記録（2026-11-02）

PR #8を`d45645c1fe953244b9ef801e9ba0cce0f1368f76`、続いてPR #9を
`c4bc7d6e5c9a3af6456abbbcb83216bd242b280f`でmainへmergeした。
そこから`feat/runtime-extraction`を作成。実装commitは
`fce04c31e3067ee9856cfe88826c975a0b21a3d1`。
集計とhashは[JSON](runtime-extraction-20261102.json)、構成と起動手順は[Runtimeガイド](../runtime.md)。

## 回帰確認

| 環境・範囲 | 結果 |
|---|---|
| LIBERO実SDK、control、policy、dataset、CLI、復帰処理、実行器、分離 | 147 passed / 3 skipped / 15 upstream warnings |
| Genesis CPU実SDK、history、policy、LLM制御、dataset、分離 | 88 passed / skipなし |
| 専用ROS 2 domain 87、関節backend、MoveIt IK | 22 passed |
| JSキーボード・分岐表示 | 12 passed |

重複するテストを含むため、これらを独立したテスト数として加算しない。
LIBERO環境での3 skipはGenesis SDK専用ケース。同ケースはGenesis環境で実行済み。
旧`HybridRunner`経由で実CaP-Xコード実行器を使う既存試験も含めて維持している。
新しい実行器Protocol自体の試験はscripted fixtureであり、新しいLLMやsandboxの実機能検証ではない。

```bash
CAPX_LIBERO_SOURCE=capx/third_party/LIBERO-PRO .local/openvla-sim/bin/python -m pytest \
  tests/test_runtime_extraction.py tests/test_backend_runtime.py tests/test_backend_boundary.py \
  tests/test_vla_extension.py tests/test_hybrid_control.py tests/test_policy_actions.py \
  tests/test_intervention_dataset.py tests/test_relay_cli.py tests/test_manual_recovery.py \
  tests/test_llm_control.py -q
RELAY_TEST_GENESIS=1 .local/genesis-sim/bin/python -m pytest \
  tests/test_runtime_extraction.py tests/test_genesis_backend.py tests/test_genesis_history.py \
  tests/test_policy_actions.py tests/test_llm_control.py tests/test_intervention_dataset.py -q
node --test tests/web/*.test.mjs
```

ROS試験は既存のユーザーsessionと別container/domainで実施。SDKとlaunch設定は
[ROS 2ガイド](../ros2.md)と[MoveItガイド](../ros2-moveit.md)を使用した。
終了時に3子プロセスのclean exitを確認。既知のMoveIt `class_loader` unload警告は残る。

`ruff`のE9/F63/F7/F82/Iを新canonicalモジュールへ適用し通過。
execution・CaP-X adapter・LIBERO task loaderの新しい3ファイルはfull lintとformat確認も通過。
既存コードを移した全ファイルの全面format／上流全体のlintを保証するものではない。

## 独立したwheelの確認

`relay_runtime-0.1.0-py3-none-any.whl`をbuild。source manifestは51ファイルでcheckoutと一致。
wheel SHA256:

```text
b57ce8ff5db31971432e5a60abda8125f7b53bcceb7ec0edfbff1d527cd26964
```

CaP-Xツリーを含まない。Python 3.12.13の専用venvへインストールし、checkout外の`/tmp`から起動。
実際の基本依存はNumPy 2.5.4・SciPy 1.18.1・Pillow 13.0.0。`uv pip check`通過。
CaP-X・Gym・Torch・Genesis・rclpy未インストールの環境でUI・CLIが動いた。
最初のinstallはcheckoutのuv overrideがNumPy 1.26.4を強制し、依存不整合で失敗した。
プロジェクト設定を継承しないinstallで解消したため、手順に`uv --no-config`を明記した。

別途、import blockerでCaP-X・Gym・Torch・各SDKを拒否するsubprocessで、
手動／VLA fixture、復元・分岐、dataset採用を確認。disabled実行器へのcode／LLM要求が
epochを変える前に拒否されること、旧aliasの同一性、worker zipと配布範囲も確認した。

## 実際のブラウザとCLIの往復

| 対象 | 操作・結果 |
|---|---|
| インストール済みwheel / kinematic / 8889 | ブラウザH＋Space/Wで15steps → CLI stop・step 3へseek → ブラウザで修正 → CLIで17stepsの経路を採用 |
| 同wheelの展開source / Genesis CPU / 8890 | CaP-X importを拒否して起動 → H＋Space/Rで14steps → CLIでstep 0へseek → H＋Space/Aで14stepsの別経路 → CLIで採用 |

両方とも元の経路を残し、`training_admitted=false`、`TEST_OPERATOR`で記録した。
Genesisでは親の初期状態と分岐初期状態の32要素physics_stateがarray-equal。
これは同一稼働scene内の保存状態についての結果で、再起動後の復元や全画像のbitwise一致は主張しない。
Genesis試験には既存SDK環境を使ったが、`relay`はwheel由来sourceを優先し、
終了まで`capx`が一度もimportされなかったことを確認した。

画面は採用済み経路とPython実行無効を正しく表示した。スクリーンショットはローカルの
`output/runtime-extraction-20261102/{standalone-adopted,genesis-no-capx-adopted}.png`へ保存。
raw記録、receipt、JUnit、wheel等も同outputに保持し、hashをJSONへ記載した。
試験用8889／8890とROS試験containerは停止済み。
ユーザーの8883はHUMAN・epoch 2・15stepsのまま維持し、他の既存サービスも再起動していない。

今回の確認はRuntime分離と既存機能の互換性。新しいVLA把持性能、実LLM性能、
Gazebo・実機対応を新しく検証したものではない。
提出タグ`pai-final-20261102`と参照タグ`ros2-reference-20261102`の参照先は変更していない。
