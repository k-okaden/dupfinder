#!/usr/bin/env python3
"""
DupFinder — 重複ファイル検出ツール（写真・動画・RAW+JPG 対応）

  python3 dupfinder.py [フォルダ ...]

ブラウザで操作画面が開きます。削除はすべて「ゴミ箱へ移動」なので元に戻せます。
外部ライブラリ不要（Python 3.8+ 標準ライブラリのみ / macOS・Windows 対応）。
"""
__version__ = '0.1.0'

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

IS_MAC = sys.platform == 'darwin'
IS_WIN = sys.platform == 'win32'
FROZEN = getattr(sys, 'frozen', False)   # PyInstaller でアプリ化されているか
NO_WINDOW = 0x08000000 if IS_WIN else 0  # CREATE_NO_WINDOW

RAW_EXTS = {'.arw', '.cr2', '.cr3', '.crw', '.nef', '.nrw', '.raf', '.orf', '.rw2', '.pef',
            '.srw', '.dng', '.x3f', '.3fr', '.iiq', '.erf', '.kdc', '.mrw', '.rwl', '.srf', '.sr2'}
JPG_EXTS = {'.jpg', '.jpeg', '.heic', '.heif', '.hif'}
VIDEO_EXTS = {'.mp4', '.mov', '.m4v', '.avi', '.mts', '.m2ts', '.mkv', '.wmv', '.mpg', '.mpeg',
              '.3gp', '.webm', '.flv', '.insv', '.lrv'}
IMAGE_EXTS = JPG_EXTS | {'.png', '.gif', '.bmp', '.tif', '.tiff', '.webp', '.psd'}

# 中身を触ると壊れる「パッケージ」フォルダは走査しない
SKIP_DIR_SUFFIXES = ('.photoslibrary', '.app', '.fcpbundle', '.lrdata', '.imovielibrary',
                     '.tvlibrary', '.musiclibrary', '.aplibrary', '.photolibrary', '.bundle',
                     '.framework', '.cosessiondb', '.cocatalog', '.lrcat-data')
SKIP_DIR_NAMES = {'.Trashes', '.Trash', '.Spotlight-V100', '.fseventsd', '.DocumentRevisions-V100',
                  '.TemporaryItems', 'node_modules', '.git', '$RECYCLE.BIN',
                  'System Volume Information', '$Recycle.Bin'}
BROWSER_IMAGE_EXTS = {'.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp'}

# 「(1)」「 copy」「のコピー」「 - コピー」「 2」などの付加部分
SUFFIX_PATTERNS = [
    re.compile(r'\s*\(\d{1,3}\)$'),                                  # IMG (1) / IMG(2)
    re.compile(r'\s*-\s*(?:copy|コピー)(?:\s*\(\d{1,3}\))?$', re.I),   # Windows: IMG - コピー (2)
    re.compile(r'\s*(?:copy|のコピー|コピー)(?:\s*\d{1,3})?$', re.I),   # mac: IMG copy 2 / IMGのコピー
    re.compile(r'\s\d{1,2}$'),                                       # Finder 複製: IMG 2
]

PARTIAL = 64 * 1024
SMALL = 256 * 1024
CHUNK = 4 * 1024 * 1024
QUICK_MIN = 64 * 1024 * 1024
QUICK_SAMPLES = 32
QUICK_SAMPLE_SIZE = 256 * 1024
RAW_CROSS_TOLERANCE = 10  # 秒

# ---------------------------------------------------------------- state
LOCK = threading.Lock()
CANCEL = threading.Event()
STATE = {'status': 'idle', 'phase': '', 'done': 0, 'total': 0, 'message': ''}
FILES = []
RESULT = None
HASH_CACHE = {}
THUMB_DIR = tempfile.mkdtemp(prefix='dupfinder-thumbs-')
THUMB_SEM = threading.Semaphore(4)
LAST_PING = [time.time()]
IDLE_EXIT = 180  # アプリ版：ブラウザを閉じて 3 分たったら自動終了


class Cancelled(Exception):
    pass


def set_state(**kw):
    with LOCK:
        STATE.update(kw)


def check_cancel():
    if CANCEL.is_set():
        raise Cancelled()


def kind_of(ext):
    if ext in RAW_EXTS:
        return 'raw'
    if ext in VIDEO_EXTS:
        return 'video'
    if ext in IMAGE_EXTS:
        return 'image'
    return 'other'


def strip_suffix(stem):
    """'IMG_0001 (1) のコピー' -> ('IMG_0001', 2)"""
    score = 0
    while True:
        for pat in SUFFIX_PATTERNS:
            new = pat.sub('', stem)
            if new != stem and new.strip():
                stem, score = new, score + 1
                break
        else:
            return stem, score


# ---------------------------------------------------------------- hashing
def partial_hash(f):
    """先頭+末尾 64KB。256KB 以下はファイル全体（＝完全一致判定）"""
    h = hashlib.blake2b(digest_size=16)
    with open(f['path'], 'rb') as fp:
        if f['size'] <= SMALL:
            h.update(fp.read())
            return h.hexdigest(), True
        h.update(fp.read(PARTIAL))
        fp.seek(f['size'] - PARTIAL)
        h.update(fp.read(PARTIAL))
    return h.hexdigest(), False


