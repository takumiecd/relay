# Relay Runtime

Runtime・CLI・ブラウザ画面・環境アダプタの独立した配布物です。
CaP-X、Gym、Torch、シミュレータSDKは基本依存に含みません。

```bash
relay-serve --backend kinematic --manual-only --port 8876 --output output/reference
relay --url http://127.0.0.1:8876 status
```

付属のkinematicは物理を持たない数値サンプルです。
Genesis・ROS 2・LIBEROには、それぞれのSDKと設定が別途必要です。
Pythonコードを実行するときはCaP-Xをインストールし、`--code-executor capx`を明示します。
コード実行器は差し替え可能で、Runtime自体はその実装をimportしません。

この配布物はCaP-Xを拡張したRelayリポジトリから切り出しています。
上流CaP-XのMITライセンスとクレジットを保持します。
コード・手順・検証記録: https://github.com/takumiecd/relay
