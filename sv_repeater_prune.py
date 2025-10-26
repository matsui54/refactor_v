#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import difflib
import re
from collections import defaultdict
from typing import Dict, List, Optional, Tuple

# 型メモ:
# key は "name" または "name[idx]" の文字列（ビット単位キー）
# val は (src_key, invert) で、src_key も同様に "name" or "name[idx]"。
BitMap = Dict[str, Tuple[str, bool]]

ASSIGN_RE = re.compile(
    r'^\s*assign\s+(?P<lhs>[^=]+?)\s*=\s*(?P<rhs>.+?)\s*;\s*(?P<comment>//.*)?$',
    re.M
)
DECL_RE_LINE = re.compile(r'^\s*(wire|logic|reg)(\s+signed)?\s*(\[[^]]+\]\s*)?([^;]+);\s*$', re.M)
PORT_DECL_WITH_WIDTH_RE = re.compile(
    r'\b(input|output|inout)\b(?:\s+(?:wire|logic|reg|signed|unsigned))*\s*(\[[^]]+\])?\s*([A-Za-z_]\w*)',
    re.M
)

# ANSIポート & 非ANSIポート対応（緩め）
PORT_INLINE_RE = re.compile(
    r'\b(input|output|inout)\b(?:\s+(?:wire|logic|reg|signed|unsigned))*\s*(?:\[[^]]+\]\s*)?\s*([A-Za-z_]\w*)',
    re.M)
PORT_STANDALONE_RE = re.compile(
    r'^\s*(input|output|inout)\b(?:\s+(?:wire|logic|reg|signed|unsigned))*\s*(?:\[[^]]+\]\s*)?\s*([A-Za-z_]\w*)\s*;',
    re.M)

SLICE_RE = re.compile(r'^(?P<name>[A-Za-z_]\w*)\[(?P<hi>\d+):(?P<lo>\d+)\]$')
INDEX_RE = re.compile(r'^(?P<name>[A-Za-z_]\w*)\[(?P<i>\d+)\]$')
BARE_RE  = re.compile(r'^(?P<name>[A-Za-z_]\w*)$')

REPL_RE = re.compile(
    r'^\{\s*(?P<count>\d+)\s*\{\s*(?P<inv>~\s*)?(?P<what>[A-Za-z_]\w*(?:\[\d+(?::\d+)?\])?)\s*\}\s*\}$'
)

INV_TOKEN_RE = re.compile(r'^\s*~\s*(?P<what>.+?)\s*$')

IDENT_OR_INDEX_RE = re.compile(r'([A-Za-z_]\w*(?:\[\d+(?::\d+)?\])?)')
DOUBLE_NEG_RE = re.compile(r'~\s*~\s*')

def decode_regex_pattern(pat: str) -> str:
    """
    CLI から渡された正規表現文字列の \\ エスケープ（二重化）を 1段だけ解釈する。
    例: \"copy\\\\d+\" -> \"copy\\d+\"
    """
    try:
        return bytes(pat, "utf-8").decode("unicode_escape")
    except UnicodeDecodeError:
        return pat

def parse_ports(src: str) -> set:
    """
    モジュールのANSI/非ANSIポート名の集合を抽出（宣言スキップ用）
    """
    ports = set()
    # module header 部（括弧内）も含めて拾う（緩い）
    for m in PORT_INLINE_RE.finditer(src):
        ports.add(m.group(2))
    for m in PORT_STANDALONE_RE.finditer(src):
        ports.add(m.group(2))
    return ports

def collect_decl_widths(src: str) -> Dict[str, str]:
    """収集した net/port 名ごとの幅（テキスト形式、例: [3:0]）。"""
    widths: Dict[str, str] = {}
    for m in DECL_RE_LINE.finditer(src):
        width = (m.group(3) or '').strip()
        if not width:
            continue
        names = [p.strip() for p in m.group(4).split(',')]
        for name in names:
            nm = re.match(r'^([A-Za-z_]\w*)', name)
            if nm and nm.group(1) not in widths:
                widths[nm.group(1)] = width
    for m in PORT_DECL_WITH_WIDTH_RE.finditer(src):
        width = (m.group(2) or '')
        name = m.group(3)
        if width:
            width = width.strip()
        if width and name not in widths:
            widths[name] = width
    return widths

