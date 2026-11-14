"""検出ロジックとゴミ箱移動の簡易テスト（mac / Windows の CI で実行）"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import dupfinder as d  # noqa: E402

tmp = tempfile.mkdtemp()


def w(rel, data):
    p = os.path.join(tmp, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, 'wb') as f:
        f.write(data)
    return p


a = os.urandom(300_000)
w('a/IMG_0001.jpg', a)
dup = w('a/IMG_0001 (1).jpg', a)
w('b/copy.jpg', a)
w('a/IMG_0002.jpg', os.urandom(1000))
w('a/IMG_0002(1).jpg', os.urandom(1000))
v = os.urandom(5_000_000)
w('a/movie.MTS', v)
w('a/movie のコピー.MTS', v)
b = bytearray(os.urandom(1_000_000))
w('a/x1.bin', bytes(b))
b[500_000] ^= 1
w('a/x2.bin', bytes(b))           # 同サイズ・先頭末尾同じ・中身だけ違う
w('a/DSC0001.ARW', os.urandom(5000))
w('a/DSC0001.JPG', os.urandom(3000))
w('a/Trip 1.txt', b'1')
w('a/Trip 2.txt', b'2')

d.run_scan([tmp], {'content': True, 'names': True, 'raw': True})
assert d.STATE['status'] == 'done', d.STATE
R = d.RESULT


def n(i):
    return d.FILES[i]['name']


groups = sorted(sorted(n(i) for i in g['ids']) for g in R['content_groups'])
assert groups == sorted([sorted(['IMG_0001.jpg', 'IMG_0001 (1).jpg', 'copy.jpg']),
                         sorted(['movie.MTS', 'movie のコピー.MTS'])]), groups
assert {n(g['keep']) for g in R['content_groups']} == {'IMG_0001.jpg', 'movie.MTS'}
names = {n(g['base']): {n(k): v for k, v in g['identical'].items()} for g in R['name_groups']}
assert names['IMG_0002.jpg'] == {'IMG_0002(1).jpg': False}, names
assert names['IMG_0001.jpg'] == {'IMG_0001 (1).jpg': True}, names
assert 'Trip 1.txt' not in names
assert [(n(p['raw'][0]), n(p['jpg'][0])) for p in R['raw_pairs']] == [('DSC0001.ARW', 'DSC0001.JPG')]

if d.IS_WIN or (d.IS_MAC and os.path.exists('/usr/bin/trash')):
    ok, failed = d.move_to_trash([dup])
    assert ok == [dup] and not os.path.exists(dup), (ok, failed)
    print('trash: OK')
print('smoke test: OK')