def full_hash(i, quick, progress):
    key = (i, quick)
    if key in HASH_CACHE:
        return HASH_CACHE[key]
    f = FILES[i]
    h = hashlib.blake2b(digest_size=20)
    size = f['size']
    with open(f['path'], 'rb') as fp:
        if quick and size >= QUICK_MIN:
            step = (size - QUICK_SAMPLE_SIZE) // (QUICK_SAMPLES - 1)
            for k in range(QUICK_SAMPLES):
                check_cancel()
                fp.seek(k * step)
                h.update(fp.read(QUICK_SAMPLE_SIZE))
            progress(size)
        else:
            while True:
                check_cancel()
                b = fp.read(CHUNK)
                if not b:
                    break
                h.update(b)
                progress(len(b))
    HASH_CACHE[key] = h.hexdigest()
    return HASH_CACHE[key]


# ---------------------------------------------------------------- scan
def walk(roots, media_only):
    global FILES
    FILES = []
    seen = set()
    n = 0
    for root in roots:
        for dirpath, dirnames, filenames in os.walk(root):
            check_cancel()
            dirnames[:] = sorted(d for d in dirnames
                                 if not d.startswith('.') and d not in SKIP_DIR_NAMES
                                 and not d.lower().endswith(SKIP_DIR_SUFFIXES))
            for fn in sorted(filenames):
                if fn.startswith('.') or fn in ('Icon\r', 'desktop.ini', 'Thumbs.db'):
                    continue
                p = os.path.join(dirpath, fn)
                try:
                    st = os.stat(p, follow_symlinks=False)
                except OSError:
                    continue
                if not stat.S_ISREG(st.st_mode):
                    continue
                ino = (st.st_dev, st.st_ino)
                if ino in seen:   # ハードリンク / フォルダの重複指定
                    continue
                seen.add(ino)
                stem, ext = os.path.splitext(fn)
                ext = ext.lower()
                kind = kind_of(ext)
                if media_only and kind == 'other':
                    continue
                FILES.append({'id': len(FILES), 'path': p, 'name': fn, 'dir': dirpath,
                              'stem': stem, 'ext': ext, 'size': st.st_size,
                              'mtime': st.st_mtime, 'kind': kind, 'deleted': False})
                n += 1
                if n % 500 == 0:
                    set_state(done=n, message=dirpath)
    set_state(done=n)


def find_name_groups():
    by = defaultdict(list)
    for f in FILES:
        base, score = strip_suffix(f['stem'])
        f['suffix'] = score
        by[(f['dir'], base.lower(), f['ext'])].append(f['id'])
    groups = []
    for ids in by.values():
        if len(ids) < 2:
            continue
        originals = [i for i in ids if FILES[i]['suffix'] == 0]
        if not originals or len(originals) == len(ids):
            continue
        base = min(originals, key=lambda i: FILES[i]['mtime'])
        others = sorted((i for i in ids if i != base),
                        key=lambda i: (FILES[i]['suffix'], FILES[i]['name']))
        groups.append({'base': base, 'ids': [base] + others})
    return groups


def find_raw_pairs(cross):
    stems = defaultdict(lambda: ([], []))
    for f in FILES:
        if f['ext'] in RAW_EXTS:
            stems[f['stem'].lower()][0].append(f['id'])
        elif f['ext'] in JPG_EXTS:
            stems[f['stem'].lower()][1].append(f['id'])
    pairs = []
    for raws, jpgs in stems.values():
        if not raws or not jpgs:
            continue
        by_dir = defaultdict(lambda: ([], []))
        for r in raws:
            by_dir[FILES[r]['dir']][0].append(r)
        for j in jpgs:
            by_dir[FILES[j]['dir']][1].append(j)
        left_r, left_j = [], []
        for rs, js in by_dir.values():
            if rs and js:
                pairs.append({'raw': rs, 'jpg': js, 'cross': False})
            else:
                left_r += rs
                left_j += js
        if cross:
            used = set()
            for r in left_r:
                m = [j for j in left_j if j not in used
                     and abs(FILES[j]['mtime'] - FILES[r]['mtime']) <= RAW_CROSS_TOLERANCE]
                if m:
                    used.update(m)
                    pairs.append({'raw': [r], 'jpg': m, 'cross': True})
    pairs.sort(key=lambda p: FILES[p['raw'][0]]['path'])
    return pairs


def keep_key(i):
    f = FILES[i]
    return (f['suffix'] > 0, f['mtime'], len(f['path']), f['path'])


