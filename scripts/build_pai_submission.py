#!/usr/bin/env python3
"""Build the PAI report and a captioned video from verified local artifacts.

Optional authoring dependencies: reportlab, Pillow, ffmpeg / ffprobe.
Screenshots are captured through the UI; this script never fabricates simulator frames.
"""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import textwrap

from PIL import Image, ImageDraw, ImageFont, ImageOps
from reportlab.pdfgen import canvas
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.lib.colors import HexColor
from reportlab.lib.utils import ImageReader

ROOT = Path(__file__).resolve().parents[1]
FONT = Path('/System/Library/Fonts/Supplemental/Arial Unicode.ttf')
BG, INK, BLUE, MUTED = '#f2f5fa', '#20344c', '#366bc0', '#64758b'
REPO = 'https://github.com/takumiecd/cap-x/tree/codex/vla-integration'


def verify_sources(output):
    info = json.loads((ROOT/'docs/verification/vla-osc-20261102.json').read_text())
    source = ROOT/'output/vla-live-02/osc-vla.mp4'
    actual = hashlib.sha256(source.read_bytes()).hexdigest()
    expected = info['raw_artifact_sha256']['output/vla-live-02/osc-vla.mp4']
    if actual != expected:
        raise ValueError('Real VLA video hash mismatch')
    for name in ('console-start.jpg', 'console-rewind.jpg', 'console-manual.jpg', 'console-adopted.jpg'):
        if not (output/'frames'/name).is_file():
            raise FileNotFoundError(name)
    return info