def width_of_slice(s: str) -> Optional[int]:
    m = SLICE_RE.match(s)
    if not m:
        return None
    hi, lo = int(m.group('hi')), int(m.group('lo'))
    return abs(hi - lo) + 1

def explode_lhs_bits(lhs: str) -> List[str]:
    """
    LHSをビット単位キー列へ展開:
      - foo         -> ["foo"]
      - foo[3]      -> ["foo[3]"]
      - foo[7:4]    -> ["foo[7]", "foo[6]", "foo[5]", "foo[4]"]
    """
    lhs = lhs.strip()
    m = INDEX_RE.match(lhs)
    if m:
        return [lhs]
    m = SLICE_RE.match(lhs)
    if m:
        name = m.group('name')
        hi, lo = int(m.group('hi')), int(m.group('lo'))
        step = 1 if hi >= lo else -1
        return [f"{name}[{i}]" for i in range(hi, lo - step, -step)]
    # bare
    return [lhs]

def explode_rhs_as_refs(rhs: str, lhs_bits: int) -> Optional[List[Tuple[str,bool]]]:
    """
    RHS（単純パターン）をビット列参照へ展開。
    戻り値は長さ lhs_bits の [(src_key, invert_flag), ...]
    対応:
      - foo / ~foo （1bitとして拡張）
      - foo[3] / ~foo[3]
      - foo[7:4] / ~foo[7:4]（幅一致想定）
      - {N{foo}} / {N{~foo}} / {N{foo[i]}}
    """
    rhs = rhs.strip()

    # 反転全体 (~something)
    inv_all = False
    m_inv = INV_TOKEN_RE.match(rhs)
    if m_inv:
        inv_all = True
        rhs = m_inv.group('what').strip()

    # レプリケーション
    m_rep = REPL_RE.match(rhs)
    if m_rep:
        count = int(m_rep.group('count'))
        inner_inv = bool(m_rep.group('inv'))
        what = m_rep.group('what').strip()
        # 内部を1bit参照として扱う（一般的なリピータ想定）
        src_bits = explode_rhs_as_refs(what, 1)
        if not src_bits:
            return None
        unit = (src_bits[0][0], src_bits[0][1] ^ inner_inv ^ inv_all)
        return [unit for _ in range(lhs_bits)] if count >= lhs_bits else None

    # スライス
    m = SLICE_RE.match(rhs)
    if m:
        name = m.group('name')
        hi, lo = int(m.group('hi')), int(m.group('lo'))
        step = 1 if hi >= lo else -1
        bits = [f"{name}[{i}]" for i in range(hi, lo - step, -step)]
        if len(bits) != lhs_bits:
            return None
        return [(b, inv_all) for b in bits]

    # インデックス
    m = INDEX_RE.match(rhs)
    if m:
        b = rhs  # like "foo[3]"
        return [(b, inv_all)] * lhs_bits  # 拡張（幅不一致でも使う場所で整合）

    # ベア
    m = BARE_RE.match(rhs)
    if m:
        b = rhs
        return [(b, inv_all)] * lhs_bits

    return None

def build_replace_map(src: str, lhs_pat: str, skip_ports: set) -> BitMap:
    """
    指定の lhs パターンに一致する assign のみ対象。
    bit単位マップ (key -> (src_key, invert))
    """
    pat = re.compile(lhs_pat)
    mp: BitMap = {}

    for m in ASSIGN_RE.finditer(src):
        lhs = m.group('lhs').strip()
        rhs = m.group('rhs').strip()

        # lhs のベース名を得る
        base = lhs
        mm = INDEX_RE.match(lhs) or SLICE_RE.match(lhs) or BARE_RE.match(lhs)
        if not mm:
            continue
        if mm.re is SLICE_RE or mm.re is INDEX_RE:
            base = mm.group('name')
        else:
            base = mm.group('name')  # BARE

        if base in skip_ports:
            continue
        if not pat.search(base):
            continue

        lhs_bits = explode_lhs_bits(lhs)
        rhs_refs = explode_rhs_as_refs(rhs, len(lhs_bits))
        if not rhs_refs:
            continue

        for k, (src_key, inv) in zip(lhs_bits, rhs_refs):
            mp[k] = (src_key, inv)

    return mp

