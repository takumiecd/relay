# ROS 2 joint backend：2026-11-02

対象：`feat/ros2-joint-backend`。mainへPR #4〜#6を統合した`0d48160`を基点とする。
提出タグ`pai-final-20261102`は変更していない。

## 確認した接続

専用の`relay-ros2-dev`コンテナ、ROS 2 Humble、arm64、domain 74で実行。
学習用の`ros2-moveit-lab`（domain 72）とGenesisの8878サーバーは維持した。
ROS 2参照コントローラとRelayは別プロセス／ノードで、実際のDDS topicとactionを使う。

- `/relay_reference_controller`：FollowJointTrajectory action server、JointState publisher。
- `/relay_backend_<id>`：action client、JointState subscriber。
- action `/relay_reference/follow_joint_trajectory`：`control_msgs/action/FollowJointTrajectory`。
- topic `/relay_reference/joint_states`：`sensor_msgs/msg/JointState`。

実行手順とコマンドは[ROS 2ガイド](../ros2.md)、機械可読の結果は[result.json](artifacts/ros2-reference-20261102/result.json)。

## ブラウザの確認

http://127.0.0.1:8881/ でH → Space＋W → Escを操作し、関節1が0.060→0.090radに変化して停止した。
続けて表示されたPythonコード`q[0] += .04; RESULT=move_joints(q)`を実行し、0.130radに変化して停止した。
JointStateを受信した棒グラフ・数値と、コード完了の表示を確認した。

QAの記録は`output/ros2-reference-20261102/browser-qa/`、スクリーンショットは同じ親ディレクトリの`browser-qa.png`。
8指令（TEST_OPERATOR 6、CODE 2）が記録され、全て結果succeeded、goal ID・feedback時刻付き。
事前の自動試験で関節5を動かしているため初期状態は全軸ゼロではない。Relay接続時にコントローラをリセットしていない。
QAセッションを保存して終了後、通常の手動セッションを同ポートに起動し、STOPPEDで引き渡す。

## 自動検査

- ROS 2：**11 passed**。7件の契約検査と、4件の実DDS/action統合検査。
  キーボード、関節5のコード操作、グリッパー、出所付き記録、最終関節上下限の実行前拒否、古いsource stampの拒否、応答timeout後の測定角度／結果の記録。
- 共通backend・policy・LLM：**64 passed, 6 skipped**。ROS 2等の環境別opt-inは上の専用環境で実施。
- Genesis実CPU physics：**82 passed**。既存のpolicy変換・状態復元・LLM・intervention dataset。
- 既存UIのキーボード・分岐図：**12 passed**。console.js構文検査とgit diffの空白検査も通過。
- LIBERO回帰：**104 passed, 2 skipped, 15 warnings**。skipは専用Genesis SDKが必要なケースで、上のGenesis環境で実施した。既存のmetadataを持たない参照fixtureに対応するConsole fallbackも維持した。

## 範囲

付属controllerは数値補間だけを行う。物理シミュレータ、ロボット形状の表示、接触、画像topic、RViz、MoveIt、IK、実機、実LLMのROS 2操作、学習済みjoint VLAの評価は含まない。
ROS 2の1stepは有限時間trajectoryの指令試行と観測結果であり、物理積分stepや成功した動作と同義ではない。
返答待ちがtimeoutしても角度が変わっていることを実テストで確認したため、その結果は不確実な指令として記録して試行を打ち切る。
reset・checkpoint・state replayは非対応で、UIも無効にする。物理状態を巻き戻したという扱いはしない。