def create_report(path, output, account, info, commit):
    pdfmetrics.registerFont(TTFont('JP', str(FONT)))
    c = canvas.Canvas(str(path), pagesize=(595.276, 841.89))
    c.setTitle('Relay - PAI最終課題説明資料')
    c.setAuthor(account)
    c.setSubject('VLA・コード・キーボード操作の切替と分岐型データ収集')
    W, H, M = 595.276, 841.89, 42
    def text(x, y, value, size=10, color=INK):
        c.setFillColor(HexColor(color));c.setFont('JP', size);c.drawString(x,y,value)
    def paragraph(value, y, width=511, size=10.2, leading=17, color=INK):
        line=''
        for char in value:
            if char=='\n' or pdfmetrics.stringWidth(line+char,'JP',size)>width:
                text(M,y,line,size,color);y-=leading;line='' if char=='\n' else char
            else:line+=char
        if line:text(M,y,line,size,color);y-=leading
        return y
    def page(number, section):
        c.setFillColor(HexColor('#ffffff'));c.rect(0,0,W,H,fill=1,stroke=0)
        text(M,H-35,'RELAY  /  PAI FINAL PROJECT',8,BLUE)
        text(W-150,H-35,f'{number:02d}  /  {section}',8,MUTED)
        c.setStrokeColor(HexColor('#dce4ef'));c.line(M,39,W-M,39)
        text(M,25,'CaP-X拡張 / LIBERO・MuJoCo / キーボード操作',7.5,MUTED)
        text(W-100,25,account,7.5,MUTED)
    def heading(value,y):
        text(M,y,value,15,BLUE);return y-28
    def box(x,y,w,h,title,body,color=BLUE):
        c.setFillColor(HexColor('#f2f5fa'));c.setStrokeColor(HexColor('#d9e2ef'))
        c.roundRect(x,y,w,h,7,fill=1,stroke=1)
        text(x+12,y+h-22,title,11,color)
        for i,line in enumerate(body):text(x+12,y+h-42-i*15,line,8.5,MUTED)
    def arrow(x1,y1,x2,y2):
        c.setStrokeColor(HexColor('#9cacc1'));c.setLineWidth(1.2);c.line(x1,y1,x2,y2)
        c.line(x2,y2,x2-4,y2+5);c.line(x2,y2,x2+4,y2+5)

    page(1,'概要')
    text(M,755,'Relay',34)
    text(M,721,'自律操作の「ここだけ」を直し、経路を残す。',17,BLUE)
    y=paragraph('VLA・コード・キーボード操作を切り替え、途中の状態へ戻って修正した経路をデータとして選択するツール。上流CaP-Xを拡張し、最初の対象をLIBERO／OSCとした。',689,size=10.5)
    shot=Image.open(output/'frames/console-adopted.jpg')
    ih=318;iw=ih*shot.width/shot.height
    c.drawImage(ImageReader(shot),M,610-ih,width=iw,height=ih)
    for offset,title,body in [(0,"01  操作を切り替える","VLA / コード / キーボード"),
                              (88,"02  途中へ戻って修正","元の未来を消さずに分岐"),
                              (176,"03  採用する経路を選ぶ","画像・操作・出所を対応付け")]:
        text(415,587-offset,title,9.5,BLUE)
        text(415,568-offset,body,7.8,MUTED)
    y=610-ih-24
    text(M,y,'目的と開発の動機',13,BLUE);y-=24
    y=paragraph('VLAの軌道全体を捨てず、苦手な局所区間だけを修正したい。最終課題のためだけでなく、GENIAC PrizeなどのCode as Policy型の利用も見据え、制御の切替と記録を共通化した。',y)
    y=paragraph('今回の対象タスクは、黒いボウルを皿へ移すLIBEROの場面。確認したのは実VLAの物理動作と、切替・巻き戻し・分岐・経路選択の機能であり、把持・配置の成功は未確認である。',y-5)
    c.showPage()

    page(2,'仕組みと工夫')
    y=heading('同じ物理環境へ、操作の出所をそろえて接続する',757)
    box(42,618,157,91,'VLA', ['画像・指示 → 操作値','OpenVLAで実モデル接続済み'])
    box(219,618,157,91,'LLM / Python', ['HTTP接続口 → コード実行','実LLMの推論は未検証'], '#8263a4')
    box(396,618,157,91,'キーボード', ['Space＋WASD / RF','Escで停止、C/Vで開閉'], '#9b773f')
    for x in (120,298,475):arrow(x,618,298,576)
    box(42,486,511,90,'切替・停止と、単一の実行入口', ['mode / epoch / leaseを照合し、切替前の遅れた推論結果を適用しない。','LIBERO／MuJoCoのOSCへ相対指令を渡し、観測・操作・出所を保存する。'])
    arrow(298,486,298,460)
    box(42,367,511,92,'物理状態を保存 → 分岐 → 採用経路を固定', ['位置だけでなく、OSC目標・グリッパー・観測cache・乱数状態も保存する。','採用は表示位置と独立。元の未来を消さず、親の共通区間と修正区間をつなぐ。'])
    y=heading('既存の部品と、自分たちが追加した部分',333)
    y=paragraph('既存：CaP-Xのコード実行器、LIBERO／MuJoCo、robosuiteのOSC、事前学習済みOpenVLAを使用。物理エンジンやAIモデルを新規開発した作品ではない。',y)
    y=paragraph('追加：VLA共通adapter・通信口、制御主体の切替と失効処理、キーボードUI、制御器を含むcheckpoint、分岐図、採用経路の固定と学習候補の書き出し。',y-7)
    y=heading('ROS 2を今は挟まなかった理由',y-12)
    y=paragraph('最終課題は他のツールも使用可。LIBEROにはOSCがあるため、まずPythonで観測・操作・記録を一貫させることを優先した。ROS 2・Gazebo・Genesisは現在未実装で、実機・別シミュレータへの接続先として今後検討する。',y)
    y=paragraph('GENIAC Prizeへの対応は開発の動機と将来目標であり、特定の大会SDKや評価環境へ対応済みという意味ではない。',y-5,size=9.5,color=MUTED)
    c.showPage()

    page(3,'検証と限界')
    y=heading('確認できたことを、実モデルと機能試験に分ける',757)
    rows=[
        ('実OpenVLA / NVIDIA L4','30回の実推論を30回のOSC指令へ接続。手先の始点・終点の距離は約0.269 m。実行は約28.4秒。把持・配置成功は未確認。'),
        ('キーボード・分岐・採用','キー入力の再現で27stepを修正。親の共通80step＋修正27step＝107stepを別の分岐として保存・選択した。'),
        ('状態復元・数値再生','別の63stepの分岐経路を再実行し、全64状態の物理状態と手先位置の最大誤差が0。同じruntimeでの当該経路の結果。'),
        ('回帰試験','Python 44件、JavaScript 12件の対象試験を確認。UI変更後もキー入力・分岐図の12件を再確認。'),
    ]
    for title,body in rows:
        c.setFillColor(HexColor('#f3f6fa'));c.roundRect(M,y-80,511,86,6,fill=1,stroke=0)
        text(M+12,y-14,title,11,BLUE)
        # Use the same paragraph wrapper in a shifted local coordinate frame.
        c.saveState();c.translate(12,0);paragraph(body,y-34,width=484,size=9.5,leading=15);c.restoreState()
        y-=94
    y=heading('提出動画の読み方と、未検証の範囲',y-8)
    y=paragraph('前半はSHA256照合済みの実OpenVLA推論記録。後半は試験用providerとキー入力の再現によるUI・記録機能の確認であり、実VLA・実LLM・実ユーザーの連続した共同作業を検証した映像ではない。',y,size=9.5,leading=16)
    y=paragraph('LLM側は固定コードで制御経路を確認した段階。学習・性能改善・タスク成功率・他環境への汎化は主張しない。経路の選択はselected_for_dataset=trueで記録し、training_admitted=falseを維持する。',y-5,size=9.5,leading=16)
    y=heading('コード・出典・再現情報',y-10)
    y=paragraph('提出コード：'+REPO,y,size=8.4,leading=14)
    y=paragraph('ソース：'+commit[:12]+' / docs/hybrid-console.md / docs/verification/',y,size=8.4,leading=14)
    y=paragraph('使用部品：github.com/capgym/cap-x / github.com/openvla/openvla\n実モデル：openvla/openvla-7b-finetuned-libero-spatial\n版：962318cec55ac10993ff0f5f43eda9a270b4c873',y,size=8.2,leading=13,color=MUTED)
    c.showPage();c.save()


