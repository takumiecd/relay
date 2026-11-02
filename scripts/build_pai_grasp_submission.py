#!/usr/bin/env python3
"""Build the revised PAI PDF and package the verified local grasp-repair demo.

Reads actual observations, adoption records and independent replay evidence.
No inference, synthetic simulator frames, uploads, or changes to old submissions.
Optional authoring dependencies: reportlab, Pillow and pypdf; ffprobe on PATH.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import math
from pathlib import Path
import shutil
import subprocess
from xml.sax.saxutils import escape

from PIL import Image, ImageOps
from reportlab.lib import colors
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    Flowable, Image as PDFImage, PageBreak, Paragraph, SimpleDocTemplate,
    Spacer, Table, TableStyle,
)

ROOT = Path(__file__).resolve().parents[1]
REPO = 'https://github.com/takumiecd/relay'
ACCOUNT = 'takumiecd'
REPORT_DATE = '2026-11-03'
FONT = Path('/System/Library/Fonts/Supplemental/Arial Unicode.ttf')
INK, BLUE, GREEN, MUTED, ORANGE = '#20344c', '#366bc0', '#398778', '#64758b', '#a67d42'
WIDTH = 511


def read(path):
    return json.loads(Path(path).read_text())


def rows(path):
    return [json.loads(x) for x in Path(path).read_text().splitlines() if x.strip()]


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def audit(capture):
    evidence = read(capture/'verification.json')
    replay = read(capture/'selected-replay/verification.json')
    baseline = read(capture/'baseline-replay/verification.json')
    scene = capture/'scene'
    adopted = read(scene/'history/manifest.json')['adopted']
    candidates = rows(scene/adopted['candidates'])
    parent = rows(scene/'trajectory/steps.jsonl')
    repair_root = scene/'branches'/adopted['branch']/'trajectory'
    repair = rows(repair_root/'steps.jsonl')
    episode = read(scene/'trajectory/episode.json')
    model = read(capture/'gpu-results/results/artifacts/worker/model.json')
    assert evidence['task_id'] == 0 and evidence['initial_index'] == 41
    assert len(parent) == 179 and len(repair) == 68 and len(candidates) == 106
    assert Counter(x['source'] for x in candidates) == {'VLA': 68, 'TEST_OPERATOR': 38}
    assert [x['source'] for x in repair] == ['TEST_OPERATOR']*38+['VLA']*30
    assert adopted['end_step'] == 106 and adopted['training_admitted'] is False
    assert digest(scene/adopted['route']) == adopted['route_sha256']
    assert all(x['training_admitted'] is False for x in candidates)
    assert replay['steps'] == 106 and replay['every_replayed_physics_state_exact'] is True
    assert replay['state_shape'] == [92] and replay['final_reward'] == 1
    assert replay['final_task_completed'] is True and replay['guard_1mm_pass'] is True
    assert replay['refresh_success_differences'] == []
    assert baseline['steps'] == 180 and baseline['every_replayed_physics_state_exact'] is True
    assert baseline['final_reward'] == 0 and baseline['final_task_completed'] is False
    assert evidence['local_correction']['not_human_demonstration'] is True
    assert episode['metadata']['test_vla'] is False
    assert model['model'] == evidence['model'] and model['revision'] == evidence['model_revision']
    assert model['code_revision'] == evidence['code_revision'] and model['device'] == 'NVIDIA L4'
    assert model['required_views'] == ['agentview'] and model['uses_robot_state'] is False
    assert model['seed'] == 7 and model['center_crop_area'] == .9 and model['quantized'] is False
    assert all(not x['backend_diagnostics']['target_contact_geoms'] for x in parent)
    assert math.isclose(repair[37]['backend_diagnostics']['target_lift_m'],
                        evidence['local_correction']['target_lift_after_m'], abs_tol=1e-12)
    displacement = math.dist(repair[37]['backend_diagnostics']['target_xyz_m'],
                             repair[-1]['backend_diagnostics']['target_xyz_m'])
    assert math.isclose(displacement, evidence['after_return']['target_translation_m'], abs_tol=1e-12)
    non_target = repair[-1]['backend_diagnostics']['non_target_max_initial_L1_m']
    assert len(non_target) == 3 and max(non_target.values()) <= .001
    a = read(scene/'trajectory'/parent[37]['next_observation'])
    b = read(repair_root/repair[0]['observation'])
    assert (scene/'trajectory'/a['physics_state']).read_bytes() == (repair_root/b['physics_state']).read_bytes()
    video = capture/'deliverables'/evidence['video']['file']
    assert digest(video) == evidence['video']['sha256']
    probe = json.loads(subprocess.check_output([
        'ffprobe', '-v', 'error', '-show_entries', 'format=duration:stream=codec_name,width,height',
        '-of', 'json', str(video)], text=True))
    assert float(probe['format']['duration']) == 63.0
    assert probe['streams'][0]['codec_name'] == 'h264'
    assert (probe['streams'][0]['width'], probe['streams'][0]['height']) == (1920, 1080)
    return evidence, parent, repair_root, repair


class Diagram(Flowable):
    def __init__(self, kind):
        super().__init__()
        self.kind = kind
        self.width, self.height = WIDTH, 178 if kind == 'architecture' else 108

    def draw(self):
        c = self.canv

        def label(x, y, text, size=9, color=INK):
            c.setFont('JP', size)
            c.setFillColor(colors.HexColor(color))
            c.drawString(x, y, text)

        def box(x, y, w, h, title, note, color=BLUE):
            c.setFillColor(colors.HexColor('#f2f5fa'))
            c.setStrokeColor(colors.HexColor('#dce4ef'))
            c.roundRect(x, y, w, h, 6, fill=1, stroke=1)
            label(x+10, y+h-18, title, 10, color)
            label(x+10, y+12, note, 8, MUTED)

        def line(x1, y1, x2, y2, color=MUTED):
            c.setStrokeColor(colors.HexColor(color))
            c.setLineWidth(1.2)
            c.line(x1, y1, x2, y2)

        if self.kind == 'architecture':
            for x, title, note, color in [
                (0, 'VLA', '画像・指示 → 操作値', BLUE),
                (174, 'LLM / Python', 'HTTP → コード → 実行器', '#8263a4'),
                (348, 'キーボード', 'Space＋WASD / RF / CV', ORANGE),
            ]:
                box(x, 126, 163, 48, title, note, color)
                line(x+81, 126, 255, 111)
            box(0, 58, WIDTH, 53, 'Relay Runtime',
                '権限・切替・停止・記録・分岐・採用  /  ブラウザとCLIは同じHTTP APIを使用')
            for x, name in [(0, 'LIBERO / OSC'), (174, 'Genesis / IK'), (348, 'ROS 2 / MoveIt 参照')]:
                line(255, 58, x+81, 44)
                box(x, 0, 163, 44, name, 'backendごとに指令・観測を変換')
        else:
            x = lambda step: 25+step/179*460
            top, bottom = 76, 26
            line(x(0), top, x(38), top, BLUE)
            line(x(38), top, x(179), top, '#a5b3c6')
            line(x(38), top, x(38), bottom, MUTED)
            line(x(38), bottom, x(76), bottom, ORANGE)
            line(x(76), bottom, x(106), bottom, BLUE)
            for step, y, col in [(0, top, BLUE), (38, top, BLUE), (179, top, MUTED),
                                 (76, bottom, ORANGE), (106, bottom, GREEN)]:
                c.setFillColor(colors.HexColor(col))
                c.circle(x(step), y, 3.2, fill=1, stroke=0)
                label(x(step)-6, y-16, str(step), 8, col)
            label(25, 94, 'VLA単独：179 stepでgoalなし', 9, MUTED)
            label(x(38)+8, 41, '把持修正38 → VLA 30', 8.5)
            label(x(106)+12, 22, '採用：106 step / goal到達', 9, GREEN)


def make_report(path, assets, evidence, source_commit):
    pdfmetrics.registerFont(TTFont('JP', str(FONT)))
    body = ParagraphStyle('body', fontName='JP', fontSize=9.7, leading=15.8,
                          wordWrap='CJK', textColor=colors.HexColor(INK), spaceAfter=8)
    styles = {'body': body}
    for name, props in {
        'title': dict(fontSize=35, leading=43, spaceAfter=8),
        'tagline': dict(fontSize=16.4, leading=24, textColor=colors.HexColor(BLUE), spaceAfter=10),
        'h1': dict(fontSize=18, leading=27, textColor=colors.HexColor(BLUE), spaceAfter=12),
        'h2': dict(fontSize=11.5, leading=18, textColor=colors.HexColor(BLUE), spaceBefore=9, spaceAfter=5),
        'small': dict(fontSize=8.2, leading=12.7, textColor=colors.HexColor(MUTED), spaceAfter=7),
        'cell': dict(fontSize=8.7, leading=13.8, spaceAfter=0),
        'metric': dict(fontSize=21, leading=28, textColor=colors.HexColor(BLUE), spaceAfter=0),
    }.items():
        styles[name] = ParagraphStyle(name, parent=body, **props)
    story = []

    def p(text, kind='body'):
        return Paragraph(text.replace('\n', '<br/>'), styles[kind])

    def add(text, kind='body'):
        story.append(p(text, kind))

    def table(data, widths, padding=8):
        t = Table([[p(str(x), 'cell') for x in row] for row in data], colWidths=widths, hAlign='LEFT')
        t.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#e9eff8')),
            ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#f4f7fa')]),
            ('VALIGN', (0, 0), (-1, -1), 'TOP'),
            ('BOX', (0, 0), (-1, -1), .4, colors.HexColor('#dce4ef')),
            ('LEFTPADDING', (0, 0), (-1, -1), padding), ('RIGHTPADDING', (0, 0), (-1, -1), padding),
            ('TOPPADDING', (0, 0), (-1, -1), padding), ('BOTTOMPADDING', (0, 0), (-1, -1), padding),
        ]))
        story.extend([t, Spacer(1, 8)])

    def pictures(items, width):
        data = []
        for filename, title in items:
            data.append([p(title, 'small'), PDFImage(str(assets/filename), width=width, height=width)])
        t = Table([[[*cell] for cell in data]], colWidths=[WIDTH/len(items)]*len(items))
        t.setStyle(TableStyle([('VALIGN', (0, 0), (-1, -1), 'TOP'),
                              ('LEFTPADDING', (0, 0), (-1, -1), 0),
                              ('RIGHTPADDING', (0, 0), (-1, -1), 5),
                              ('TOPPADDING', (0, 0), (-1, -1), 0),
                              ('BOTTOMPADDING', (0, 0), (-1, -1), 0)]))
        story.extend([t, Spacer(1, 7)])

    def page(title):
        story.append(PageBreak())
        add(title, 'h1')

    def link(label, url):
        return f'<link href="{escape(url)}" color="{BLUE}">{escape(label)}</link>'

    add('Relay', 'title')
    add('自律操作の「ここだけ」を直し、経路を残す。', 'tagline')
    add('VLA・LLMのコード・キーボード操作を切り替え、途中へ戻って修正した経路を選ぶデータ収集ツール。今回の提出では、VLAの把持ミスを局所的に直し、VLAへ返す実演を示す。')
    add(f'OmniCampus: takumiecd  /  {REPORT_DATE} 把持修正デモ版', 'small')
    pictures([('goal-agentview.png', '全体カメラ / VLA再開後のstep 106'),
              ('goal-wrist.png', '手首カメラ / LIBERO goal判定に到達')], 240)
    metrics = Table([[
        [p('VLA単独', 'small'), p('goalなし', 'metric'), p('探索時180 step', 'small')],
        [p('局所修正＋VLA', 'small'), p('106 step', 'metric'), p('reward=1 / goal=true', 'small')],
        [p('独立した再実行', 'small'), p('全step一致', 'metric'), p('92要素の物理状態を照合', 'small')],
    ]], colWidths=[170, 171, 170])
    metrics.setStyle(TableStyle([('BACKGROUND', (0, 0), (-1, -1), colors.HexColor('#f2f5fa')),
                                ('VALIGN', (0, 0), (-1, -1), 'TOP'),
                                ('LEFTPADDING', (0, 0), (-1, -1), 10),
                                ('TOPPADDING', (0, 0), (-1, -1), 9),
                                ('BOTTOMPADDING', (0, 0), (-1, -1), 7)]))
    story.append(metrics)
    add('解きたい問題と、今回の工夫', 'h2')
    add('自律操作の一部だけが失敗したとき、元の経路を残してその地点から修正し、残りを再びVLAへ任せる。切替・巻き戻し・分岐・採用を一つの履歴として扱う点を中心に実装した。')
    add('修正はエージェントが位置の数値情報も参考にして入力した代理操作（TEST_OPERATOR）として記録。モデルは公開済みの重みを利用し、人間の操作実演と区別している。学習への投入は別工程とした。', 'small')

    page('01  制御の切替と、分岐する実行履歴')
    add('ブラウザとCLIから同じAPIで状態確認・切替・停止・巻き戻し・経路採用を行う。RuntimeをCaP-Xから分離し、コード実行が必要な場合にCaP-Xの実行器を接続する。')
    story.append(Diagram('architecture'))
    add('遅れて届いた指示を、そのまま適用しない', 'h2')
    add('実行入口を一つにし、操作主体とepoch・実行権限を照合する。切替前の推論結果や古いキー入力は失効させる。ブラウザではHで手動、PでVLA、Escで停止。Spaceを押す間だけ進み、Cで閉じ、Vで開く。')
    add('元の未来を保存し、修正後の経路を採用する', 'h2')
    story.append(Diagram('history'))
    add('図は比較対象の2経路を抜粋。step38の状態を復元し、共通区間を参照した新しい分岐を作る。採用した終点は、後から別の地点を表示しても自動では変わらない。全分岐は記録に保持する。', 'small')
    table([
        ['既存部品を利用', 'Relayで追加した部分'],
        ['CaP-XのPython実行器、LIBERO／MuJoCo／robosuite OSC、Genesis、ROS 2／MoveIt／RViz、公開AIモデル。',
         '共通backend・policy契約、切替と失効、ブラウザ／CLI、制御器を含む状態復元、分岐図、採用範囲と学習候補の書き出し。'],
    ], [250, 261])
    add('モデルや物理エンジンを新規開発した作品ではない。CaP-X由来の部分と、共通Runtime・接続adapter・操作履歴の追加を区別している。', 'small')

    page('02  実OpenVLAの把持ミスを、局所的に直す')
    add('LIBERO Spatial task0 / init41：皿とラメキンの間にある黒いボウルを皿へ移す。VLAがボウル付近で早く指を閉じ、つかめないまま空の手で皿へ向かう例を選定した。')
    table([
        ['モデルとGPU', 'LIBERO Spatial向け公開微調整済みOpenVLA 7B / NVIDIA L4 / BF16 / Torch 2.2.2+cu121'],
        ['シミュレータ', 'LIBERO-PRO標準Spatial / MuJoCo 2.3.7 / robosuite 1.4.1 / OSC_POSE / 20 Hz'],
        ['入力と初期化', 'VLAはagentview 1枚＋指示。256px観測を224pxへ前処理（中心crop面積0.9）。推論seed=7。表示・記録には手首カメラも使用。'],
    ], [94, 417], padding=7)
    pictures([('failed-agentview.png', 'VLA単独 step85 / 空の手で移動'),
              ('grasp-agentview.png', '修正後 step76 / ボウルを把持'),
              ('goal-agentview.png', 'VLA再開後 step106 / 皿へ')], 161)
    table([
        ['区間', 'step数', '実行した動き'],
        ['VLAの共通部分', '38', '対象付近まで接近。ここへ巻き戻して分岐。'],
        ['代理キー操作', '38', '開く、位置合わせ、閉じる、約4.4cm持ち上げる。'],
        ['再開後のVLA', '30', '皿まで移動し、LIBERO goalへ到達。ボウルの始終点間距離は約13.7cm。'],
    ], [113, 48, 350], padding=7)
    add('確認できた結果', 'h2')
    add('VLA単独は探索時180 stepでgoalなし。制御step末尾の接触ログでは、グリッパーと対象物の接触は0回だった。撮り直した179 stepも、対応する全指令と物理状態が探索記録に一致した。修正経路は106 step（VLA計68、TEST_OPERATOR計38）でreward=1・goal=trueとなった。')
    add('初期状態と全106 step後の92要素の物理状態を、独立した再実行で完全一致と確認。記録した非対象3物体（別のボウル・箱・ラメキン）の初期位置からの最大L1変位も1mm以内だった。画像や全SDK内部配列の一致を保証する指標ではない。', 'small')
    add('特定の一例を選んだデモで、成功率向上の統計評価ではない。goalはLIBEROのOn(ボウル, 皿)条件（接触と位置関係）で判定。グリッパー解放後の安定性、学習効果、未知環境への汎化は未検証。', 'small')

    page('03  対応範囲と、データとして残すもの')
    table([
        ['接続先', '確認した範囲', '制約'],
        ['LIBERO / OSC', '今回の63秒動画。実VLA、局所修正、再開、巻き戻し・分岐・採用、106 step再実行を確認。',
         '対象は選定した1配置。全タスクの成功率や追加学習の効果は未評価。'],
        ['Genesis / CPU', '別試験で手動・記録・実LLMコード実行・切替・状態復元を確認。実VLAの短い接続試験も保存。',
         '把持タスク成功やLIBERO学習済みVLAの適応能力を確認した試験ではない。'],
        ['ROS 2 / MoveIt / RViz', '別試験で実IK service、FollowJointTrajectory、JointState、TF、RViz表示への接続を確認。',
         '独自6軸の数値参照controller。物理・接触・衝突回避・巻き戻しなし。'],
        ['Gazebo / 実機', '今後、共通backendの先へadapterを追加する接続先。', '現時点では未対応。ゲームパッドも今回の提出対象外。'],
    ], [105, 214, 192], padding=8)
    add('画像・指示・操作・出所を対応付ける', 'h2')
    add('採用した経路をVLA用の学習候補へ出力し、画像・指示・状態・実行指令・操作元を対応付ける。分岐の親子関係と採用終点も保存する。選んだことと学習への投入は分け、今回もtraining_admitted=falseを維持した。')
    add('VLM／コード生成側は、LLMが生成して実際に実行したPythonを別の候補として記録する。VLAの数値軌道から作る再生コードは意味的なCode as Policyとは区別する。今回のLIBERO動画ではLLM生成コードを実行していない。')
    add('LLMとGPUの位置付け', 'h2')
    add('今回のL4はVLA推論用。実LLMの別試験にはMac MLXのQwen2.5-VL-7Bを使用した。LLMのHTTP接続口とCaP-X実行器を使うことで、API提供モデルやGPUサーバー上のモデルを差し替える構成にしている。', 'small')
    add('今回の到達点と残る検証', 'h2')
    add('今回の到達点は、制御の切替・分岐・採用と、選定した1配置での局所修正・VLA再開を記録し、再実行できたこと。VLA用の操作データと、VLM／コード生成用の実行コードの候補を扱う基盤とした。今回のデモは代理操作の1例で、人間のデータによる有効性や学習効果は評価していない。追加する環境や実機、開発の順序は未決定。')
    add('GENIAC Prize等でのCode as Policy利用は開発動機であり、特定の大会SDKへの対応済みという意味ではない。', 'small')
    add('状態復元は対応する同一scene／reset内の能力。ROS 2や実機に、シミュレータと同じ物理的な巻き戻しや即時安全停止を保証するものではない。', 'small')

    page('04  提出物、コード、再現情報')
    add('提出用ファイルと動画', 'h2')
    table([
        ['説明PDF', '説明資料_PAI最終課題_takumiecd.pdf'],
        ['動画ファイル／タイトル', 'PAI最終課題_takumiecd.mp4 / PAI最終課題_takumiecd'],
        ['動画形式', '63秒 / 1920×1080 / H.264 / 字幕付き・音声なし'],
        ['再生の扱い', '記録観測の編集再生。推論待ちの短縮と代理操作の出所を動画内に明記。実時間の推論速度を示す映像ではない。'],
    ], [120, 391], padding=7)
    add('対応動画は、把持修正・VLA復帰・経路比較を示した63秒版。YouTubeの動画URLはOmniCampusの指定欄へ提出する。', 'small')
    add('公開コードと再現手順', 'h2')
    add('リポジトリ：'+link(REPO, REPO))
    add('今回の再現用コード：'+link(source_commit[:12], REPO+'/tree/'+source_commit)+'<br/>'
        '実行時Runtimeのbase：'+evidence['runtime_base_commit'][:12]+'。実験スクリプトの当時の版も記録と共に保存。', 'small')
    for name, note in [
        ('docs/verification/grasp-screen-20261102.md', '候補探索、実測、制約、同じ配置を開く手順'),
        ('docs/verification/grasp-repair-20261102.json', 'モデル版、操作数、再実行検証、動画hash'),
        ('docs/runtime.md', 'ブラウザ／CLI／API／Runtime／backendの構成'),
        ('configs/relay/grasp-demo.json', 'task0 / init41 / 256px / 300 step上限の設定'),
    ]:
        add(link(name, REPO+'/blob/'+source_commit+'/'+name)+'<br/>'+note, 'small')
    add('使用モデルと既存部品への帰属', 'h2')
    add('VLA：openvla/openvla-7b-finetuned-libero-spatial<br/>'
        'weights：'+evidence['model_revision']+'<br/>'
        'code：'+evidence['code_revision'], 'small')
    for label, url in [
        ('CaP-X / Pythonコード実行器', 'https://github.com/capgym/cap-x'),
        ('OpenVLA / 公開VLA', 'https://github.com/openvla/openvla'),
        ('LIBERO / ロボット操作タスク', 'https://github.com/Lifelong-Robot-Learning/LIBERO'),
        ('Genesis / シミュレータ', 'https://github.com/Genesis-Embodied-AI/Genesis'),
        ('ROS 2 / MoveIt 2', 'https://github.com/moveit/moveit2'),
    ]:
        add(link(label, url), 'small')
    add('生の画像・操作・復元状態・操作元・モデル出所・hashを保存。採用経路は学習投入前の候補として扱い、失敗経路も保持する。図中の写真には表示向きを補正した実際の観測を使用した。', 'small')

    def footer(c, doc):
        c.setFont('JP', 8)
        c.setFillColor(colors.HexColor(MUTED))
        c.drawString(42, 817, 'RELAY / PAI FINAL PROJECT / GRASP REPAIR')
        c.setStrokeColor(colors.HexColor('#dce4ef'))
        c.line(42, 39, 553, 39)
        c.drawString(42, 24, f'takumiecd  |  {REPORT_DATE} 改訂  |  実行記録に基づく資料')
        c.drawRightString(553, 24, f'{doc.page:02d} / 05')

    SimpleDocTemplate(str(path), pagesize=(595.276, 841.89), leftMargin=42, rightMargin=42,
                      topMargin=55, bottomMargin=47,
                      title='Relay - 自律操作の局所修正と分岐型データ収集',
                      author=ACCOUNT).build(story, onFirstPage=footer, onLaterPages=footer)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--capture', type=Path, default=ROOT/'output/grasp-miss-repair-20261102')
    parser.add_argument('--output', type=Path, default=ROOT/'output/pdf/grasp-repair')
    parser.add_argument('--package', type=Path, default=ROOT/'output/submission-grasp-repair-takumiecd')
    parser.add_argument('--assignment-pdf', type=Path)
    parser.add_argument('--replace', action='store_true', help='Replace only this revised output')
    args = parser.parse_args()
    evidence, parent, repair_root, repair = audit(args.capture)
    old = [ROOT/'output/pdf/説明資料_PAI最終課題_takumiecd.pdf',
           ROOT/'output/pdf/polished/説明資料_PAI最終課題_takumiecd.pdf']
    preserved = {str(p.relative_to(ROOT)): digest(p) for p in old if p.is_file()}
    pdf = args.output/'説明資料_PAI最終課題_takumiecd.pdf'
    if pdf.exists() and not args.replace:
        raise FileExistsError('Choose a fresh revised output or explicitly use --replace')
    if pdf.resolve() in {p.resolve() for p in old}:
        raise ValueError('Never overwrite previous submissions')
    args.output.mkdir(parents=True, exist_ok=True)
    args.package.mkdir(parents=True, exist_ok=True)
    assets = args.output/'assets'
    assets.mkdir(exist_ok=True)
    frames = [
        ('failed-agentview.png', args.capture/'scene/trajectory', parent[84], 'agentview'),
        ('grasp-agentview.png', repair_root, repair[37], 'agentview'),
        ('goal-agentview.png', repair_root, repair[-1], 'agentview'),
        ('goal-wrist.png', repair_root, repair[-1], 'wrist'),
    ]
    materials = {}
    for name, trajectory, row, camera in frames:
        obs = read(trajectory/row['next_observation'])
        source = trajectory/obs['images'][camera]
        image = Image.open(source).convert('RGB')
        assert image.size == (256, 256)
        if obs['image_convention'] == 'libero_raw':
            image = image.transpose(Image.Transpose.ROTATE_180)
        ImageOps.exif_transpose(image).save(assets/name)
        materials[str(source.relative_to(ROOT))] = digest(source)
    source_commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    make_report(pdf, assets, evidence, source_commit)
    from pypdf import PdfReader
    reader = PdfReader(pdf)
    if len(reader.pages) != 5:
        raise ValueError(f'Expected 5 pages; got {len(reader.pages)}. Inspect and revise layout.')
    shutil.copy2(pdf, args.package/pdf.name)
    video = args.capture/'deliverables'/evidence['video']['file']
    packaged_video = args.package/f'PAI最終課題_{ACCOUNT}.mp4'
    shutil.copy2(video, packaged_video)
    description = (args.capture/'deliverables/YouTube_description.txt').read_text().splitlines()
    description[0] = f'PAI最終課題_{ACCOUNT}'
    (args.package/'YouTube_description.txt').write_text('\n'.join(description)+'\n')
    (args.package/'提出手順.txt').write_text(
        f'今回の提出セット / {ACCOUNT}\n\n'
        f'1. PAI最終課題_{ACCOUNT}.mp4 をYouTubeへアップロード。\n'
        f'   タイトルは PAI最終課題_{ACCOUNT}。概要欄はYouTube_description.txtを使用。\n'
        '   公開または限定公開。子ども向け・年齢制限・プレミア公開は設定しない。\n'
        f'2. 説明資料_PAI最終課題_{ACCOUNT}.pdf をOmniCampusへ提出。\n'
        f'3. GitHubリポジトリURL {REPO} と、新しい動画のYouTube URLを指定欄へ入力。\n\n'
        f'この版のコードの固定URL：{REPO}/tree/{source_commit}\n'
        '採用経路はVLA 38 → 代理操作38 → VLA 30、計106step。\n'
        '動画は63秒。学習への投入は行っていない。\n'
        'ファイルの準備のみで、YouTubeアップロードとOmniCampus送信はまだ行っていない。\n')
    for name, expected in preserved.items():
        assert digest(ROOT/name) == expected, 'Previous submission changed'
    manifest = dict(schema='relay.grasp-submission.v1', account=ACCOUNT, source_commit=source_commit,
                    verification=evidence, preserved_reports=preserved,
                    report_sha256=digest(pdf), video_sha256=digest(packaged_video),
                    report_pages=len(reader.pages), builder_sha256=digest(Path(__file__)),
                    material_sha256=materials, uploaded=False, submitted=False)
    if args.assignment_pdf:
        manifest['assignment'] = dict(file=args.assignment_pdf.name, sha256=digest(args.assignment_pdf))
    (args.package/'deliverables.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2)+'\n')
    print(json.dumps(dict(pdf=str(pdf), package=str(args.package), pages=len(reader.pages)), ensure_ascii=False))


if __name__ == '__main__':
    main()
