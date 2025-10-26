#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import difflib
import re
from collections import defaultdict
from typing import Dict, List, Optional, Tuple

BitIndex = Optional[int]
BitMap = Dict[str, Dict[BitIndex, Tuple[str, bool]]]

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

# 右辺/その他のトークン置換検出（スライスも拾う）
IDENT_OR_INDEX_RE = re.compile(r'([A-Za-z_]\w*(?:\[\d+(?::\d+)?\])?)')
DOUBLE_NEG_RE = re.compile(r'~\s*~\s*')

# -------------------------
# ヘルパ
# -------------------------
def iter_slice_indices(hi: int, lo: int):
    """
    Yield the inclusive indices for a `[hi:lo]` slice, handling both descending
    (`[3:0]`) and ascending (`[0:3]`) notations.
    """
    if hi >= lo:
        return range(hi, lo - 1, -1)
    return range(hi, lo + 1)

def parse_ports(src: str) -> set:
    """
    Collect the set of port names declared either in ANSI headers or in separate
    standalone port statements inside the module body.
    """
    ports = set()
    for m in PORT_INLINE_RE.finditer(src):
        ports.add(m.group(2))
    for m in PORT_STANDALONE_RE.finditer(src):
        ports.add(m.group(2))
    return ports

def collect_decl_widths(src: str) -> Dict[str, str]:
    """
    Build a dictionary of `net_name -> "[msb:lsb]"` for both regular
    `wire|logic|reg` declarations and ANSI-style port declarations.
    """
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

def parse_width_range(width: str) -> Optional[Tuple[int, int]]:
    """
    Parse a packed dimension like "[7:0]" into integer `(hi, lo)` bounds.

    Returns None when the dimension is absent or contains expressions that are
    not simple integers (e.g. `[WIDTH-1:0]`).
    """
    if not width:
        return None
    m = re.match(r'^\[\s*(\d+)\s*:\s*(\d+)\s*\]$', width)
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))

def decompose_lhs(lhs: str) -> Tuple[str, List[BitIndex]]:
    """
    Split an assignment LHS into `(base_name, [bit_indices...])`.

    Examples:
        foo         -> ("foo", [None])
        foo[3]      -> ("foo", [3])
        foo[7:4]    -> ("foo", [7, 6, 5, 4])
    """
    lhs = lhs.strip()
    m = INDEX_RE.match(lhs)
    if m:
        return m.group('name'), [int(m.group('i'))]
    m = SLICE_RE.match(lhs)
    if m:
        name = m.group('name')
        hi, lo = int(m.group('hi')), int(m.group('lo'))
        return name, list(iter_slice_indices(hi, lo))
    # bare
    return BARE_RE.match(lhs).group('name'), [None]

def parse_key_to_name_idx(tok: str) -> Tuple[str, BitIndex]:
    """
    Convert an identifier like `foo` or `foo[3]` into `(name, index)` where
    scalars are represented as `None`.
    """
    m = INDEX_RE.match(tok)
    if m:
        return m.group('name'), int(m.group('i'))
    return BARE_RE.match(tok).group('name'), None

def render_token(base: str, idx: BitIndex) -> str:
    """Render `(base, idx)` back into `base` or `base[idx]`."""
    return base if idx is None else f"{base}[{idx}]"