def run_scan(roots, opts):
    global RESULT
    quick = opts.get('quick', False)
    try:
        HASH_CACHE.clear()
        set_state(status='scanning', phase='ファイル一覧を作成中', done=0, total=0, message='')
        walk(roots, opts.get('media_only', False))

        name_groups = find_name_groups() if opts.get('names', True) else []
        raw_pairs = find_raw_pairs(opts.get('cross_raw', False)) if opts.get('raw', True) else []
        if not opts.get('names', True):
            for f in FILES:
                f['suffix'] = strip_suffix(f['stem'])[1]

        # --- 1) サイズでふるい → 2) 部分ハッシュ → 3) 完全ハッシュ
        content_groups = []
        exact_ids = set()
        need_full = []
        if opts.get('content', True):
            by_size = defaultdict(list)
            for f in FILES:
                if f['size'] > 0:
                    by_size[f['size']].append(f['id'])
            cands = [i for ids in by_size.values() if len(ids) > 1 for i in ids]
            set_state(phase='部分比較中', done=0, total=len(cands))
            by_ph = defaultdict(list)
            for n, i in enumerate(cands):
                check_cancel()
                try:
                    ph, exact = partial_hash(FILES[i])
                except OSError:
                    continue
                if exact:
                    exact_ids.add(i)
                    HASH_CACHE[(i, quick)] = ph
                by_ph[(FILES[i]['size'], ph)].append(i)
                if n % 50 == 0:
                    set_state(done=n, message=FILES[i]['path'])
            groups2 = [ids for ids in by_ph.values() if len(ids) > 1]
            need_full = [i for ids in groups2 for i in ids if i not in exact_ids]
        else:
            groups2 = []

        # 名前重複グループのうち、元ファイルと同サイズのものも完全比較する
        for g in name_groups:
            bs = FILES[g['base']]['size']
            for i in g['ids'][1:]:
                if FILES[i]['size'] == bs:
                    need_full += [g['base'], i]
        need_full = list(dict.fromkeys(need_full))
        bytes_total = sum(FILES[i]['size'] if not (quick and FILES[i]['size'] >= QUICK_MIN)
                          else FILES[i]['size'] for i in need_full)
        progress = {'b': 0, 't': 0}

        def prog(nb):
            progress['b'] += nb
            now = time.time()
            if now - progress['t'] > 0.2:
                progress['t'] = now
                set_state(done=progress['b'])

        set_state(phase='完全比較中（ハッシュ計算）', done=0, total=max(bytes_total, 1))

        def fh(i):
            if i in exact_ids:
                return HASH_CACHE[(i, quick)]
            set_state(message=FILES[i]['path'])
            return full_hash(i, quick, prog)

        for ids in groups2:
            by_fh = defaultdict(list)
            for i in ids:
                try:
                    by_fh[fh(i)].append(i)
                except OSError:
                    pass
            for same in by_fh.values():
                if len(same) > 1:
                    same.sort(key=keep_key)
                    content_groups.append({'ids': same, 'keep': same[0],
                                           'size': FILES[same[0]]['size']})
        content_groups.sort(key=lambda g: -g['size'] * (len(g['ids']) - 1))

        for g in name_groups:
            b = FILES[g['base']]
            g['identical'] = {}
            for i in g['ids'][1:]:
                same = False
                if FILES[i]['size'] == b['size']:
                    try:
                        same = fh(i) == fh(g['base'])
                    except OSError:
                        pass
                g['identical'][i] = same

        RESULT = {
            'roots': roots, 'quick': quick,
            'files': FILES,
            'content_groups': content_groups,
            'name_groups': name_groups,
            'raw_pairs': raw_pairs,
            'total_files': len(FILES),
        }
        set_state(status='done', phase='完了', message='')
    except Cancelled:
        set_state(status='cancelled', phase='中止しました', message='')
    except Exception as e:  # noqa
        set_state(status='error', phase='エラー', message=repr(e))


# ---------------------------------------------------------------- platform
def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, creationflags=NO_WINDOW, **kw)


def _win_trash(batch):
    """Windows: SHFileOperation + FOF_ALLOWUNDO でごみ箱へ"""
    import ctypes
    from ctypes import wintypes

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [('hwnd', wintypes.HWND), ('wFunc', wintypes.UINT),
                    ('pFrom', wintypes.LPCWSTR), ('pTo', wintypes.LPCWSTR),
                    ('fFlags', ctypes.c_uint16), ('fAnyOperationsAborted', wintypes.BOOL),
                    ('hNameMappings', ctypes.c_void_p), ('lpszProgressTitle', wintypes.LPCWSTR)]
    FO_DELETE, FOF_SILENT, FOF_NOCONFIRMATION, FOF_ALLOWUNDO, FOF_NOERRORUI = 3, 0x4, 0x10, 0x40, 0x400
    FOF_WANTNUKEWARNING = 0x4000  # ごみ箱が無いドライブ（USB/NAS 等）では完全削除前に警告を出す
    op = SHFILEOPSTRUCTW(wFunc=FO_DELETE, pFrom='\0'.join(batch) + '\0',
                         fFlags=FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI
                         | FOF_WANTNUKEWARNING)
    ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))


def move_to_trash(paths):
    """ゴミ箱へ移動（Finder の「戻す」/ ごみ箱の「元に戻す」で復元可能）"""
    ok, failed = [], []
    for k in range(0, len(paths), 100):
        batch = paths[k:k + 100]
        if IS_WIN:
            _win_trash(batch)
        elif IS_MAC and os.path.exists('/usr/bin/trash'):
            run(['/usr/bin/trash', *batch])
        elif IS_MAC:
            items = ', '.join('POSIX file ' + json.dumps(p) for p in batch)
            run(['osascript', '-e', f'tell application "Finder" to delete {{{items}}}'])
        elif shutil.which('gio'):
            run(['gio', 'trash', *batch])
        for p in batch:
            (failed if os.path.lexists(p) else ok).append(p)
    return ok, failed


def choose_folder():
    if IS_MAC:
        r = run(['osascript', '-e', 'POSIX path of (choose folder with prompt "検索するフォルダを選択")'],
                text=True)
        return r.stdout.strip().rstrip('/') or None
    if IS_WIN:
        ps = ('[Console]::OutputEncoding=[Text.Encoding]::UTF8;'
              'Add-Type -AssemblyName System.Windows.Forms;'
              '$o=New-Object System.Windows.Forms.Form -Property @{TopMost=$true};'
              '$f=New-Object System.Windows.Forms.FolderBrowserDialog;'
              '$f.Description="検索するフォルダを選択";'
              'if($f.ShowDialog($o) -eq "OK"){$f.SelectedPath}')
        r = run(['powershell', '-NoProfile', '-STA', '-Command', ps], text=True, encoding='utf-8')
        return r.stdout.strip() or None
    return None