def resolve_final(src_map: BitMap, key: str, seen=None) -> Tuple[str, bool]:
    """
    置換先を再帰に辿って最終キーへ。
    反転は XOR（偶奇）で畳み込み。
    """
    if seen is None:
        seen = set()
    if key in seen:
        # ループ回避: 自身を最終とみなす
        return (key, False)
    seen.add(key)

    if key not in src_map:
        return (key, False)

    nxt, inv = src_map[key]
    base, inv2 = resolve_final(src_map, nxt, seen)
    return (base, inv ^ inv2)

def make_final_map(src_map: BitMap) -> BitMap:
    out: BitMap = {}
    for k in src_map.keys():
        out[k] = resolve_final(src_map, k)
    return out

def collapse_double_neg(expr: str) -> str:
    """Repeatedly remove sequences like ~~foo (optionally spaced)."""
    prev = None
    while prev != expr:
        prev = expr
        expr = DOUBLE_NEG_RE.sub('', expr)
    return expr

def _compact_slice_from_parts(parts: List[str]) -> Optional[str]:
    """Try to turn per-bit tokens back into a contiguous slice expression."""
    if len(parts) < 2:
        return None
    parsed = []
    for p in parts:
        m = re.match(r'^([A-Za-z_]\w*)\[(\d+)\]$', p.strip())
        if not m:
            return None
        parsed.append((m.group(1), int(m.group(2))))
    names = {n for n, _ in parsed}
    if len(names) != 1:
        return None
    deltas = [parsed[i+1][1] - parsed[i][1] for i in range(len(parsed)-1)]
    if not deltas or any(d != deltas[0] for d in deltas):
        return None
    step = deltas[0]
    if step not in (-1, 1):
        return None
    hi = parsed[0][1]
    lo = parsed[-1][1]
    if step == 1:  # ascending
        hi, lo = parsed[-1][1], parsed[0][1]
    name = parsed[0][0]
    return f"{name}[{hi}:{lo}]"

def _replace_token(tok: str, repl_table: Dict[str, str]) -> str:
    """Replace identifiers, including slice tokens, using per-bit table."""
    m = SLICE_RE.match(tok)
    if m:
        name = m.group('name')
        hi = int(m.group('hi'))
        lo = int(m.group('lo'))
        if hi >= lo:
            idxs = range(hi, lo - 1, -1)
        else:
            idxs = range(hi, lo + 1)
        parts = []
        for idx in idxs:
            key = f"{name}[{idx}]"
            parts.append(repl_table.get(key, key))
        if not parts:
            return tok
        if len(parts) == 1:
            return parts[0]
        if all(p == parts[0] for p in parts):
            return f"{{{len(parts)}{{{parts[0]}}}}}"
        compact = _compact_slice_from_parts(parts)
        if compact:
            return compact
        return "{" + ", ".join(parts) + "}"
    return repl_table.get(tok, tok)

def _should_collapse_lhs(slice_m, new_rhs: str) -> bool:
    expr = new_rhs.strip()
    if not expr:
        return False
    # collapse when RHS is replication of a single token (no explicit comma)
    if expr.startswith('{') and expr.endswith('}') and ',' not in expr:
        return True
    return False