# -------------------------
# RHS 展開: 右辺を LHS のビット列長に合わせて [(src_key_str, invert)] に展開
# -------------------------
def explode_rhs_as_refs(rhs: str, lhs_bits: int) -> Optional[List[Tuple[str,bool]]]:
    """
    Expand the RHS expression into a list of `(token, invert)` pairs whose
    length matches `lhs_bits`.

    Handles single identifiers, indexed bits, slices, and replication forms
    like `{4{foo}}`, propagating explicit bit widths so that each LHS bit knows
    which source bit it mirrors.
    """
    rhs = rhs.strip()

    inv_all = False
    m_inv = INV_TOKEN_RE.match(rhs)
    if m_inv:
        inv_all = True
        rhs = m_inv.group('what').strip()

    m_rep = REPL_RE.match(rhs)
    if m_rep:
        count = int(m_rep.group('count'))
        inner_inv = bool(m_rep.group('inv'))
        what = m_rep.group('what').strip()
        src_bits = explode_rhs_as_refs(what, 1)
        if not src_bits:
            return None
        unit = (src_bits[0][0], src_bits[0][1] ^ inner_inv ^ inv_all)
        return [unit for _ in range(lhs_bits)] if count >= lhs_bits else None

    m = SLICE_RE.match(rhs)
    if m:
        name = m.group('name')
        hi, lo = int(m.group('hi')), int(m.group('lo'))
        bits = [f"{name}[{i}]" for i in iter_slice_indices(hi, lo)]
        if len(bits) != lhs_bits:
            return None
        return [(b, inv_all) for b in bits]

    m = INDEX_RE.match(rhs)
    if m:
        b = rhs
        return [(b, inv_all)] * lhs_bits

    m = BARE_RE.match(rhs)
    if m:
        b = rhs
        return [(b, inv_all)] * lhs_bits

    return None

# -------------------------
# replace_map を二段マップで作成
# -------------------------
def build_replace_map(src: str, lhs_pat: str, skip_ports: set, decl_widths: Dict[str, str]) -> BitMap:
    """
    Build the raw map from targeted assign LHS bits to their driving expression.

    Each entry takes the form `map[base][bit_idx] = ("src_token", invert)` where
    `bit_idx` is `None` for unsliced assignments. When the declaration width is
    a literal range such as `[3:0]`, full-vector assignments are expanded to the
    appropriate per-bit keys so later slice references (e.g. `foo[2:1]`) can
    still resolve correctly.
    """
    pat = re.compile(lhs_pat)
    mp: BitMap = defaultdict(dict)

    for m in ASSIGN_RE.finditer(src):
        lhs = m.group('lhs').strip()
        rhs = m.group('rhs').strip()

        base, lhs_idx_list = decompose_lhs(lhs)
        if base in skip_ports:
            continue
        if not pat.search(base):
            continue

        if lhs_idx_list == [None]:
            inferred = parse_width_range(decl_widths.get(base, ''))
            if inferred:
                hi, lo = inferred
                lhs_idx_list = list(iter_slice_indices(hi, lo))

        rhs_refs = explode_rhs_as_refs(rhs, len(lhs_idx_list))
        if not rhs_refs:
            continue

        for dst_idx, (src_key, inv) in zip(lhs_idx_list, rhs_refs):
            mp[base][dst_idx] = (src_key, inv)

    return dict(mp)

# -------------------------
# 再帰解決（二段マップ版）
# -------------------------
def resolve_final(src_map: BitMap, base: str, idx: int, seen=None) -> Tuple[str, bool]:
    """
    Recursively resolve `(base, idx)` until it reaches a token that no longer
    appears in `src_map`, folding inversion bits along the path.
    """
    if seen is None:
        seen = set()
    key = (base, idx)
    if key in seen:
        return (render_token(base, idx), False)  # ループ回避
    seen.add(key)

    if base not in src_map or idx not in src_map[base]:
        return (render_token(base, idx), False)

    nxt_key_str, inv = src_map[base][idx]
    nbase, nidx = parse_key_to_name_idx(nxt_key_str)

    final_key_str, inv2 = resolve_final(src_map, nbase, nidx, seen)
    return (final_key_str, inv ^ inv2)

def make_final_map(src_map: BitMap) -> BitMap:
    """
    Apply `resolve_final` to every `(base, bit_idx)` combination so later
    lookups can be constant-time dictionary reads instead of recursive walks.
    """
    out: BitMap = {}
    for base, inner in src_map.items():
        out_inner: Dict[int, Tuple[str, bool]] = {}
        for idx in inner.keys():
            out_inner[idx] = resolve_final(src_map, base, idx)
        out[base] = out_inner
    return out

