"""Compress superseded campaigns (campaign folder + its game folders) into runs/_archive/<name>.zip.
Paths inside each zip are relative to runs/, so `unzip X.zip -d runs` restores them exactly.
Originals are deleted only after the zip re-reads every file with the right size and CRC.
usage: python archive_runs.py <runs_dir> [--apply]
"""
import json, os, re, shutil, stat, sys, time, zipfile


def _force(func, path, exc):
    os.chmod(path, stat.S_IWRITE)   # OneDrive marks folders read-only; Windows then refuses rmdir
    func(path)
from pathlib import Path

runs = Path(sys.argv[1]); apply = '--apply' in sys.argv
ARCHIVE = ['_fake-v3', '_fake-v4', '_fake-v5', '_fake-v6', 'arm-20c', 'v2-20c', 'v3-check-20c',
           'v4-check-20c', 'v4-20c', 'v5-check-20c', 'v6-check-20c', 'v7-check-30c', 'test']
KEEP_GAMES = {'ce0c9dd1-1826-451b-a81c-1cc75ca23ed5'}   # v8 seed 5 aborted attempt, kept with v8
UUID = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-')

owner = {}
for c in os.listdir(runs):
    b = runs / c / 'batch.json'
    if not UUID.match(c) and b.exists():
        for a in json.loads(b.read_text(encoding='utf-8'))['attempts']:
            gid = a.get('game_id')
            if not gid:
                try: gid = json.loads(Path(a['directory'], 'stdout.json').read_text(encoding='utf-8')).get('game_id')
                except Exception: gid = None
            if gid: owner[gid] = c

# the running v8 game: newest folder not owned by anyone, modified within 2 h -> keep
now = time.time()
orphans = [d for d in os.listdir(runs) if UUID.match(d) and d not in owner and d not in KEEP_GAMES]
live = {d for d in orphans if now - max((runs / d / f).stat().st_mtime for f in os.listdir(runs / d)) < 7200}
orphans = [d for d in orphans if d not in live]
print('live (kept):', live)

groups = {c: [c] + sorted(g for g, o in owner.items() if o == c and (runs / g).exists()) for c in ARCHIVE if (runs / c).exists()}
groups['orphan-games'] = sorted(orphans)
(runs / '_archive').mkdir(exist_ok=True)
manifest = []
for name, items in groups.items():
    files = [p for it in items for p in sorted((runs / it).rglob('*')) if p.is_file()]
    size = sum(p.stat().st_size for p in files)
    print(f'{name}: {len(items)} folders, {len(files)} files, {size/2**20:.0f} MB')
    if not apply or not files:
        continue
    z = runs / '_archive' / f'{name}.zip'
    if not z.exists():   # an earlier run may have zipped it and failed while deleting: never overwrite
        with zipfile.ZipFile(z, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as out:
            for p in files:
                out.write(p, p.relative_to(runs).as_posix())
    with zipfile.ZipFile(z) as check:
        infos = {i.filename: i for i in check.infolist()}
        assert check.testzip() is None, name
        for p in files:
            i = infos[p.relative_to(runs).as_posix()]
            assert i.file_size == p.stat().st_size, p
    for it in items:
        shutil.rmtree(runs / it, onexc=_force)
    manifest.append((name, items, len(infos), sum(i.file_size for i in infos.values()), z.stat().st_size))
    print(f'  -> {z.name} {z.stat().st_size/2**20:.0f} MB, originals removed', flush=True)

if apply:
    lines = ['# runs/_archive', '', '압축한 실행 기록. 복원: 해당 zip을 `runs/`에 그대로 풀면 원래 경로로 돌아옴.', '',
             '| 묶음 | 폴더 수 | 파일 수 | 원본 MB | 압축 MB | 포함 폴더 |', '|---|---|---|---|---|---|']
    for name, items, n, size, zs in manifest:
        lines.append(f'| {name} | {len(items)} | {n} | {size/2**20:.0f} | {zs/2**20:.0f} | {", ".join(i[:8] if UUID.match(i) else i for i in items)} |')
    m = runs / '_archive' / 'MANIFEST.md'
    old = m.read_text(encoding='utf-8') + '\n' if m.exists() else ''
    m.write_text(old + '\n'.join(lines) + '\n', encoding='utf-8')
    print('done')
