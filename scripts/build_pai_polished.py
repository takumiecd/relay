#!/usr/bin/env python3
"""Revised report/video from real captures, with hashes and explicit provenance.

Leaves the original submission untouched. Requires local verification artifacts,
reportlab, Pillow, NumPy and ffmpeg. No inference or upload is performed here.
"""

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import subprocess

import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageOps
from reportlab.lib import colors
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Image as PDFImage, PageBreak, Table, TableStyle,
)

ROOT = Path(__file__).resolve().parents[1]
FONT = Path('/System/Library/Fonts/Supplemental/Arial Unicode.ttf')
REPO = 'https://github.com/takumiecd/relay'
BG, INK, BLUE, MUTED = '#f2f5fa', '#20344c', '#366bc0', '#64758b'


def read(path):
    return json.loads(Path(path).read_text())


def rows(path):
    return [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def audit(capture):
    demo = capture / 'genesis-demo-v2'
    parent = rows(demo / 'trajectory/steps.jsonl')
    repaired = rows(demo / 'branches/0002/trajectory/steps.jsonl')
    selection = read(demo / 'history/manifest.json')['adopted']
    selected = rows(demo / selection['candidates'])
    assert len(parent) == 108 and len(repaired) == 40 and len(selected) == 100
    assert Counter(r['source'] for r in selected) == {'CODE': 60, 'TEST_OPERATOR': 40}
    assert selection['training_admitted'] is False and selection['code_candidate_count'] == 1
    assert digest(demo / selection['route']) == selection['route_sha256']
    assert digest(demo / selection['code_candidates']) == selection['code_candidates_sha256']
    assert all(not r['training_admitted'] for r in selected)
    program = next((demo / 'trajectory/programs').glob('*/metadata.json'))
    metadata = read(program)
    assert metadata['generator'] == 'llm_http'
    assert read(program.parent / 'result.json')['reason'] == 'program_completed'

    def state(root, observation):
        return np.load(root / read(root / observation)['physics_state'], allow_pickle=False)

    original = state(demo / 'trajectory', parent[59]['next_observation'])
    restored = state(demo / 'branches/0002/trajectory', repaired[0]['observation'])
    assert np.array_equal(original, restored)
    shutdown = read(capture / 'shutdown/shutdown-results.json')
    assert len(shutdown) == 3 and all(r['launch_exit'] == 0 and len(r['clean_children']) == 3 for r in shutdown)
    vla = ROOT / 'output/vla-live-02/osc-vla.mp4'
    assert digest(vla) == 'a4432dbdd07617ffaf9d9434f48a2f30e912ba4df48c123ef64f76fc5cfc049f'
    inputs = [vla, program, program.parent / 'code.py', demo / 'trajectory/steps.jsonl',
              demo / 'branches/0002/trajectory/steps.jsonl', demo / selection['route'],
              demo / selection['code_candidates'], capture / 'shutdown/shutdown-results.json']
    inputs += [capture / name for name in ['genesis-adopted.png', 'genesis-manual.png',
               'genesis-rewind.png', 'rviz-polished.png', 'control-polished.png']]
    info = dict(
        schema='relay.pai-polish-evidence.v1',
        source_commit=subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
        original_submission='pai-final-20261102',
        genesis=dict(model=metadata['model'], generator=metadata['generator'], llm_actions=60,
                     llm_delta_m=(np.array(parent[59]['eef_after_m'])-parent[0]['eef_before_m']).tolist(),
                     manual_actions=48,
                     manual_delta_m=(np.array(parent[107]['eef_after_m'])-parent[60]['eef_before_m']).tolist(),
                     original_route_steps=len(parent), fork_step=60, repair_actions=len(repaired),
                     repair_delta_m=(np.array(repaired[-1]['eef_after_m'])-repaired[0]['eef_before_m']).tolist(),
                     selected_steps=len(selected), selected_sources=dict(Counter(r['source'] for r in selected)),
                     code_candidates=selection['code_candidate_count'], training_admitted=False,
                     restored_physics_vector_bitwise=True, compared_state_shape=list(original.shape),
                     code=(program.parent / 'code.py').read_text()),
        shutdown=shutdown,
        material_sha256={str(p.relative_to(ROOT)): digest(p) for p in inputs},
        limitations=['No grasp/place success or policy performance claim',
                     'Genesis VLA uses a LIBERO-trained checkpoint; connection test only',
                     'ROS reference has no physics/collision model or rewind',
                     'class_loader warning remains at MoveIt teardown',
                     'Agent-operated keyboard inputs are TEST_OPERATOR, not HUMAN'],
    )
    return info, parent, repaired


def report(path, capture, info):
    pdfmetrics.registerFont(TTFont('JP', str(FONT)))
    body = ParagraphStyle('Body', fontName='JP', fontSize=10, leading=17,
                          textColor=colors.HexColor(INK), wordWrap='CJK', spaceAfter=9)
    styles = {key: ParagraphStyle(key, parent=body, **props) for key, props in {
        'title': dict(fontSize=34, leading=43, textColor=colors.HexColor(INK), spaceAfter=10),
        'h1': dict(fontSize=19, leading=28, textColor=colors.HexColor(BLUE), spaceAfter=15),
        'h2': dict(fontSize=12, leading=20, textColor=colors.HexColor(BLUE), spaceBefore=10),
        'small': dict(fontSize=8.5, leading=14, textColor=colors.HexColor(MUTED)),
        'cell': dict(fontSize=9, leading=15, spaceAfter=0),
    }.items()}
    story = []

    def p(text, kind='body'):
        return Paragraph(text.replace('\n', '<br/>'), styles.get(kind, body))

    def add(text, kind='body'):
        story.append(p(text, kind))

    def shot(name, height=260):
        image = Image.open(capture / name)
        width = min(511, height * image.width / image.height)
        story.append(PDFImage(str(capture / name), width=width, height=width * image.height / image.width))
        story.append(Spacer(1, 10))

    def table(data, widths):
        t = Table([[p(str(v), 'cell') for v in row] for row in data], colWidths=widths, hAlign='LEFT')
        t.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#e9eff8')),
            ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#f5f7fa')]),
            ('VALIGN', (0, 0), (-1, -1), 'TOP'), ('BOX', (0, 0), (-1, -1), .4, colors.HexColor('#dce4ef')),
            ('LEFTPADDING', (0, 0), (-1, -1), 10), ('RIGHTPADDING', (0, 0), (-1, -1), 10),
            ('TOPPADDING', (0, 0), (-1, -1), 9), ('BOTTOMPADDING', (0, 0), (-1, -1), 9),
        ]))
        story.append(t)
        story.append(Spacer(1, 9))

    def new_page(title):
        story.append(PageBreak())
        add(title, 'h1')

    add('Relay', 'title')
    add('自律操作の「ここだけ」を直し、経路を残す。', 'h1')
    add('VLA・LLMのPythonコード・キーボード操作を切り替え、途中から修正した経路をデータ候補として選ぶツール。CaP-Xを拡張した試作です。')
    shot('genesis-adopted.png', 280)
    add('解きたい問題', 'h2')
    add('自律操作の一部だけがうまくいかないとき、最初からすべてを手動でやり直すのは手間がかかります。途中へ戻り、元の経路も残して修正を比較・選択できる仕組みを目指しました。')
    add('今回の成果', 'h2')
    add('LIBERO／OSCでの実OpenVLA接続に加え、Genesisで実LLMのコード生成と物理動作、手動への引き継ぎ、巻き戻し・分岐・採用を確認しました。ROS 2／MoveIt／RVizへの接続も参照モデルで実装しました。')
    add('OmniCampus: takumiecd  |  2026-11-02 改良版\n元の提出タグ pai-final-20261102 は保持。把持タスクの成功を示す作品ではありません。', 'small')

    new_page('01  操作の出所をそろえて、環境へ接続する')
    table([
        ['VLA', 'LLM / Code as Policy', 'キーボード'],
        ['画像＋指示から操作値\nOpenVLAは接続例。出力契約をadapterで復号。',
         '画像・状態＋指示からPythonコード\nHTTP APIでモデルを差し替え。',
         'Hで引き継ぎ\nSpace＋WASD／RF、速度調整、C/V開閉。'],
    ], [170, 171, 170])
    add('↓  共通の権限・実行・記録層  ↓', 'h2')
    add('mode・epoch・leaseを照合し、切り替え前の遅い返答を失効させます。物理／controllerへの実行入口を一つにし、観測、元のpolicy出力、変換後の指令、生成コード、実行結果を対応付けます。Escで停止します。')
    table([
        ['接続先', '動作と能力', '今回の確認範囲'],
        ['LIBERO / MuJoCo', '既存robosuite OSCへ相対指令。状態・制御器の復元。', '実OpenVLA 30推論・30指令。把持／配置成功は未確認。'],
        ['Genesis / CPU', 'IK＋関節位置制御。8D絶対手先指令。画像・状態復元・分岐。', '実LLM、手動、短い実VLA接続。再起動をまたぐ復元は未対応。'],
        ['ROS 2 / MoveIt / RViz', '実service／action／JointState／TF。手先目標→IK→関節指令。', '独自6軸の数値参照controller。物理・接触・衝突回避・巻き戻しなし。'],
    ], [100, 210, 201])
    add('既存部品と追加した部分', 'h2')
    add('既存：CaP-Xのコード実行器、LIBERO、MuJoCo、OSC、Genesis、ROS 2、MoveIt、RViz、事前学習済みモデル。\n追加：backend契約、操作の変換・切替・失効、キーボードUI、状態復元、分岐図、採用経路と学習候補の出力。')
    add('Gazebo・実機・MoveIt Servo・経路計画は未実装。GENIAC Prizeでの利用は動機と今後の目標で、特定の大会SDKへの対応済みという意味ではありません。', 'small')

    new_page('02  実モデルと、機能試験を分けて検証する')
    dz = info['genesis']['llm_delta_m'][2] * 100
    table([
        ['証拠', '観測した結果と、その範囲'],
        ['実OpenVLA / L4', 'LIBEROで30回の推論→30回のOSC指令。始終点の手先距離約0.269m。推論の壁時計約28.4秒。タスク成功率の評価ではない。'],
        ['実Qwen2.5-VL-7B / MLX', f'Genesisの2画像と数値状態を実HTTP APIへ送信。生成コードを60指令実行し、Zが約{dz:.2f}cm上昇。要求5cmに対する残差は約{50-dz*10:.1f}mmで、3mm以内の到達は未確認。'],
        ['Genesisの三者接続（別試験）', '実VLA 3＋代理キー操作2＋実LLMコード5＝10指令。LIBERO学習済みVLAを接続した小試験で、Genesisへの適応・把持能力は評価していない。'],
        ['ROS 2 / MoveIt', '実IK／FK・action・TF等の22件が通過。改良版で起動→IK→実行→終了を3回繰り返し、全子プロセスの正常終了を確認。class_loader警告は残る。'],
    ], [134, 377])
    shot('rviz-polished.png', 170)
    add('ROS 2での流れ：Relay → MoveItのIK service → 解の検査 → FollowJointTrajectory → JointState → TF → RViz。表示モデルの動作であり、実機や物理の成功を意味しません。', 'small')
    add('回帰：Genesis 82件、既存runtime＋手動復帰111件（Genesis専用2件skip）、JS 12件。LLM説明と手動復帰の追加確認18件。各検査の対象・条件はGitHubの確認記録へ記載。', 'small')

    new_page('03  元の未来を消さず、修正した経路を選ぶ')
    add('実LLM 60指令 → 手動48指令：元の経路 108step\n　　　　　　　　　　　↳ step 60へ戻る → 修正40指令：採用 100step', 'h2')
    shot('genesis-adopted.png', 255)
    add('今回撮り直した手動区間', 'h2')
    dx = info['genesis']['manual_delta_m'][0] * 100
    dy = info['genesis']['repair_delta_m'][1] * 100
    add(f'実LLMの実行後、Space＋WでX方向へ約{dx:.1f}cm移動。step 60へ戻して、Space＋AでY方向へ約{dy:.1f}cm修正しました。代理操作はTEST_OPERATORとして記録し、本人によるHUMANデモとは区別しています。')
    add('状態とデータの対応', 'h2')
    add('分岐開始時の32要素physics_stateベクトルは、元のstep 60とbitwise一致しました。全SDK内部配列・画像・別runtimeでも一致するという主張ではありません。採用経路はCODE 60＋TEST_OPERATOR 40。元の108stepも保存しています。')
    add('画像＋指示→操作値のVLA候補と、画像＋指示→実行したPythonのコード候補1件を出力。selected_for_dataset=trueでも、training_admitted=falseを維持します。数値再生コードは意味的なpolicy生成とは区別します。')

    new_page('04  再現方法、制約、今後の進め方')
    add('再現できる形で残す', 'h2')
    add(f'公開コード：<link href="{REPO}" color="#366bc0">{REPO}</link>\nソース：{info["source_commit"][:12]}\n元の提出：pai-final-20261102 / ROS参照版：ros2-reference-20261102')
    for name, desc in [('docs/genesis-vla.md', 'VLA・LLM・手動の接続とノートブック'),
                       ('docs/genesis-llm.md', 'GPUサーバー／Colab／MacのLLM API'),
                       ('docs/ros2-moveit.md', 'Docker、ros2 launch、実IK・RVizの起動'),
                       ('docs/verification/submission-polish-20261102.md', '今回の実測・回帰・終了修正の証拠')]:
        add(f'<link href="{REPO}/blob/{info["source_commit"]}/{name}" color="#366bc0">{name}</link><br/>{desc}', 'small')
    add('失敗も記録する', 'h2')
    add('撮影時、LLMが観測snapshotのキーをget_robot_state()のキーと取り違え、0指令で停止した例がありました。失敗を保存し、APIの二つのschemaをプロンプトで区別した後、上昇動作を確認しました。誤ったコードの生成を防ぎ切る保証はありません。')
    add('制約', 'h2')
    add('タスク成功・把持／配置・学習による改善・未知環境への汎化は未検証です。Genesisの数値状態には真値を使い、vision-only評価ではありません。状態復元は同じ稼働中scene/reset内に限定します。ROS参照版に物理・衝突モデルや実機の即時安全停止保証はありません。')
    add('次に取り組むこと', 'h2')
    add('まず少数のタスクで修正区間と採用理由をレビューし、再現できる経路だけをデータ候補として整理します。その後、Gazeboまたは実機を一つ選び、モデル・controller・座標・時刻・停止方法をbackendごとに検証します。')
    add('主要部品の出典', 'h2')
    add('github.com/capgym/cap-x / github.com/openvla/openvla<br/>github.com/Lifelong-Robot-Learning/LIBERO / github.com/Genesis-Embodied-AI/Genesis<br/>github.com/ros2 / github.com/moveit/moveit2<br/>OpenVLA checkpoint: openvla/openvla-7b-finetuned-libero-spatial<br/>LLM: mlx-community/Qwen2.5-VL-7B-Instruct-4bit', 'small')
    add('動画は複数の検証記録を編集したものです。実モデル・代理キー入力・記録画像の再生・再生倍率を各区間に表示し、連続した一回の三者共同作業と誤認させない構成としました。', 'small')

    def footer(c, doc):
        c.setFont('JP', 8)
        c.setFillColor(colors.HexColor(MUTED))
        c.drawString(42, 817, 'RELAY / PAI FINAL PROJECT / REVISED')
        c.drawString(42, 24, 'takumiecd  |  CaP-X extension  |  2026-11-02')
        c.drawRightString(553, 24, f'{doc.page:02d} / 05')

    SimpleDocTemplate(str(path), pagesize=(595.276, 841.89), leftMargin=42, rightMargin=42,
                      topMargin=55, bottomMargin=43, title='Relay - PAI最終課題説明資料',
                      author='takumiecd').build(story, onFirstPage=footer, onLaterPages=footer)