# -------------------------
# 置換テーブル作成（二段マップ→トークン→置換文字列）
# -------------------------
def build_repl_table(final_map: BitMap) -> Dict[str, str]:
    """
      "name" / "name[i]" -> "base" or "~base" or "foo[j]" / "~foo[j]" を生成
    """
    table: Dict[str, str] = {}
    for base, inner in final_map.items():
        for idx, (src_key, inv) in inner.items():
            tok = render_token(base, idx)
            table[tok] = f"~{src_key}" if inv else src_key
    return table

# -------------------------
# 文字列置換系（既存ロジックを流用）
# -------------------------
def collapse_double_neg(expr: str) -> str:
    """Repeatedly remove `~~foo` style constructs that may appear post-rewrite."""
    prev = None
    while prev != expr:
        prev = expr
        expr = DOUBLE_NEG_RE.sub('', expr)
    return expr

def _compact_slice_from_parts(parts: List[str]) -> Optional[str]:
    """
    Attempt to compress a list like `["bus[7]", "bus[6]", ...]` back into the
    canonical slice `bus[7:4]`. Returns None when the bits are discontiguous or
    sourced from multiple nets.
    """
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
    if step == 1:
        hi, lo = parsed[-1][1], parsed[0][1]
    name = parsed[0][0]
    return f"{name}[{hi}:{lo}]"

def _assemble_parts(name: str, idxs, repl_table: Dict[str, str]) -> List[str]:
    parts: List[str] = []
    for idx in idxs:
        key = f"{name}[{idx}]"
        parts.append(repl_table.get(key, key))
    return parts

def _replace_token(tok: str, repl_table: Dict[str, str], decl_widths: Dict[str, str], allow_vector_assembly: bool) -> str:
    """
    Replace identifiers (scalar, indexed, or sliced) using the bit-level table.

    Slices are expanded bit-by-bit, rewritten, then compacted back into either
    a replication literal or another slice when possible.
    """
    m = SLICE_RE.match(tok)
    if m:
        name = m.group('name')
        hi = int(m.group('hi'))
        lo = int(m.group('lo'))
        parts = _assemble_parts(name, iter_slice_indices(hi, lo), repl_table)
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
    if tok in repl_table:
        return repl_table[tok]
    if allow_vector_assembly:
        rng = parse_width_range(decl_widths.get(tok, ''))
    else:
        rng = None
    if rng:
        hi, lo = rng
        parts = _assemble_parts(tok, iter_slice_indices(hi, lo), repl_table)
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
    return tok

def _should_collapse_lhs(slice_m, new_rhs: str) -> bool:
    """
    Decide whether a sliced LHS (e.g. `copy[1:0]`) can be collapsed back to the
    bare name. We only do so when the RHS turned into a replication literal,
    meaning every bit carries the same driver.
    """
    expr = new_rhs.strip()
    if not expr:
        return False
    if expr.startswith('{') and expr.endswith('}') and ',' not in expr:
        return True
    return False