def reveal(path):
    if IS_MAC:
        run(['open', '-R', path])
    elif IS_WIN:
        subprocess.Popen(f'explorer /select,"{path}"')
    else:
        run(['xdg-open', os.path.dirname(path)])


def open_file(path):
    if IS_MAC:
        run(['open', path])
    elif IS_WIN:
        os.startfile(path)  # noqa
    else:
        run(['xdg-open', path])


def make_thumb(i):
    """サムネイル画像のパスと MIME を返す"""
    f = FILES[i]
    out = os.path.join(THUMB_DIR, f'{i}.png')
    if os.path.exists(out):
        return out, 'image/png'
    if f['kind'] == 'other':
        return None
    if not IS_MAC:
        # Windows 等：ブラウザが表示できる画像はそのまま返す（RAW・動画はアイコン表示）
        if f['ext'] in BROWSER_IMAGE_EXTS and f['size'] <= 30 * 1024 * 1024:
            return f['path'], 'image/jpeg' if f['ext'] in ('.jpg', '.jpeg') else 'image/' + f['ext'][1:]
        return None
    with THUMB_SEM:
        if os.path.exists(out):
            return out, 'image/png'
        tmp = tempfile.mkdtemp(dir=THUMB_DIR)
        try:
            run(['qlmanage', '-t', '-s', '256', '-o', tmp, f['path']], timeout=30)
            produced = os.path.join(tmp, f['name'] + '.png')
            if os.path.exists(produced):
                os.replace(produced, out)
                return out, 'image/png'
        except Exception:
            pass
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    return None


# ---------------------------------------------------------------- http
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, body, ctype='application/json; charset=utf-8'):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store' if 'json' in ctype else 'max-age=3600')
        self.end_headers()
        self.wfile.write(body)

    def body(self):
        n = int(self.headers.get('Content-Length') or 0)
        return json.loads(self.rfile.read(n) or b'{}')

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == '/':
            return self.send(200, HTML, 'text/html; charset=utf-8')
        if u.path == '/api/ping':
            LAST_PING[0] = time.time()
            return self.send(200, {'ok': True})
        if u.path == '/api/status':
            LAST_PING[0] = time.time()
            with LOCK:
                return self.send(200, dict(STATE))
        if u.path == '/api/result':
            return self.send(200, RESULT or {})
        if u.path == '/thumb':
            try:
                i = int(parse_qs(u.query)['id'][0])
                assert 0 <= i < len(FILES)
            except Exception:
                return self.send(404, b'', 'text/plain')
            t = make_thumb(i)
            if not t:
                return self.send(404, b'', 'text/plain')
            with open(t[0], 'rb') as fp:
                return self.send(200, fp.read(), t[1])
        self.send(404, {'error': 'not found'})

    def do_POST(self):
        # 他サイトからの操作を拒否
        origin = self.headers.get('Origin')
        if origin and urlparse(origin).hostname not in ('127.0.0.1', 'localhost'):
            return self.send(403, {'error': 'forbidden'})
        u = urlparse(self.path)
        data = self.body()
        if u.path == '/api/scan':
            if STATE['status'] == 'scanning':
                return self.send(409, {'error': 'スキャン中です'})
            roots = [os.path.abspath(os.path.expanduser(r.strip())) for r in data.get('roots', [])
                     if r.strip()]
            bad = [r for r in roots if not os.path.isdir(r)]
            if not roots or bad:
                return self.send(400, {'error': 'フォルダが見つかりません: ' + ', '.join(bad)})
            CANCEL.clear()
            threading.Thread(target=run_scan, args=(roots, data), daemon=True).start()
            return self.send(200, {'ok': True})
        if u.path == '/api/cancel':
            CANCEL.set()
            return self.send(200, {'ok': True})
        if u.path == '/api/choose':
            return self.send(200, {'path': choose_folder()})
        if u.path == '/api/reveal':
            reveal(FILES[int(data['id'])]['path'])
            return self.send(200, {'ok': True})
        if u.path == '/api/open':
            open_file(FILES[int(data['id'])]['path'])
            return self.send(200, {'ok': True})
        if u.path == '/api/quit':
            self.send(200, {'ok': True})
            CANCEL.set()
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return
        if u.path == '/api/trash':
            ids = {int(i) for i in data.get('ids', []) if 0 <= int(i) < len(FILES)}
            # 安全装置：完全一致グループの全ファイルを消すことはできない
            if RESULT:
                for g in RESULT['content_groups']:
                    alive = [i for i in g['ids'] if not FILES[i]['deleted']]
                    if alive and all(i in ids for i in alive):
                        return self.send(400, {'error': '同一内容のファイルがすべて選択されているグループがあります（1つは残してください）: '
                                                         + FILES[alive[0]]['name']})
            ids = [i for i in ids if not FILES[i]['deleted']]
            ok, failed = move_to_trash([FILES[i]['path'] for i in ids])
            okset = set(ok)
            done = []
            for i in ids:
                if FILES[i]['path'] in okset:
                    FILES[i]['deleted'] = True
                    done.append(i)
            return self.send(200, {'trashed': done, 'failed': failed})
        self.send(404, {'error': 'not found'})