def font(size):
    return ImageFont.truetype(str(FONT), size)


def text(draw, value, x, y, width, size=30, color=INK):
    line = ''
    for char in value:
        if char == '\n' or draw.textlength(line + char, font=font(size)) > width:
            draw.text((x, y), line, font=font(size), fill=color)
            y += size * 1.6
            line = '' if char == '\n' else char
        else:
            line += char
    if line:
        draw.text((x, y), line, font=font(size), fill=color)


def card(title, note):
    image = Image.new('RGB', (1920, 1080), BG)
    d = ImageDraw.Draw(image)
    d.text((65, 35), 'RELAY / PAI FINAL PROJECT / takumiecd', font=font(24), fill=BLUE)
    text(d, title, 65, 96, 1790, 46)
    text(d, note, 65, 960, 1790, 26, MUTED)
    return image


def ffmpeg(*args):
    subprocess.run(['ffmpeg', '-y', '-hide_banner', '-loglevel', 'error', *map(str, args)], check=True)


def video(path, capture, assets, info, parent, repaired):
    assets.mkdir(parents=True, exist_ok=True)
    segments = []
    encoding = ['-an', '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '20',
                '-pix_fmt', 'yuv420p', '-r', '24', '-threads', '2']

    def still(name, title, body, duration=7, screenshot=None):
        img = card(title, '実モデル接続・代理操作・記録画像の再生を区間ごとに区別しています。')
        if screenshot:
            shot = ImageOps.contain(Image.open(screenshot).convert('RGB'), (1260, 730))
            img.paste(shot, (65, 205))
            text(ImageDraw.Draw(img), body, 1370, 235, 475, 30)
        else:
            text(ImageDraw.Draw(img), body, 100, 310, 1700, 43)
        png = assets / f'{name}.png'
        img.save(png)
        out = assets / f'{name}.mp4'
        ffmpeg('-loop', '1', '-framerate', '24', '-i', png, '-t', duration, *encoding, out)
        segments.append(out)

    def captured(name, root, title, label):
        times = read(root / 'times.json')
        concat = assets / f'{name}.ffconcat'
        lines = ['ffconcat version 1.0']
        for i, item in enumerate(times):
            lines += [f"file '{(root / item['file']).resolve()}'",
                      f"duration {((times[i+1]['time_ms']-item['time_ms']) if i+1<len(times) else 100)/1000:.6f}"]
        lines.append(f"file '{(root / times[-1]['file']).resolve()}'")
        concat.write_text('\n'.join(lines) + '\n')
        bg = assets / f'{name}-bg.png'
        card(title, label + ' / 画面収録 / 代理キー入力はTEST_OPERATOR').save(bg)
        out = assets / f'{name}.mp4'
        ffmpeg('-loop', '1', '-i', bg, '-safe', '0', '-f', 'concat', '-i', concat,
               '-filter_complex', '[1:v]setpts=2*PTS,scale=1320:742:force_original_aspect_ratio=decrease[v];[0:v][v]overlay=(W-w)/2:195:shortest=1[out]',
               '-map', '[out]', *encoding, out)
        segments.append(out)

    def observations(name, root, records, title, caption):
        frames = assets / name
        frames.mkdir(exist_ok=True)
        observations = [records[0]['observation']] + [r['next_observation'] for r in records]
        for i, obs in enumerate(observations):
            data = read(root / obs)
            img = card(title, caption + ' / 記録画像再生・物理時間0.20倍 / 待ち時間省略')
            d = ImageDraw.Draw(img)
            for cam, x, size in [('wrist', 100, 710), ('overview', 1020, 590)]:
                source = Image.open(root / data['images'][cam]).convert('RGB')
                img.paste(source.resize((size, size), Image.Resampling.NEAREST), (x, 220))
                d.text((x, 175), '手首カメラ' if cam == 'wrist' else '全体カメラ', font=font(28), fill=INK)
            d.text((1020, 840), f'{i:02d} / {len(records)} executed actions', font=font(30), fill=BLUE)
            img.save(frames / f'{i:04d}.png')
        out = assets / f'{name}.mp4'
        ffmpeg('-framerate', '4', '-i', frames / '%04d.png', *encoding, out)
        segments.append(out)

    still('01-intro', '自律操作の「ここだけ」を直し、経路を残す。',
          'Relay\nVLA・LLM・キーボードの切り替えと、分岐する操作記録。\nCaP-Xを拡張 / LIBERO・Genesis・ROS 2参照版', 8)
    bg = assets / '02-vla-bg.png'
    img = card('実OpenVLA → LIBERO / OSC', '物理時間0.05倍スロー / 推論待ち省略 / 把持・配置の成功は未確認')
    text(ImageDraw.Draw(img), 'NVIDIA L4 / 実推論\n30推論 → 30指令\n手先の始終点距離 約0.269m\n\n事前学習済みモデルを利用。\nこの後のGenesisとは別の検証記録です。', 1050, 280, 750, 36)
    img.save(bg)
    out = assets / '02-vla.mp4'
    ffmpeg('-loop', '1', '-i', bg, '-i', ROOT / 'output/vla-live-02/osc-vla.mp4',
           '-filter_complex', '[1:v]setpts=20*PTS,fps=24,scale=720:720,tpad=stop_mode=clone:stop_duration=1[v];[0:v][v]overlay=160:220:shortest=1[out]',
           '-map', '[out]', '-t', '30', *encoding, out)
    segments.append(out)
    demo = capture / 'genesis-demo-v2'
    still('03-code', '実LLMが生成したPythonを、そのまま実行',
          info['genesis']['code'] + '\nQwen2.5-VL-7B 4bit / Mac MLX / 実HTTP API\n2画像＋数値状態を入力。5cmの目標に対し約4.56cm上昇。', 10)
    observations('04-llm', demo / 'trajectory', parent[:60], '実LLM → Genesisの物理動作',
                 'CODE / 60指令 / 3mm以内の目標到達・タスク成功は未確認')
    captured('05-manual', capture / 'manual-capture', '手動へ引き継ぐ：Space＋W → Escで停止',
             '壁時計0.5倍スロー / X方向へ約28.6cm移動')
    observations('05b-manual-detail', demo / 'trajectory', parent[60:],
                 '引き継ぎ区間の動きを、2つの記録カメラで見る',
                 'TEST_OPERATOR / 48指令 / X方向へ約28.6cm / 画面収録と同じ操作')
    still('06-rewind', 'step 60へ戻る。元の108stepは残す。',
          '分岐図のstepをクリック。\n手先・物理状態を復元し、停止。\n復元前後のphysics_state 32要素がbitwise一致。', 8,
          capture / 'genesis-rewind.png')
    observations('07-repair', demo / 'branches/0002/trajectory', repaired,
                 '別方向へ修正し、新しい分岐へ記録', 'TEST_OPERATOR / Space＋A / Y方向へ約15.4cm')
    still('08-adopt', '採用する経路を固定し、学習候補へ書き出す',
          'CODE 60＋修正40\n採用100step\n元の経路108stepを保持\n\nPythonコード候補1件\ntraining_admitted=false', 9,
          capture / 'genesis-adopted.png')
    captured('09-ros', capture / 'rviz-capture', 'ROS 2：MoveIt IK → 関節指令 → JointState → RViz',
             '壁時計0.5倍スロー / 独自6軸参照モデル / 物理・衝突モデルなし')
    still('10-limits', '既存部品を組み合わせ、修正と記録を共通化。',
          '確認：実VLA／実LLMの接続、切り替え、状態復元、分岐、出所付き記録。\n未確認：把持・配置成功率、学習改善、未知環境への汎化、実機。\n\n公開コード：github.com/takumiecd/relay\nPAI最終課題 / takumiecd', 12)
    concat = assets / 'segments.ffconcat'
    concat.write_text('ffconcat version 1.0\n' + ''.join(f"file '{p.resolve()}'\n" for p in segments))
    ffmpeg('-safe', '0', '-f', 'concat', '-i', concat, '-c', 'copy', '-movflags', '+faststart', path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--capture', type=Path, default=ROOT / 'output/submission-polish-20261102')
    parser.add_argument('--output', type=Path, default=ROOT / 'output/submission-polished-takumiecd')
    parser.add_argument('--pdf-output', type=Path, default=ROOT / 'output/pdf/polished')
    parser.add_argument('--no-video', action='store_true')
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    args.pdf_output.mkdir(parents=True, exist_ok=True)
    info, parent, repaired = audit(args.capture)
    report(args.pdf_output / '説明資料_PAI最終課題_takumiecd.pdf', args.capture, info)
    if not args.no_video:
        video(args.output / 'PAI最終課題_takumiecd.mp4', args.capture, args.output / 'video-assets', info, parent, repaired)
    products = [args.pdf_output / '説明資料_PAI最終課題_takumiecd.pdf']
    movie = args.output / 'PAI最終課題_takumiecd.mp4'
    if not args.no_video:
        duration = float(subprocess.check_output(['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
                                                  '-of', 'default=noprint_wrappers=1:nokey=1', str(movie)]))
        assert 60 <= duration <= 180, duration
        info['video_duration_s'] = duration
        products.append(movie)
    info['outputs_sha256'] = {str(p.relative_to(ROOT)): digest(p) for p in products}
    (args.output / 'deliverables.json').write_text(json.dumps(info, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(dict(outputs=info['outputs_sha256'], duration_s=info.get('video_duration_s')), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
