#!/usr/bin/env python3
"""Trimbin tree scanner — recursive directory size analysis with type classification."""

import json
import os
import sys
import time

import trimbin_common as tc

DATA_DIR = tc.DATA_DIR
TREE_FILE = DATA_DIR / "tree_scan.json"

_scanned = [0]      # dirs visited this run (for the live progress counter)
_total_est = [0]    # dir-count estimate from the last scan (for the progress bar)

# Media-type classification sets are shared/canonical; junk + trickplay names
# stay local to the tree scanner.
VIDEO_EXTS = tc.VIDEO_EXTS
AUDIO_EXTS = tc.AUDIO_EXTS
SUB_EXTS = tc.SUB_EXTS
IMAGE_EXTS = tc.IMAGE_EXTS
META_EXTS = tc.META_EXTS
JUNK_EXTS = {'.sfv', '.srr', '.torrent', '.exe', '.bat', '.cmd', '.com', '.msi',
             '.scr', '.ds_store', '.bts_search', '.part', '.!qb'}
TRICKPLAY_NAMES = {'.trickplay', 'trickplay', '.bif'}

JUNK_NAMES = {'thumbs.db', '.ds_store', 'desktop.ini', '@eadir', '.@__thumb',
              'rarbg.txt', 'yify.txt', 'www.yts.mx.jpg'}


def classify_file(name):
    lower = name.lower()
    if lower in JUNK_NAMES:
        return 'junk'
    ext = os.path.splitext(lower)[1]
    if ext in VIDEO_EXTS:
        return 'video'
    if ext in AUDIO_EXTS:
        return 'audio'
    if ext in SUB_EXTS:
        return 'subtitle'
    if ext in IMAGE_EXTS:
        return 'image'
    if ext in META_EXTS:
        return 'metadata'
    if ext in JUNK_EXTS:
        return 'junk'
    return 'other'


def is_trickplay_dir(name):
    return name.lower() in TRICKPLAY_NAMES


def scan_tree(root_path, max_depth=5):
    """Recursively scan directory tree, returning nested structure."""
    def scan_dir(path, depth=0):
        _scanned[0] += 1
        tc.progress("tree", done=_scanned[0], total=_total_est[0], current=path)
        node = {
            'name': os.path.basename(path) or path,
            'path': path,
            'size': 0,
            'files': 0,
            'dirs': 0,
            'mtime': 0,
            'oldest': time.time(),
            'types': {},
        }
        if depth < max_depth:
            node['children'] = []

        try:
            entries = list(os.scandir(path))
        except (PermissionError, OSError):
            return node

        for entry in entries:
            try:
                if entry.is_file(follow_symlinks=False):
                    stat = entry.stat(follow_symlinks=False)
                    ftype = classify_file(entry.name)
                    fsize = stat.st_size
                    mtime = stat.st_mtime
                    node['size'] += fsize
                    node['files'] += 1
                    node['mtime'] = max(node['mtime'], mtime)
                    node['oldest'] = min(node['oldest'], mtime)
                    node['types'][ftype] = node['types'].get(ftype, 0) + fsize

                elif entry.is_dir(follow_symlinks=False):
                    node['dirs'] += 1
                    if is_trickplay_dir(entry.name):
                        tp_size = _dir_size(entry.path)
                        node['size'] += tp_size
                        node['types']['trickplay'] = node['types'].get('trickplay', 0) + tp_size
                        continue

                    if depth < max_depth:
                        child = scan_dir(entry.path, depth + 1)
                        node['children'].append(child)
                        node['size'] += child['size']
                        node['files'] += child['files']
                        node['dirs'] += child['dirs']
                        node['mtime'] = max(node['mtime'], child['mtime'])
                        node['oldest'] = min(node['oldest'], child['oldest'])
                        for t, s in child['types'].items():
                            node['types'][t] = node['types'].get(t, 0) + s
                    else:
                        ds = _dir_size(entry.path)
                        node['size'] += ds
            except OSError:
                continue

        if 'children' in node:
            node['children'].sort(key=lambda c: c['size'], reverse=True)

        if node['oldest'] == time.time():
            node['oldest'] = node['mtime']

        return node

    return scan_dir(root_path)


_dir_size = tc.dir_size  # fast recursive size without building the tree


def main():
    media_libs = tc.get_media_roots()  # config file first, then env fallback

    if not media_libs:
        print("ERROR: No MEDIA_LIBRARIES configured", file=sys.stderr)
        sys.exit(1)

    print(f"Scanning {len(media_libs)} libraries...")

    try:
        _total_est[0] = json.loads(TREE_FILE.read_text()).get("total_dirs", 0)
    except Exception:
        _total_est[0] = 0
    _scanned[0] = 0

    root = {
        'name': 'Media Libraries',
        'path': '/',
        'size': 0,
        'files': 0,
        'dirs': 0,
        'mtime': 0,
        'oldest': time.time(),
        'types': {},
        'children': []
    }

    for lib in media_libs:
        if not os.path.isdir(lib):
            print(f"  SKIP {lib} (not found)")
            continue
        print(f"  Scanning {lib}...")
        tree = scan_tree(lib)
        root['children'].append(tree)
        root['size'] += tree['size']
        root['files'] += tree['files']
        root['dirs'] += tree['dirs']
        root['mtime'] = max(root['mtime'], tree['mtime'])
        root['oldest'] = min(root['oldest'], tree['oldest'])
        for t, s in tree['types'].items():
            root['types'][t] = root['types'].get(t, 0) + s

    result = {
        'tree': root,
        'last_scan': time.strftime('%Y-%m-%d %H:%M:%S'),
        'total_size': root['size'],
        'total_files': root['files'],
        'total_dirs': root['dirs'],
    }

    tc.write_json_atomic(TREE_FILE, result, indent=None)
    tc.progress_done("tree")
    print(f"Done: {root['files']} files, {root['dirs']} dirs, "
          f"{root['size'] / (1024**3):.1f} GB → {TREE_FILE}")


if __name__ == '__main__':
    main()