HTML = r'''<!doctype html>
<html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>DupFinder</title>
<style>
:root{--bg:#f5f5f7;--card:#fff;--fg:#1d1d1f;--sub:#6e6e73;--line:#e3e3e8;--accent:#0a66d8;--accent-bg:#e8f1fd;
--danger:#d70015;--danger-bg:#fde8ea;--ok:#1a7f37;--ok-bg:#e6f4ea;--warn:#9a6700;--warn-bg:#fff4d6;--sel:#fff3f3}
@media (prefers-color-scheme:dark){:root{--bg:#161618;--card:#222225;--fg:#f2f2f4;--sub:#a1a1a8;--line:#36363b;
--accent:#4c9aff;--accent-bg:#1b2a40;--danger:#ff5c5c;--danger-bg:#3d1d20;--ok:#56d364;--ok-bg:#16301d;--warn:#e3b341;--warn-bg:#3a2f12;--sel:#36221f}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 -apple-system,BlinkMacSystemFont,"Hiragino Sans",sans-serif;padding-bottom:80px}
header{padding:20px 24px 8px;max-width:1200px;margin:auto}
h1{font-size:20px;margin:0 0 12px}
.panel{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px;margin-bottom:14px}
textarea{width:100%;font:13px ui-monospace,Menlo,monospace;padding:8px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--fg);resize:vertical}
button{font:inherit;border:1px solid var(--line);background:var(--card);color:var(--fg);border-radius:8px;padding:6px 12px;cursor:pointer}
button:hover{border-color:var(--accent)}
button.primary{background:var(--accent);color:#fff;border-color:var(--accent);font-weight:600}
button.danger{background:var(--danger);color:#fff;border-color:var(--danger);font-weight:600}
button:disabled{opacity:.45;cursor:default}
.row-flex{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.opts{display:flex;flex-wrap:wrap;gap:6px 18px;margin:10px 0}
.opts label{display:flex;gap:6px;align-items:center;cursor:pointer}
.hint{color:var(--sub);font-size:12px}
main{max-width:1200px;margin:auto;padding:0 24px}
.progress{height:8px;background:var(--line);border-radius:4px;overflow:hidden;margin:8px 0}
.progress>div{height:100%;background:var(--accent);width:0;transition:width .2s}
.msg{font:12px ui-monospace,Menlo,monospace;color:var(--sub);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.tabs{display:flex;gap:4px;border-bottom:1px solid var(--line);margin:8px 0 12px;flex-wrap:wrap}
.tab{border:none;border-bottom:2px solid transparent;border-radius:0;background:none;padding:8px 14px;color:var(--sub)}
.tab.on{color:var(--fg);border-bottom-color:var(--accent);font-weight:600}
.tab .n{background:var(--line);border-radius:10px;padding:0 7px;font-size:12px;margin-left:4px}
.group{background:var(--card);border:1px solid var(--line);border-radius:10px;margin-bottom:10px;overflow:hidden}
.group.bad{border-color:var(--danger)}
.ghead{display:flex;justify-content:space-between;gap:8px;padding:8px 12px;background:var(--bg);border-bottom:1px solid var(--line);font-size:12px;color:var(--sub);flex-wrap:wrap}
.ghead b{color:var(--fg)}
.frow{display:grid;grid-template-columns:24px 64px 1fr auto;gap:10px;align-items:center;padding:8px 12px;border-top:1px solid var(--line)}
.frow:first-of-type{border-top:none}
.frow.on{background:var(--sel)}
.frow input{width:18px;height:18px}
.thumb{width:64px;height:48px;border-radius:6px;background:var(--bg);display:flex;align-items:center;justify-content:center;font-size:11px;color:var(--sub);position:relative;overflow:hidden;text-transform:uppercase}
.thumb img{position:absolute;inset:0;width:100%;height:100%;object-fit:cover}
.fname{font-weight:500;word-break:break-all}
.fpath{font-size:12px;color:var(--sub);word-break:break-all}
.meta{font-size:12px;color:var(--sub);text-align:right;white-space:nowrap}
.badge{display:inline-block;font-size:11px;border-radius:5px;padding:0 6px;margin-left:6px;font-weight:600;vertical-align:1px}
.b-keep{background:var(--ok-bg);color:var(--ok)}.b-same{background:var(--danger-bg);color:var(--danger)}
.b-diff{background:var(--warn-bg);color:var(--warn)}.b-raw{background:var(--accent-bg);color:var(--accent)}
.b-del{background:var(--danger-bg);color:var(--danger)}
.links button{font-size:12px;padding:2px 8px;margin-left:4px}
.bulk{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px;align-items:center}
.empty{color:var(--sub);text-align:center;padding:40px}
footer{position:fixed;left:0;right:0;bottom:0;background:var(--card);border-top:1px solid var(--line);padding:12px 24px;display:flex;justify-content:center}
footer .in{max-width:1200px;width:100%;display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap}
.warnline{color:var(--danger);font-size:12px}
.summary{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:10px;margin-bottom:12px}
.summary div{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 14px}
.summary b{display:block;font-size:20px}
.summary span{font-size:12px;color:var(--sub)}
@media (max-width:640px){header,main{padding-left:16px;padding-right:16px}.frow{grid-template-columns:24px 48px 1fr}.meta{grid-column:3;text-align:left}.thumb{width:48px;height:40px}}
</style></head><body>
<header>
<div class="row-flex" style="justify-content:space-between;margin-bottom:12px">
<h1 style="margin:0">🔍 DupFinder <span class="hint">v__VERSION__ · 重複ファイル検出（写真・動画・RAW+JPG）</span></h1>
<button id="quit" title="アプリを終了します">終了</button></div>
<div class="panel">
  <div class="row-flex" style="justify-content:space-between;margin-bottom:6px">
    <b>検索するフォルダ</b><span class="hint">1行に1フォルダ。複数フォルダをまたいで比較できます</span>
  </div>
  <textarea id="roots" rows="2" placeholder="/Users/you/Pictures"></textarea>
  <div class="opts">
    <label><input type="checkbox" id="o-content" checked>内容が同じファイル（完全一致）</label>
    <label><input type="checkbox" id="o-names" checked>「(1)」「コピー」付きの名前重複</label>
    <label><input type="checkbox" id="o-raw" checked>RAW + JPG 同時記録ペア</label>
    <label><input type="checkbox" id="o-cross">RAW と JPG が別フォルダでもペアにする</label>
    <label><input type="checkbox" id="o-media">写真・動画のみ対象</label>
    <label><input type="checkbox" id="o-quick">動画の高速比較（64MB以上はサンプリング）</label>
  </div>
  <div class="row-flex">
    <button id="choose">フォルダを選択…</button>
    <button class="primary" id="scan">スキャン開始</button>
    <button id="cancel" style="display:none">中止</button>
    <span class="hint" id="note"></span>
  </div>
  <div id="prog" style="display:none">
    <div class="progress"><div id="bar"></div></div>
    <div class="row-flex" style="justify-content:space-between"><span id="phase"></span><span id="pnum" class="hint"></span></div>
    <div class="msg" id="pmsg"></div>
  </div>
</div>
</header>
<main id="main"></main>
<footer id="foot" style="display:none"><div class="in">
  <div><b id="selcount">0件</b> 選択中 <span class="hint" id="selsize"></span> <div class="warnline" id="warn"></div></div>
  <div class="row-flex"><button id="clear">選択解除</button><button class="danger" id="trash">ゴミ箱へ移動</button></div>
</div></footer>
<script>
const $=s=>document.querySelector(s);
let R=null,sel=new Set(),tab='content',limit={content:150,name:150,raw:150},timer=null;
const esc=s=>String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function fmt(b){const u=['B','KB','MB','GB','TB'];let i=0;while(b>=1024&&i<4){b/=1024;i++}return (i?b.toFixed(b<10?2:1):b)+' '+u[i]}
const fdate=t=>{const d=new Date(t*1000);return d.toLocaleString('ja-JP',{year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'})};
async function api(p,b){const r=await fetch(p,b===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b)});return r.json()}
try{const s=localStorage.getItem('df-roots');if(s)$('#roots').value=s}catch(e){}
if(INIT_ROOTS.length)$('#roots').value=INIT_ROOTS.join('\n');

$('#choose').onclick=async()=>{const r=await api('/api/choose',{});if(r.path){const t=$('#roots');t.value=(t.value.trim()?t.value.trim()+'\n':'')+r.path}};
$('#scan').onclick=async()=>{
  const roots=$('#roots').value.split('\n').map(s=>s.trim()).filter(Boolean);
  if(!roots.length){alert('フォルダを指定してください');return}
  try{localStorage.setItem('df-roots',roots.join('\n'))}catch(e){}
  const r=await api('/api/scan',{roots,content:$('#o-content').checked,names:$('#o-names').checked,raw:$('#o-raw').checked,
    cross_raw:$('#o-cross').checked,media_only:$('#o-media').checked,quick:$('#o-quick').checked});
  if(r.error){alert(r.error);return}
  R=null;sel.clear();$('#main').innerHTML='';$('#foot').style.display='none';
  $('#prog').style.display='';$('#scan').disabled=true;$('#cancel').style.display='';poll();
};
$('#cancel').onclick=()=>api('/api/cancel',{});
async function poll(){
  const s=await api('/api/status');
  $('#phase').textContent=s.phase;$('#pmsg').textContent=s.message||'';
  const hashing=s.phase.startsWith('完全');
  $('#pnum').textContent=s.total?(hashing?fmt(s.done)+' / '+fmt(s.total):s.done+' / '+s.total):(s.done?s.done+' 件':'');
  $('#bar').style.width=s.total?Math.min(100,s.done/s.total*100)+'%':'0';
  if(s.status==='scanning'){timer=setTimeout(poll,300);return}
  $('#scan').disabled=false;$('#cancel').style.display='none';
  if(s.status==='done'){$('#prog').style.display='none';R=await api('/api/result');defaults();render()}
  else if(s.status==='error'){$('#phase').textContent='エラー: '+s.message}
}
function defaults(){
  sel.clear();
  R.content_groups.forEach(g=>g.ids.forEach(i=>{if(i!==g.keep)sel.add(i)}));
  R.name_groups.forEach(g=>g.ids.slice(1).forEach(i=>{if(g.identical[i])sel.add(i)}));
  tab=R.content_groups.length?'content':R.name_groups.length?'name':R.raw_pairs.length?'raw':'content';
}
const alive=i=>!R.files[i].deleted;
function thumb(f){return `<div class="thumb">${esc(f.ext.slice(1))}${f.kind!=='other'?`<img loading="lazy" src="/thumb?id=${f.id}" onerror="this.remove()">`:''}</div>`}
function frow(i,badges){
  const f=R.files[i];if(f.deleted)return'';const on=sel.has(i);
  return `<div class="frow ${on?'on':''}" data-row="${i}"><input type="checkbox" data-id="${i}" ${on?'checked':''}>${thumb(f)}
  <div><div class="fname">${esc(f.name)}${badges||''}${on?'<span class="badge b-del">削除</span>':''}</div><div class="fpath">${esc(f.dir)}</div></div>
  <div class="meta">${fmt(f.size)}<br>${fdate(f.mtime)}<div class="links"><button data-open="${i}">開く</button><button data-reveal="${i}">Finder</button></div></div></div>`;
}
function render(){
  if(!R)return;
  const cg=R.content_groups.filter(g=>g.ids.filter(alive).length>1);
  const ng=R.name_groups.filter(g=>g.ids.filter(alive).length>1);
  const rp=R.raw_pairs.filter(p=>p.raw.some(alive)&&p.jpg.some(alive));
  const waste=cg.reduce((a,g)=>a+g.size*(g.ids.filter(alive).length-1),0);
  let h=`<div class="summary"><div><b>${R.total_files.toLocaleString()}</b><span>スキャンしたファイル</span></div>
  <div><b>${cg.length}</b><span>完全一致グループ</span></div><div><b>${fmt(waste)}</b><span>重複で使われている容量</span></div>
  <div><b>${ng.length}</b><span>名前重複「(1)」など</span></div><div><b>${rp.length}</b><span>RAW+JPG ペア</span></div></div>
  <div class="tabs">
   <button class="tab ${tab==='content'?'on':''}" data-tab="content">完全一致<span class="n">${cg.length}</span></button>
   <button class="tab ${tab==='name'?'on':''}" data-tab="name">名前の重複 (1)<span class="n">${ng.length}</span></button>
   <button class="tab ${tab==='raw'?'on':''}" data-tab="raw">RAW + JPG<span class="n">${rp.length}</span></button></div>`;
  if(tab==='content'){
    h+=`<div class="bulk"><button data-bulk="c-rec">おすすめで選択（1つ残す）</button><button data-bulk="c-none">このタブの選択を解除</button>
    <span class="hint">内容がバイト単位で同一のファイル。${R.quick?'<b>高速比較モード</b>（大きな動画はサンプリング比較）。':''}「(1)」付き・新しいものを削除候補にします。</span></div>`;
    if(!cg.length)h+='<div class="empty">内容が同じファイルは見つかりませんでした</div>';
    cg.slice(0,limit.content).forEach(g=>{const ids=g.ids.filter(alive);const all=ids.every(i=>sel.has(i));
      h+=`<div class="group ${all?'bad':''}"><div class="ghead"><span><b>${ids.length}個</b> が同一内容 · ${fmt(g.size)}</span>${all?'<span class="warnline">⚠ すべて選択されています。1つは残してください</span>':''}</div>`
      +ids.map(i=>frow(i,(i===g.keep?'<span class="badge b-keep">残す推奨</span>':'')+(R.files[i].suffix>0?'<span class="badge b-diff">番号付き</span>':''))).join('')+'</div>'});
    if(cg.length>limit.content)h+=`<div class="empty"><button data-more="content">さらに表示（残り${cg.length-limit.content}）</button></div>`;
  }else if(tab==='name'){
    h+=`<div class="bulk"><button data-bulk="n-same">同一内容の番号付きを選択</button><button data-bulk="n-none">このタブの選択を解除</button>
    <span class="hint">同じフォルダ内の「IMG.jpg」と「IMG (1).jpg」「IMG のコピー.jpg」「IMG 2.jpg」など。<b>内容が異なる</b>ものは自動選択しません。</span></div>`;
    if(!ng.length)h+='<div class="empty">名前の重複は見つかりませんでした</div>';
    ng.slice(0,limit.name).forEach(g=>{const diff=g.ids.slice(1).filter(alive).some(i=>!g.identical[i]);
      h+=`<div class="group"><div class="ghead"><span><b>${esc(R.files[g.base].name)}</b> の名前違い</span>${diff?'<span>内容が異なるファイルあり — 確認してください</span>':''}</div>`
      +g.ids.map((i,k)=>frow(i,k===0?'<span class="badge b-keep">元ファイル</span>':g.identical[i]?'<span class="badge b-same">元と同一内容</span>':'<span class="badge b-diff">内容が異なる</span>')).join('')+'</div>'});
    if(ng.length>limit.name)h+=`<div class="empty"><button data-more="name">さらに表示（残り${ng.length-limit.name}）</button></div>`;
  }else{
    const rs=rp.reduce((a,p)=>a+p.raw.filter(alive).reduce((b,i)=>b+R.files[i].size,0),0);
    const js=rp.reduce((a,p)=>a+p.jpg.filter(alive).reduce((b,i)=>b+R.files[i].size,0),0);
    h+=`<div class="bulk"><button data-bulk="r-raw">すべてのRAWを選択（JPGを残す・${fmt(rs)}）</button><button data-bulk="r-jpg">すべてのJPG/HEIFを選択（RAWを残す・${fmt(js)}）</button>
    <button data-bulk="r-none">このタブの選択を解除</button><span class="hint">同じファイル名のRAW（ARW/CR2/CR3/NEF/RAF/DNG…）とJPG/HEIF。個別にチェックも可能です。</span></div>`;
    if(!rp.length)h+='<div class="empty">RAW + JPG のペアは見つかりませんでした</div>';
    rp.slice(0,limit.raw).forEach(p=>{const ids=[...p.raw,...p.jpg].filter(alive);const all=ids.every(i=>sel.has(i));
      h+=`<div class="group ${all?'bad':''}"><div class="ghead"><span><b>${esc(R.files[p.raw[0]].stem)}</b>${p.cross?' · 別フォルダ（撮影時刻で照合）':''}</span>${all?'<span class="warnline">⚠ RAWとJPGの両方が選択されています</span>':''}</div>`
      +ids.map(i=>frow(i,`<span class="badge ${R.files[i].kind==='raw'?'b-raw':'b-keep'}">${R.files[i].kind==='raw'?'RAW':'JPG'}</span>`)).join('')+'</div>'});
    if(rp.length>limit.raw)h+=`<div class="empty"><button data-more="raw">さらに表示（残り${rp.length-limit.raw}）</button></div>`;
  }
  $('#main').innerHTML=h;footer();
}
function footer(){
  const ids=[...sel].filter(alive);const size=ids.reduce((a,i)=>a+R.files[i].size,0);
  $('#foot').style.display='';$('#selcount').textContent=ids.length+'件';$('#selsize').textContent='('+fmt(size)+' 解放)';
  const bad=R.content_groups.some(g=>{const a=g.ids.filter(alive);return a.length>1&&a.every(i=>sel.has(i))});
  const rawBoth=R.raw_pairs.some(p=>{const a=[...p.raw,...p.jpg].filter(alive);return a.length>1&&a.every(i=>sel.has(i))});
  $('#warn').textContent=bad?'同一内容のファイルがすべて選択されているグループがあります':rawBoth?'RAWとJPGの両方が選択されているペアがあります（両方削除されます）':'';
  $('#trash').disabled=!ids.length||bad;
}
document.addEventListener('change',e=>{const id=e.target.dataset.id;if(id===undefined)return;const i=+id;
  e.target.checked?sel.add(i):sel.delete(i);render()});
document.addEventListener('click',async e=>{const t=e.target.closest('button');if(!t)return;const d=t.dataset;
  if(d.tab){tab=d.tab;render();window.scrollTo(0,0)}
  else if(d.more){limit[d.more]+=300;render()}
  else if(d.reveal)api('/api/reveal',{id:+d.reveal});
  else if(d.open)api('/api/open',{id:+d.open});
  else if(d.bulk){const b=d.bulk;
    if(b==='c-rec'||b==='c-none')R.content_groups.forEach(g=>g.ids.forEach(i=>{sel.delete(i);if(b==='c-rec'&&i!==g.keep)sel.add(i)}));
    if(b==='n-same'||b==='n-none')R.name_groups.forEach(g=>g.ids.slice(1).forEach(i=>{sel.delete(i);if(b==='n-same'&&g.identical[i])sel.add(i)}));
    if(b.startsWith('r-'))R.raw_pairs.forEach(p=>{[...p.raw,...p.jpg].forEach(i=>sel.delete(i));
      if(b==='r-raw')p.raw.forEach(i=>sel.add(i));if(b==='r-jpg')p.jpg.forEach(i=>sel.add(i))});
    render()}
});
$('#clear').onclick=()=>{sel.clear();render()};
setInterval(()=>fetch('/api/ping').catch(()=>{}),20000);
$('#quit').onclick=async()=>{if(!confirm('DupFinder を終了しますか？'))return;try{await api('/api/quit',{})}catch(e){}
  document.body.innerHTML='<div class="empty" style="padding:80px">DupFinder を終了しました。このタブは閉じてかまいません。</div>'};
$('#trash').onclick=async()=>{
  const ids=[...sel].filter(alive);const size=ids.reduce((a,i)=>a+R.files[i].size,0);
  if(!confirm(`${ids.length}件（${fmt(size)}）をゴミ箱へ移動します。\nFinderの「戻す」で復元できます。よろしいですか？`))return;
  $('#trash').disabled=true;$('#trash').textContent='移動中…';
  const r=await api('/api/trash',{ids});
  $('#trash').textContent='ゴミ箱へ移動';
  if(r.error){alert(r.error);footer();return}
  r.trashed.forEach(i=>{R.files[i].deleted=true;sel.delete(i)});
  if(r.failed.length)alert(r.failed.length+'件を移動できませんでした:\n'+r.failed.slice(0,10).join('\n'));
  render();
};
</script></body></html>'''


