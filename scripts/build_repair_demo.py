"""Render an edited demo from actual recorded observations and UI captures.

No model inference, simulated actions, uploads, or changes to the session.
The storyboard explicitly supplies every clip and its playback duration.
"""

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import subprocess

import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageOps

WIDTH, HEIGHT, FPS = 1920, 1080, 12
BG, INK, MUTED = '#f2f5fa', '#20344c', '#64758b'
BLUE, GREEN = '#366bc0', '#398778'
FONT = '/System/Library/Fonts/Supplemental/Arial Unicode.ttf'


def read(path):
    return json.loads(Path(path).read_text())


def rows(path):
    return [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def audit(root, story):
    scene = root / story['scene']
    manifest = read(scene / 'history/manifest.json')
    selection = manifest['adopted']
    assert selection and selection['training_admitted'] is False
    route = rows(scene / selection['route'])
    candidates = rows(scene / selection['candidates'])
    assert len(route) == len(candidates) == selection['end_step']
    assert sha(scene / selection['route']) == selection['route_sha256']
    assert all(r['training_admitted'] is False for r in candidates)
    sources = Counter(r['source'] for r in candidates)
    assert sources['VLA'] > 0 and sources['TEST_OPERATOR'] > 0
    assert not any(s.startswith('TEST_VLA') for s in sources)

    original = scene / 'trajectory'
    branch = scene / 'branches' / selection['branch'] / 'trajectory'
    parent, repair = rows(original / 'steps.jsonl'), rows(branch / 'steps.jsonl')
    fork = story['fork_step']
    origin = parent[fork - 1]['next_observation'] if fork else read(original / 'episode.json')['initial_observation']
    a = np.load(original / read(original / origin)['physics_state'], allow_pickle=False)
    b = np.load(branch / read(branch / repair[0]['observation'])['physics_state'], allow_pickle=False)
    np.testing.assert_array_equal(a, b)
    manual = [r for r in repair if r['source'] == 'TEST_OPERATOR']
    assert manual
    displacement = np.array(manual[-1]['eef_after_m']) - manual[0]['eef_before_m']
    assert np.linalg.norm(displacement) > .03, 'Manual movement must be visible (>3cm)'
    assert any(r['source'] == 'VLA' for r in repair[repair.index(manual[-1]) + 1:]), 'Resume VLA after repair'
    replay = read(root/'replay-verification.json')
    assert replay['steps'] == len(route) and replay['every_replayed_physics_state_exact'] is True
    return dict(
        schema='relay.repair-demo.v1', source_commit=story['source_commit'],
        parent_steps=len(parent), fork_step=fork, repair_steps=len(repair),
        selected_steps=len(candidates), selected_sources=dict(sources),
        manual_displacement_m=displacement.tolist(),
        restored_physics_vector_exact=True, compared_state_shape=list(a.shape),
        training_admitted=False,
        task_completed=bool(route[-1].get('task_completed', False)),
        terminal_reward=route[-1]['reward'], terminated=route[-1]['terminated'],
        every_replayed_physics_state_exact=True,
        replay_refresh_differences=replay['refresh_differences'],
        route_sha256=selection['route_sha256'],
        limitations=story['limitations'],
    )


def build(root, story, output):
    result = audit(root, story)
    fonts = {size: ImageFont.truetype(FONT, size) for size in [22, 26, 30, 34, 44, 64]}
    assets = set()
    clip_cache = {}

    def picture(path, size):
        path = Path(path)
        assets.add(path)
        key = (path, size)
        if key not in clip_cache:
            if len(clip_cache) >= 24:
                clip_cache.clear()
            with Image.open(path) as image:
                clip_cache[key] = ImageOps.contain(image.convert('RGB'), size, Image.Resampling.LANCZOS)
        return clip_cache[key]

    def paste(image, path, box):
        x, y, w, h = box
        p = picture(path, (w, h))
        image.paste(p, (x + (w - p.width)//2, y + (h - p.height)//2))

    def observations(clip):
        base = root / clip['trajectory']
        data = rows(base / 'steps.jsonl')[clip['start']:clip['end']]
        assert data, clip
        assets.add(base / 'steps.jsonl')
        return [(base, r) for r in data]

    def camera(image, frame, box, camera_name):
        base, row = frame
        obs_path = base / row['next_observation']
        assets.add(obs_path)
        obs = read(obs_path)
        # Recorder stores raw LIBERO camera convention; display must orient it.
        path = base / obs['images'][camera_name]
        assets.add(path)
        key = (path, box[2:])
        if key not in clip_cache:
            if len(clip_cache) >= 24:
                clip_cache.clear()
            with Image.open(path) as source:
                p = source.convert('RGB')
                if obs.get('image_convention') == 'libero_raw':
                    p = p.transpose(Image.Transpose.ROTATE_180)
                clip_cache[key] = ImageOps.contain(p, box[2:], Image.Resampling.LANCZOS)
        x, y, w, h = box
        p = clip_cache[key]
        image.paste(p, (x+(w-p.width)//2, y+(h-p.height)//2))

    output.parent.mkdir(parents=True, exist_ok=True)
    assert not output.exists(), 'Choose a fresh video output path'
    command = ['ffmpeg', '-v', 'error', '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-s', f'{WIDTH}x{HEIGHT}',
               '-r', str(FPS), '-i', '-', '-an', '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '19',
               '-pix_fmt', 'yuv420p', '-movflags', '+faststart', str(output)]
    process = subprocess.Popen(command, stdin=subprocess.PIPE)
    chapters, cursor = [], 0
    try:
        for clip in story['clips']:
            count = round(clip['seconds']*FPS)
            assert count > 0
            chapters.append(dict(start_seconds=cursor/FPS, title=clip['title'], duration=count/FPS))
            frames = observations(clip) if clip['kind'] == 'camera' else None
            compare = [observations(c) for c in clip['routes']] if clip['kind'] == 'compare' else None
            for i in range(count):
                image = Image.new('RGB', (WIDTH, HEIGHT), BG)
                draw = ImageDraw.Draw(image)
                draw.text((64, 30), 'Relay', font=fonts[44], fill=INK)
                draw.text((250, 45), clip.get('eyebrow', 'VLA × キーボード × 分岐記録'), font=fonts[26], fill=MUTED)
                draw.text((64, 112), clip['title'], font=fonts[44], fill=INK)
                if clip['kind'] == 'camera':
                    frame = frames[min(len(frames)-1, i*len(frames)//count)]
                    camera(image, frame, (64, 210, 940, 710), 'agentview')
                    camera(image, frame, (1040, 285, 800, 600), 'wrist')
                    draw.text((72, 187), '全体カメラ', font=fonts[26], fill=MUTED)
                    draw.text((1048, 245), '手首カメラ', font=fonts[26], fill=MUTED)
                    draw.text((1050, 900), f"出所: {frame[1]['source']}", font=fonts[26], fill=BLUE)
                elif clip['kind'] == 'ui':
                    paste(image, root / clip['image'], (64, 195, 1792, 735))
                elif clip['kind'] == 'compare':
                    for j, data in enumerate(compare):
                        # Same relative action step in both panels; hold the shorter route's last image.
                        frame = data[min(len(data)-1, i*max(map(len, compare))//count)]
                        camera(image, frame, (64+j*920, 255, 870, 640), 'agentview')
                        draw.text((70+j*920, 205), clip['routes'][j]['label'], font=fonts[30], fill=BLUE if j == 0 else GREEN)
                elif clip['kind'] == 'summary':
                    lines = [f"採用経路：{result['selected_steps']} steps",
                             f"操作の出所：{result['selected_sources']}",
                             f"手動修正の移動量：{np.linalg.norm(result['manual_displacement_m'])*100:.1f} cm",
                             '巻き戻し地点の保存状態：完全一致',
                             'route.jsonl / vla-candidates.jsonl / selection.json',
                             '学習候補として保存。学習投入は別工程。']
                    for j, line in enumerate(lines):
                        draw.text((95, 270+j*90), line, font=fonts[34], fill=INK)
                elif clip['kind'] == 'title':
                    for j, line in enumerate(clip['lines']):
                        draw.text((95, 300+j*110), line, font=fonts[64 if j == 0 else 44], fill=INK if j == 0 else BLUE)
                else:
                    raise ValueError(clip['kind'])
                draw.rectangle((0, 955, WIDTH, HEIGHT), fill='white')
                draw.text((64, 978), clip['caption'], font=fonts[30], fill=INK)
                draw.text((64, 1030), clip.get('disclosure', '実行記録に基づく編集映像 / 代理キー操作はTEST_OPERATOR'), font=fonts[22], fill=MUTED)
                draw.rectangle((0, HEIGHT-5, int(WIDTH*(cursor+i+1)/(sum(round(c['seconds']*FPS) for c in story['clips']))), HEIGHT), fill=BLUE)
                process.stdin.write(image.tobytes())
            cursor += count
            print(json.dumps({'chapter': clip['title'], 'frames': count}), flush=True)
        process.stdin.close()
        if process.wait() != 0:
            raise RuntimeError('ffmpeg failed')
    except BaseException:
        process.stdin.close()
        process.terminate()
        process.wait()
        raise
    result.update(duration_seconds=cursor/FPS, chapters=chapters, video_sha256=sha(output),
                  material_sha256={str(p.relative_to(root)): sha(p) for p in sorted(assets)},
                  storyboard_sha256=sha(root/'story.json'),
                  replay_verification_sha256=sha(root/'replay-verification.json'),
                  build_script_sha256=sha(Path(__file__)))
    output.with_suffix('.json').write_text(json.dumps(result, ensure_ascii=False, indent=2)+'\n')
    print(json.dumps({'video': str(output), 'seconds': cursor/FPS, 'sources': result['selected_sources']}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--capture', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    build(args.capture, read(args.capture/'story.json'), args.output)