def font(size):
    return ImageFont.truetype(str(FONT),size)


def wrap_text(draw, text, x, y, width, size=30, color=INK, leading=None):
    line='';leading=leading or int(size*1.6)
    for char in text:
        if char=='\n' or draw.textlength(line+char,font=font(size))>width:
            draw.text((x,y),line,font=font(size),fill=color);y+=leading;line='' if char=='\n' else char
        else:line+=char
    if line:draw.text((x,y),line,font=font(size),fill=color);y+=leading
    return y


def card(output,name,title,body,number=None,screenshot=None):
    image=Image.new('RGB',(1920,1080),BG);d=ImageDraw.Draw(image)
    d.text((66,42),'RELAY  /  PAI FINAL PROJECT',font=font(24),fill=BLUE)
    if number:d.text((1680,42),number,font=font(24),fill=MUTED)
    if screenshot:
        d.text((66,100),title,font=font(42),fill=INK)
        source=Image.open(screenshot).convert('RGB')
        source=ImageOps.contain(source,(1120,850),Image.Resampling.LANCZOS)
        image.paste(source,(66+(1120-source.width)//2,180+(850-source.height)//2))
        wrap_text(d,body,1250,260,600,30)
    else:
        y=wrap_text(d,title,100,240,1700,66,leading=91)
        wrap_text(d,body,108,y+50,1640,35,leading=62)
    d.line((66,1035,1854,1035),fill='#d4deeb',width=2)
    d.text((66,1048),'CaP-X / LIBERO / MuJoCo',font=font(19),fill=MUTED)
    path=output/'video-assets'/f'{name}.png';path.parent.mkdir(exist_ok=True)
    image.save(path);return path


def ffmpeg(args):
    subprocess.run(['ffmpeg','-y','-hide_banner','-loglevel','error',*map(str,args)],check=True)


def create_video(output, account):
    assets=output/'video-assets';assets.mkdir(exist_ok=True)
    segments=[]
    encoding=['-an','-c:v','libx264','-preset','veryfast','-crf','21','-pix_fmt','yuv420p','-r','24','-threads','2']
    def still(name,title,body,duration,screenshot=None):
        png=card(output,name,title,body,screenshot=screenshot)
        segment=assets/f'{name}.mp4'
        ffmpeg(['-loop','1','-framerate','24','-i',png,'-t',duration,*encoding,segment]);segments.append(segment)
    frames=output/'frames'
    still('01-intro','自律操作の「ここだけ」を直す。','Relay - VLA・コード・キーボード操作を切り替えるデータ収集支援\nPAI最終課題 / '+account+'\n目的：元の経路を残し、局所的な修正をデータとして選ぶ。',10)
    bg=assets/'02-vla-background.png'
    image=Image.new('RGB',(1920,1080),BG);d=ImageDraw.Draw(image)
    d.text((66,42),'RELAY  /  PAI FINAL PROJECT',font=font(24),fill=BLUE)
    d.text((66,100),'実OpenVLAの推論記録',font=font(42),fill=INK)
    wrap_text(d,'実モデル：OpenVLA\nGPU：NVIDIA L4\n\n30回の推論をOSCへ接続\n手先の始点・終点の距離：約0.269 m\n\n物理時間基準の0.05倍スロー再生。\n推論待ち時間は省略。\n把持・配置の成功は未確認。',1010,220,780,31)
    image.save(bg)
    segment=assets/'02-real-vla.mp4'
    ffmpeg(['-loop','1','-framerate','24','-i',bg,'-i',ROOT/'output/vla-live-02/osc-vla.mp4',
            '-filter_complex','[1:v]setpts=20*PTS,fps=24,scale=740:740,tpad=stop_mode=clone:stop_duration=2[v];[0:v][v]overlay=130:210:shortest=1[out]',
            '-map','[out]','-t','30',*encoding,segment]);segments.append(segment)
    still('03-console','キーボードだけで、途中へ介入する。','ここからはUI・記録機能の動作確認です。\n\n試験providerとキー入力の再現を使用。\n実モデルの連続共同操作とは区別します。\n\nH：手動へ\nSpace＋WASD / RF：移動\nEsc：停止',12,frames/'console-start.jpg')
    still('04-rewind','01  記録したstepへ戻る','分岐図のstepを選び、物理状態を復元。\n\n今回はstep 80へ巻き戻しました。\n位置だけでなく、グリッパーやOSCの状態も戻します。\n\n元の未来は消しません。',10,frames/'console-rewind.jpg')
    times=json.loads((frames/'capture-times.json').read_text())
    concat=[]
    for i,item in enumerate(times):
        png=card(output,f'motion-{i:03d}','02  キーボードで局所的に修正','Space＋Rで手先を上へ。\n\nこの実演では27stepを追加しました。\n\nキー入力の再現を0.25倍スローで表示。\n動画用の架空のロボット状態は生成していません。',screenshot=frames/item['file'])
        next_time=times[i+1]['seconds'] if i+1<len(times) else 1.8
        duration=(next_time-(item['seconds'] if i else 0))*4
        concat += [f"file '{png.as_posix()}'",f'duration {max(.04,duration):.6f}']
    last=card(output,'motion-end','02  キーボードで局所的に修正','27stepの修正後にEscで停止。\n\n元の80stepを引き継いだ新しい分岐は、107stepになりました。',screenshot=frames/'console-manual.jpg')
    concat += [f"file '{last.as_posix()}'",'duration 1.2',f"file '{last.as_posix()}'"]
    (assets/'motion.ffconcat').write_text('\n'.join(concat)+'\n')
    segment=assets/'05-manual.mp4'
    ffmpeg(['-f','concat','-safe','0','-i',assets/'motion.ffconcat','-t','8.4',*encoding,segment]);segments.append(segment)
    still('06-adoption','03  採用する経路を固定する','共通80step ＋ 修正27step\n＝ 107stepの経路を選択。\n\n観測・実行済み操作・コードの出所を対応付けます。\n\nその後に操作を続けても、採用範囲は自動で伸びません。',13,frames/'console-adopted.jpg')
    still('07-design','講義の課題から、今後も使う道具へ。','既存のCaP-X・LIBERO・OSC・OpenVLAを利用。\n追加：切替・状態復元・分岐・経路の選択。\n\nGENIAC Prizeなどへの再利用は将来目標。\nROS 2・Gazebo・Genesisは未実装。',10)
    still('08-limits','確認した範囲と、次の一歩。','実モデル接続と、切替・復元・分岐・経路選択を確認。\n実LLM・実ユーザーとの連続共同操作、タスク成功は未確認。\n\n採用データは学習投入前の候補。\n検証条件はPDFとコードに記載。',12)
    (assets/'segments.ffconcat').write_text('\n'.join(f"file '{p.as_posix()}'" for p in segments)+'\n')
    final=output/f'PAI最終課題_{account}.mp4'
    ffmpeg(['-f','concat','-safe','0','-i',assets/'segments.ffconcat','-c','copy','-movflags','+faststart',final])
    metadata=json.loads(subprocess.check_output(['ffprobe','-v','error','-show_entries','format=duration','-of','json',str(final)]))
    duration=float(metadata['format']['duration'])
    if not 60<=duration<=180:raise ValueError(f'Invalid submission duration {duration}')
    return final,duration


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--account',required=True)
    parser.add_argument('--output',type=Path,default=ROOT/'output/submission-takumiecd')
    parser.add_argument('--pdf-output',type=Path,default=ROOT/'output/pdf')
    parser.add_argument('--no-video',action='store_true')
    args=parser.parse_args()
    output=args.output.resolve();output.mkdir(parents=True,exist_ok=True)
    args.pdf_output.mkdir(parents=True,exist_ok=True)
    info=verify_sources(output)
    commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()
    pdf=args.pdf_output/f'説明資料_PAI最終課題_{args.account}.pdf'
    create_report(pdf,output,args.account,info,commit)
    result=dict(pdf=str(pdf),code_commit=commit,github_url=REPO,training_admitted=False)
    if not args.no_video:
        video,duration=create_video(output,args.account)
        result.update(video=str(video),video_seconds=duration)
    (output/'deliverables.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(result,ensure_ascii=False),flush=True)


if __name__=='__main__':main()