def main():
    ap = argparse.ArgumentParser(description='重複ファイル検出ツール')
    ap.add_argument('folders', nargs='*', help='最初に入力しておくフォルダ')
    ap.add_argument('--port', type=int, default=0)
    ap.add_argument('--no-browser', action='store_true')
    a = ap.parse_args()
    global HTML
    # macOS の .app 起動時に渡される -psn_XXX 引数などはフォルダとして扱わない
    folders = [os.path.abspath(f) for f in a.folders if os.path.isdir(f)]
    HTML = (HTML.replace('INIT_ROOTS', json.dumps(folders))
                .replace('__VERSION__', __version__))
    srv = ThreadingHTTPServer(('127.0.0.1', a.port), Handler)
    url = f'http://127.0.0.1:{srv.server_address[1]}/'
    print(f'DupFinder {__version__} 起動: {url}\n終了するには Ctrl+C か画面の「終了」ボタン')
    if not a.no_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    if FROZEN:
        # アプリ版はウィンドウが無いので、ブラウザを閉じたら自動で終了する
        def watchdog():
            while True:
                time.sleep(15)
                if time.time() - LAST_PING[0] > IDLE_EXIT and STATE['status'] != 'scanning':
                    srv.shutdown()
                    return
        threading.Thread(target=watchdog, daemon=True).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        shutil.rmtree(THUMB_DIR, ignore_errors=True)


if __name__ == '__main__':
    main()
