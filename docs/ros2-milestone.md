# ROS 2参照版の一区切り

PR #7の範囲は、**関節操作・MoveIt IKによる手先操作・RViz表示・出所付き記録を再現できる参照実装**まで。
再現用タグは`ros2-reference-20261102`。提出版`pai-final-20261102`は変更しない。

## 実装したこと

| 接続 | 入力と動作 | 観測・記録 |
|---|---|---|
| `ros2` | キー／Pythonから6関節の絶対角度＋グリッパー幅をFollowJointTrajectoryへ送る | JointState、goal ID、時刻、成功・拒否・timeout等の結果 |
| `ros2-moveit` | WORLDの手先目標をMoveItのcompute_ikへ渡し、検査済みの関節解だけを同じactionへ送る | 上記にIKのseed・手先目標・関節解・solver・モデルhashを追加 |
| 表示 | URDF＋robot_state_publisher＋TFで同じJointStateをRVizに表示 | 表示のための別のJointState publisherは起動しない |
| 共通部分 | Spaceのdeadman、速度調整、Esc、キー／コードの権限、出所付き記録 | backendの能力に従い、非対応のreset／巻き戻しを無効にする |

付属controllerは数値補間、モデルは表示用6軸アーム。物理・接触・衝突回避・MoveIt Servo・経路計画・実機・学習済みVLA／LLMのROS動作評価はこの区切りに含めない。
ROS 2のstepは有限時間の指令試行と観測結果で、物理エンジンの積分stepではない。

## 再現する

- [最小の関節操作](ros2.md)：headlessの`runtime` target、ROS domain 74。
- [URDF／TF／RViz](ros2-rviz.md)：`visualization` target、domain 75。
- [MoveIt IKの手先操作](ros2-moveit.md)：`moveit` target、domain 76。操作画面8883、RViz6084。

Dockerのbuild contextは`docker/ros2`だけ。ソースの必要なディレクトリをread-onlyでマウントし、モデルキャッシュ・認証情報・`.git`は渡さない。画面の公開先はホストのloopbackだけ。
各ガイドのコマンドでROS環境をsourceし、`ros2 launch`とRelayを別端末から明示的に起動する。既存の学習用／Genesisのコンテナを変更する必要はない。

操作画面でH → Space＋Rで少し上へ、Escで停止する。Python欄では次を実行できる。

```python
s = get_robot_state()
p = s['robot_cartesian_pos'][:3]
RESULT = move_to_pose([p[0], p[1], p[2] + 0.02])
```

終了はRelayをCtrl+Cで止めて保存し、その後launchをCtrl+Cで止める。自分たちの参照ノードは、ROSのsignal handlerで終了済みの場合も二重shutdownにせず正常終了する。
**既知の制限：この構成のMoveIt 2.5.10では、Ctrl+C後のC++プロセス終了処理でexit -11になるケースを確認した。** launchコマンド自体のexit 0は全ノードの正常終了を意味しない。原因の切り分け・SDK修正は未完で、この参照版に残る制限として扱う。記録保存を先に完了する終了順を守り、実機向けの安定した構成とは扱わない。

## 一区切り前の確認

最終テストは操作中のdomain 76と分け、**新しいdomain 78の専用コンテナ**で`start_rviz:=false`を指定して実施した。controller・robot_state_publisher・MoveItは実際に起動する。

```bash
# 同じイメージ／sourceを使うテスト専用コンテナ内
source /opt/ros/humble/setup.bash
ros2 launch /workspace/relay/ros2/reference_moveit.launch.py start_rviz:=false

# 別端末。環境変数はこの検査のコマンドだけに設定
RELAY_TEST_ROS2=1 RELAY_TEST_ROS2_VIEW=1 RELAY_TEST_MOVEIT=1 \
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 /opt/relay-venv/bin/python -m pytest \
  tests/test_ros2_backend.py tests/test_ros2_view.py tests/test_ros2_moveit.py -q
```

- ROS 2／MoveIt：**21件通過**。IK／FK、実DDSのaction／topic／service、TF、コード／キー操作、記録、古い状態・遅い返答の拒否、Ctrl+Cでの正常終了。
- GUIの動作は[RViz検証](verification/ros2-rviz-20261102.md)と[MoveIt検証](verification/ros2-moveit-20261102.md)で確認。コード3cm上昇＋キー4.2cm前進、18指令を保存済み。
- ROS 2モジュールのRuff検査・format検査、Python構文、git diffの空白検査を実施。
- `relay.ros2`のURDF・SRDF・RViz設定をpackage-dataへ追加。実際の設定を使った小さなresource wheelで6ファイルの内容一致と、展開先でのReferenceModel読込を確認。**CaP-X全体の配布wheelをビルド・公開した検査ではない。**
- 既存Genesis／LIBERO／JSの最終回帰結果と、検査範囲の機械可読データは[最終確認記録](verification/ros2-finalization-20261102.md)へ保存する。上流の学習済みモデル／oracleのスコア評価は今回の対象外。

## 次の変更に分けること

次は使用するロボット／物理シミュレータを一つ決め、そのモデル・controller・衝突形状・時刻・停止方法を検証する。
Gazebo、MoveIt Servo、実機、別のrobot profile、実VLA／LLM接続の評価はそれぞれこの参照版とは別の変更で進める。
今回の成功を、それらの環境やタスクの成功率の証拠として扱わない。