def replace_in_rhs_only(line: str, repl_table: Dict[str, str], decl_widths: Dict[str, str]) -> str:
    """
    Rewrite only the RHS of an `assign` statement using the replacement table,
    keep indentation/comments, and optionally collapse the LHS slice.
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
        """Regex callback that swaps tokens using the replacement table."""
        tok = match.group(1)
        return _replace_token(tok, repl_table, decl_widths, allow_vector_assembly=True)

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

def global_replace_line(line: str, repl_table: Dict[str, str], decl_widths: Dict[str, str]) -> str:
    """Perform identifier replacement across an arbitrary line (non-assign)."""
    def repl_token(match):
        """Regex callback shared by global replacements."""
        tok = match.group(1)
        return _replace_token(tok, repl_table, decl_widths, allow_vector_assembly=False)
    return IDENT_OR_INDEX_RE.sub(repl_token, line)

def collect_assign_lhs_names(src: str, lhs_pat: str) -> set:
    """
    Collect the set of base names that appear on the LHS of targeted assigns.

    Used later to determine which declarations/assignments can be pruned once
    their nets become dead.
    """
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
    Remove redundant assign statements and declaration fragments for nets whose
    names appear in `target_names` but are no longer referenced elsewhere.

    The analysis skips assign LHS tokens and declaration headers so we do not
    accidentally count the definitions themselves as "uses".
    """
    lines = src.splitlines(keepends=False)

    uses = {n: 0 for n in target_names}
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

    for i, line in enumerate(lines):
        tokens = IDENT_OR_INDEX_RE.findall(line)
        exclude = set()
        if is_assign_line[i]:
            exclude |= assign_lhs_names_per_line[i]
        if is_decl_line[i]:
            decl_m = DECL_RE_LINE.match(line)
            if decl_m:
                names_part = decl_m.group(4)
                for name in re.findall(r'[A-Za-z_]\w*', names_part):
                    exclude.add(name)

        for t in tokens:
            base = t.split('[')[0]
            if base in target_names and base not in exclude:
                uses[base] += 1

    to_remove = {n for n, c in uses.items() if c == 0}
    if not to_remove:
        return src

    out_lines: List[str] = []
    for i, line in enumerate(lines):
        if is_assign_line[i]:
            lhs = ASSIGN_RE.match(line).group('lhs').strip()
            base = (INDEX_RE.match(lhs) or SLICE_RE.match(lhs) or BARE_RE.match(lhs)).group('name')
            if base in to_remove:
                continue  # drop

        if is_decl_line[i]:
            decl_m = DECL_RE_LINE.match(line)
            names_part = decl_m.group(4)
            parts = [p.strip() for p in names_part.split(',')]
            keep_parts = []
            for p in parts:
                nm = re.match(r'^([A-Za-z_]\w*)', p)
                if not nm:
                    keep_parts.append(p)
                    continue
                base = nm.group(1)
                if base in to_remove:
                    continue
                keep_parts.append(p)
            if len(keep_parts) == 0:
                continue
            else:
                head = ''.join(line[:decl_m.start(4)])
                tail = ''.join(line[decl_m.end(4):])
                new_line = head + ', '.join(keep_parts) + tail
                out_lines.append(new_line)
                continue

        out_lines.append(line)

    return '\n'.join(out_lines) + ('\n' if src.endswith('\n') else '')

# -------------------------
# main
# -------------------------
def main():
    """CLI entrypoint for the repeater pruner."""
    ap = argparse.ArgumentParser(description='Remove manual repeaters in Verilog by resolving copy/pow nets.')
    ap.add_argument('file', help='Input Verilog file')
    ap.add_argument('--lhs-pattern', required=True,
                    help=r'Regex for LHS base names to target (e.g. "(copy\d+|cpy\d+|pow\d+)")')
    ap.add_argument('--inplace', action='store_true', help='Overwrite the input file')
    ap.add_argument('--encoding', default='utf-8')
    args = ap.parse_args()

    with open(args.file, 'r', encoding=args.encoding) as f:
        orig = f.read()

    lhs_pattern = args.lhs_pattern

    ports = parse_ports(orig)
    decl_widths = collect_decl_widths(orig)

    replace_map = build_replace_map(orig, lhs_pattern, ports, decl_widths)
    final_map = make_final_map(replace_map)
    repl_table = build_repl_table(final_map)

    lines = orig.splitlines(keepends=False)
    new_lines: List[str] = []
    for line in lines:
        if ASSIGN_RE.match(line):
            new_lines.append(replace_in_rhs_only(line, repl_table, decl_widths))
        elif DECL_RE_LINE.match(line):
            new_lines.append(line)
        else:
            new_lines.append(global_replace_line(line, repl_table, decl_widths))
    replaced = '\n'.join(new_lines) + ('\n' if orig.endswith('\n') else '')

    target_bases = collect_assign_lhs_names(orig, lhs_pattern)
    pruned = prune_unused_assigns_and_decls(replaced, target_bases)

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

if __name__ == '__main__':
    main()