def replace_in_rhs_only(line: str, repl_table: Dict[str, str], decl_widths: Dict[str, str]) -> str:
    """
    assign 行の右辺だけを置換。assign 以外の行はこの関数を使わない。
    """
    m = ASSIGN_RE.match(line)
    if not m:
        return line
    lhs = m.group('lhs')
    rhs = m.group('rhs')
    comment = m.group('comment') or ''
    indent = line[:len(line) - len(line.lstrip())]
    lhs_render = lhs
    slice_m = SLICE_RE.match(lhs.strip())

    def repl_token(match):
        tok = match.group(1)
        return _replace_token(tok, repl_table)

    new_rhs = IDENT_OR_INDEX_RE.sub(repl_token, rhs)
    new_rhs = collapse_double_neg(new_rhs)
    if slice_m and _should_collapse_lhs(slice_m, new_rhs):
        base = slice_m.group('name')
        width = decl_widths.get(base)
        if width:
            slice_txt = f"[{slice_m.group('hi')}:{slice_m.group('lo')}]"
            if width.replace(' ', '') == slice_txt.replace(' ', ''):
                lhs_render = base
    suffix = f" {comment}" if comment else ""
    return f"{indent}assign {lhs_render} = {new_rhs};{suffix}"

def global_replace_line(line: str, repl_table: Dict[str, str]) -> str:
    def repl_token(match):
        tok = match.group(1)
        return _replace_token(tok, repl_table)
    return IDENT_OR_INDEX_RE.sub(repl_token, line)

def build_repl_table(final_map: BitMap) -> Dict[str, str]:
    """
    "name" / "name[i]" -> "base" or "~base" の置換表を構築
    """
    table: Dict[str, str] = {}
    # より長いキー（name[idx]）を先に適用したいので別に保持しておく手もあるが
    # 正規表現置換なので token 単位で問題になりにくい。
    for k, (src, inv) in final_map.items():
        table[k] = f"~{src}" if inv else src

        # name[i] の親 name について、完全一致の scalar を作る必要はない。
        # scalar lhs がある場合は final_map 側に同名キーが入る。
    return table

def collect_assign_lhs_names(src: str, lhs_pat: str) -> set:
    pat = re.compile(lhs_pat)
    names = set()
    for m in ASSIGN_RE.finditer(src):
        lhs = m.group('lhs').strip()
        mm = INDEX_RE.match(lhs) or SLICE_RE.match(lhs) or BARE_RE.match(lhs)
        if not mm:
            continue
        base = mm.group('name')
        if pat.search(base):
            names.add(base)
    return names

def prune_unused_assigns_and_decls(src: str, target_names: set) -> str:
    """
    target_names（置換対象の lhs ベース名）について、
    ・置換後ソース中で、その名前が assign 左辺/宣言 以外に現れなければ
      - その assign 行を削除
      - その宣言行から該当名を除去（残り無しなら行ごと削除）
    """
    lines = src.splitlines(keepends=False)

    # 1) 利用状況カウント（assign 左辺と宣言は除外）
    uses = {n: 0 for n in target_names}

    # まず assign 左辺位置と宣言行を特定
    is_assign_line = [False]*len(lines)
    assign_lhs_names_per_line: List[set] = [set() for _ in lines]
    is_decl_line   = [False]*len(lines)

    for i, line in enumerate(lines):
        if ASSIGN_RE.match(line):
            is_assign_line[i] = True
            lhs = ASSIGN_RE.match(line).group('lhs').strip()
            mm = INDEX_RE.match(lhs) or SLICE_RE.match(lhs) or BARE_RE.match(lhs)
            if mm:
                base = mm.group('name')
                assign_lhs_names_per_line[i].add(base)

        if DECL_RE_LINE.match(line):
            is_decl_line[i] = True

    # カウント：各行で token を見て、assign 左辺/宣言に該当するトークンは除外
    for i, line in enumerate(lines):
        # 行内で現れる識別子
        tokens = IDENT_OR_INDEX_RE.findall(line)
        # 除外トークン集合
        exclude = set()
        if is_assign_line[i]:
            exclude |= assign_lhs_names_per_line[i]
        if is_decl_line[i]:
            # 宣言行に現れる各名を除外（型/幅など除く）
            decl_m = DECL_RE_LINE.match(line)
            if decl_m:
                # 末尾の名前群（カンマ区切り）
                names_part = decl_m.group(4)
                # 各要素から識別子抽出
                for name in re.findall(r'[A-Za-z_]\w*', names_part):
                    exclude.add(name)

        for t in tokens:
            # t は "name" または "name[idx]"
            base = t.split('[')[0]
            if base in target_names and base not in exclude:
                uses[base] += 1

    # 2) 削除対象名
    to_remove = {n for n, c in uses.items() if c == 0}

    if not to_remove:
        return src

    # 3) 行の削除/調整
    out_lines: List[str] = []
    for i, line in enumerate(lines):
        if is_assign_line[i]:
            # 対象名がこの assign 左辺なら行ごと削除
            lhs = ASSIGN_RE.match(line).group('lhs').strip()
            base = (INDEX_RE.match(lhs) or SLICE_RE.match(lhs) or BARE_RE.match(lhs)).group('name')
            if base in to_remove:
                continue  # drop

        if is_decl_line[i]:
            decl_m = DECL_RE_LINE.match(line)
            names_part = decl_m.group(4)
            # カンマ区切りの宣言名をパースして対象名だけ除去
            parts = [p.strip() for p in names_part.split(',')]
            keep_parts = []
            for p in parts:
                # 末尾に次元 [..] を含む場合があるので先頭の識別子だけ見る
                nm = re.match(r'^([A-Za-z_]\w*)', p)
                if not nm:
                    keep_parts.append(p)
                    continue
                base = nm.group(1)
                if base in to_remove:
                    continue
                keep_parts.append(p)
            if len(keep_parts) == 0:
                # 行ごと削除
                continue
            else:
                # 再構築
                prefix = decl_m.group(0)[:decl_m.start(4)-decl_m.start(0)]
                # prefix は "wire [..] " のような前半。安全のため組み直す:
                head = ''.join(line[:decl_m.start(4)])
                tail = ''.join(line[decl_m.end(4):])
                new_line = head + ', '.join(keep_parts) + tail
                out_lines.append(new_line)
                continue

        out_lines.append(line)

    return '\n'.join(out_lines) + ('\n' if src.endswith('\n') else '')

