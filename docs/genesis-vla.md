# GenesisでVLA・LLM・手動を切り替える

モデルの出力を、モデルadapterで明示した制御形式へ復号し、
Relayの実行threadでGenesisのnative actionへ変換する。
VLAはOpenVLA専用の接続ではなく、同じ出力契約を満たす別モデルadapterも使える。

## 制御形式

| 段階 | 意味 |
|---|---|
| OpenVLA LIBERO checkpointの出力 | LIBERO用に復号されたOSC入力6軸と、モデル側のgripper |
| モデルadapterの出力 | WORLDの`[dx_m,dy_m,dz_m,rx_rad,ry_rad,rz_rad,gripper]` |
| Genesisに実際に渡す指令 | 絶対手先`[x_m,y_m,z_m,qw,qx,qy,qz,gripper]` |
| 関節の動き | Genesis IK→関節目標→PD制御→物理step |

WORLD deltaの契約名は`relay.cartesian.world_delta.xyz_rotvec_m_rad.grip.7.v1`。
回転はWORLD座標のaxis-angleで、`R_target = R_delta @ R_current`。
gripperは-1=open、+1=close。1actionの並進normは2cm以内、回転normは0.2rad以内。
不正なchunkは全体を拒否し、黙ってclipして動かさない。

chunk全体を最初の姿勢から絶対目標へ変換することはしない。
**実行直前の測定手先姿勢**から1actionずつ変換するので、相対指令を順に積み上げられる。
変換は物理を進めず、既存の権限・epoch・停止検査を通る`apply_native`だけが実行する。

同じ7要素でも、LIBEROの正規化OSCとこの実単位のWORLD deltaは互換ではない。
モデルの復号・単位・座標系・gripperの意味をadapterで一致させる必要がある。
関節角や速度を出すVLAは、このdelta契約へ暗黙に変換できるとは扱わない。

## OpenVLAの例

[openvla-genesis.json](../configs/relay/openvla-genesis.json)で、全体カメラ`overview`と
Genesisの`rgb_top_left`画像を選ぶ。LIBEROのraw画像だけに必要な180度回転を、Genesis画像には重ねない。
JPEG・Lanczos resize・center crop等のモデル前処理はadapter内に残す。

このcheckpointが出すLIBERO OSC入力に、明示した
`[0.05,0.05,0.05,0.5,0.5,0.5]`のcontroller scaleと、実験用`motion_gain=0.2`を掛ける。
gripperは既存と同じ`-sign(2*x-1)`で復号する。
scaleは[robosuite 1.4.1 OSC_POSE設定](https://github.com/ARISE-Initiative/robosuite/blob/v1.4.1/robosuite/controllers/config/osc_pose.json)と
手元のLIBERO用環境の設定に照合した。異なるcontroller/checkpointで無条件に再利用しない。

これは**LIBEROで学習したpolicyを、別カメラ・別物理エンジン・小さい操作量で動かす接続試験用の設定**。
Genesisに適応した学習済みpolicyや、把持成功を示すものではない。
既定のOpenVLA adapterは従来のLIBERO native出力を保ち、この変換は明示的な設定時だけ使う。

## 起動とGPU接続

Genesis環境は[genesis.md](genesis.md)、LLM APIは[genesis-llm.md](genesis-llm.md)で用意する。

```bash
.local/genesis-sim/bin/python -m capx.extensions.hybrid.server \
  --backend genesis --backend-config configs/relay/genesis-panda.json \
  --vla-actions world-delta --vla-port 8898 \
  --llm-url http://127.0.0.1:8000/v1/chat/completions \
  --llm-model mlx-community/Qwen2.5-VL-7B-Instruct-4bit --llm-images \
  --port 8878 --output output/genesis-hybrid-01
```

VLAも使うため`--no-vla`と`--manual-only`は付けない。
`--vla-actions native`が従来の既定値で、backendと同じnative形式を出すadapter用。
bridgeが要求するpolicy契約と、環境contextのnative契約を別々に照合する。
上の設定では前者はWORLD delta 7D、後者はGenesis絶対目標8Dである。

別のGPUからは、手元のbridgeへ到達するURLが必要。
既存のSSH転送を使うか、一時的なCloudflare Tunnelを作り、そのURLを起動時に渡す。

```bash
# 別のターミナル：利用するbridgeだけを一時的に公開する
cloudflared tunnel --url http://127.0.0.1:8898
# 上のconsoleの起動引数に追加する
# --vla-public-url https://<表示されたホスト>.trycloudflare.com
```

bridgeはBearer tokenで認証する。出力先の`connection.json`をGPUへ渡すが、
セル出力・Git・共有する資料にはtokenを入れない。console自体をトンネルへ公開する必要はない。
既にconsoleを起動した場合は、connection.jsonの`url`だけを到達可能なURLへ変更し、tokenは保つ。

```bash
python3 scripts/package_vla_worker.py --output output/vla-worker.zip
```

生成したZIPとconnection.jsonをGPU側へ置き、[vla_worker.ipynb](../notebooks/vla_worker.ipynb)を使う。
ノートブックの`backend = "genesis"`を選ぶと、同梱したOpenVLA設定を使う。
通常のColabまたはLinux CUDA Jupyter用で、モデル依存は専用venvに分ける。
共有L4 poolが所有するruntimeでは、ノートブックを直接実行せずpoolのqueueを使う。

`policy_ready`を待ち、VLAボタンが有効になってから開始する。
worker終了時は`/detach`で接続待ちへ戻り、実行中のVLAモードを停止する。
異常終了でdetachが届かない場合は既存のHTTP timeoutで処理するため、即時の切断検知ではない。

## 操作と記録

操作指示欄へ入力し、P／VLAボタンでVLA、L／「指示を実行」でLLMを開始する。
Hで手動へ引き継ぎ、Escで停止する。入力欄の編集中はショートカット文字を入力として扱う。
LLMは準備済みVLAがある場合、`run_vla(指示,max_actions=...)`で委譲することもできる。

- rawの`native_action`とVLA候補の`action`は、実際にbackendへ渡した指令（Genesisでは8D）。
- `provenance.policy_action`と`policy_action_contract`は元のpolicy出力（この設定では実単位7D）。
- `policy_control`は変換規約、`policy_model`はその操作時点のモデル情報、`policy_diagnostics`はadapterの診断。
- OpenVLAの診断には復号前の`proposed_action`・入力画像hash・camera・scale・gainを残す。
- 指示欄のVLA指示と、コードから委譲した個別の指示を各操作へ保存する。

wire上の`native_actions`という既存フィールド名は互換性のため残す。
その値は返答の`action_contract`に従うpolicy出力であり、backend nativeと同じとは限らない。
実際に適用した指令との対応は上記のraw記録で保持する。

採用経路とコード候補は[intervention-dataset.md](intervention-dataset.md)の形式で出力する。
[実モデルの接続試験と制限](verification/genesis-vla-20261102.md)も参照。

## 別のVLA adapter

workerの`--adapter package.module:Factory`はそのまま使用できる。
モデル固有の前処理・un-normalization・座標系・開閉の変換をadapterに置き、
`metadata['action_contract']`と`ActionChunk.action_contract`の両方をWORLD delta契約にそろえる。
既にGenesis nativeの絶対手先目標を出せるadapterなら、consoleを`--vla-actions native`で起動する。

OpenVLA用のOSC scaleを別モデルへ自動適用しない。
ノートブックで外部adapterを指定するときは、そのモデル用のconfig・requirements・専用環境名も指定する。
この実装で実モデルを確認したのはOpenVLAだけであり、他のcheckpointの性能や互換性を確認した結果ではない。
