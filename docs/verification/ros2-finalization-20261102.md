# ROS 2参照版の最終確認：2026-11-02

対象：PR #7。機能追加はROS 2関節操作・MoveIt IK・RViz・記録で止め、mainへ統合する参照版の区切り。
再現用タグ：`ros2-reference-20261102`。提出タグは`pai-final-20261102`（commit `ec5fcd4`）を維持。

## レビューで修正したこと

- 参照ノードのCtrl+Cで、ROS signal handlerの後に`rclpy.shutdown()`を二重に呼んでexit 1になっていた。`ExternalShutdownException`を処理し、`try_shutdown()`を使うよう修正。別domainの子プロセスをSIGINTで止める回帰検査を追加し、exit 0・Tracebackなしを確認。
- URDF／SRDF／RViz設定を`relay.ros2`のpackage-dataへ追加。ROS標準の`.launch.py`名をRuffの例外として明示し、新しいROSモジュールの整形・lintを実施。
- 操作中のセッションを触らず最終テストできるよう、`start_rviz:=false`を追加。controller・robot_state_publisher・MoveItは省略しない。
- README／ガイドをmain基準の説明へ整理し、[一区切りの範囲](../ros2-milestone.md)を明示。

## 最終検査

| 対象 | 結果 | 実行範囲 |
|---|---|---|
| ROS 2／MoveIt | **21 passed** | 新しい`relay-ros2-milestone-check`コンテナ、domain 78、GUI省略、実DDS／service／action／TF |
| Genesis | **82 passed** | CPU実physics、policy変換・history・LLM・intervention |
| LIBERO回帰 | **104 passed, 2 skipped, 15 warnings** | 専用venv。skipはGenesis環境が必要なケースで、上段のGenesis環境で実施 |
| Web JS | **12 passed** | キーボード・分岐図 |
| Ruff | pass | 新しいROS 2モジュール＋backend契約／loader。format検査も通過。既存pyprojectの非推奨設定に関する警告は残る |
| resource wheel | pass | 同じpackage-data設定を使った小さな隔離wheel。URDF／SRDF／RViz・3 launchファイルを内容照合し、展開先でReferenceModelを読込 |

resource wheelは`relay`の追跡済みsourceと最小build metadataを使う検査で、CaP-X全体の配布物のビルド／公開ではない。
上流CaP-Xの学習済みモデル／oracleのスコア評価も今回の検査では行っていない。

生の記録：`output/ros2-finalization-20261102/`。JUnit XML、headless launchログ、package-check.json、lint結果を保存。
機械可読の結果は[result.json](artifacts/ros2-finalization-20261102/result.json)。
既存のGUI動作確認は[MoveIt検証](ros2-moveit-20261102.md)と[RViz検証](ros2-rviz-20261102.md)を参照。18指令のQA記録を維持した。

## 終了確認で残った制限

domain 78のlaunchをSIGINTで終了したところ、参照ノードとrobot_state_publisherは正常終了したが、MoveIt 2.5.10はrclcppのNode／CallbackGroup破棄を含むstack traceを出し、exit -11になった。launch自体はexit 0だった。
**この参照構成での再現事実であり、原因の特定やSDK修正を完了したという結果ではない。** 計算・動作・保存の検査結果とは分けて扱う。
今回の版は、先にRelayを止めて記録保存を完了してから計算／表示側を終了する。衝突回避・物理・実機・終了処理のSDK対応は次の変更へ分ける。
最終テストで操作中のdomain 76や既存のGenesis／学習用セッションを再起動・巻き戻ししていない。