def main():
    ap = argparse.ArgumentParser(description='Remove manual repeaters in Verilog by resolving copy/pow nets.')
    ap.add_argument('file', help='Input Verilog file')
    ap.add_argument('--lhs-pattern', required=True,
                    help=r'Regex for LHS base names to target (e.g. "(copy\d+|cpy\d+|pow\d+)")')
    ap.add_argument('--inplace', action='store_true', help='Overwrite the input file')
    ap.add_argument('--encoding', default='utf-8')
    args = ap.parse_args()

    with open(args.file, 'r', encoding=args.encoding) as f:
        orig = f.read()

    lhs_pattern = decode_regex_pattern(args.lhs_pattern)

    ports = parse_ports(orig)
    decl_widths = collect_decl_widths(orig)
    replace_map = build_replace_map(orig, lhs_pattern, ports)
    final_map = make_final_map(replace_map)
    repl_table = build_repl_table(final_map)

    # 置換：assign 行は RHS のみ、宣言行はスキップ、それ以外は全体置換
    lines = orig.splitlines(keepends=False)
    new_lines: List[str] = []
    for line in lines:
        if ASSIGN_RE.match(line):
            new_lines.append(replace_in_rhs_only(line, repl_table, decl_widths))
        elif DECL_RE_LINE.match(line):
            new_lines.append(line)  # 宣言はここでは触らない（後で prune）
        else:
            new_lines.append(global_replace_line(line, repl_table))
    replaced = '\n'.join(new_lines) + ('\n' if orig.endswith('\n') else '')

    # 不要な assign / 宣言 を削除
    target_bases = collect_assign_lhs_names(orig, lhs_pattern)
    pruned = prune_unused_assigns_and_decls(replaced, target_bases)

    # diff 表示
    diff = ''.join(difflib.unified_diff(
        orig.splitlines(keepends=True),
        pruned.splitlines(keepends=True),
        fromfile=args.file + ' (before)',
        tofile=args.file + ' (after)',
        n=3
    ))
    print(diff, end='')

    if args.inplace:
        with open(args.file, 'w', encoding=args.encoding) as f:
            f.write(pruned)
    else:
        # diff の後に変換後全文も出力したい場合は以下のコメントアウトを外す
        # print(pruned, end='')
        pass

if __name__ == '__main__':
    main()
