#!/usr/bin/env python3
import re
import sys
import argparse
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Sequence, Set, Tuple, Union

BEGIN = r'// @extract-begin'
END   = r'// @extract-end'

# --------------------------------------------------
# Utility
# --------------------------------------------------


def strip_comments(text: str) -> str:
    """Remove /* ... */ and // ... comments while preserving newlines."""
    without_block = re.sub(r'/\*.*?\*/', '', text, flags=re.S)
    return re.sub(r'//.*', '', without_block)


@dataclass
class PortInfo:
    direction: str
    width: str = ""

    def __iter__(self):
        """Allow tuple-unpacking semantics (direction, width)."""
        yield self.direction
        yield self.width

    def __eq__(self, other):
        """Support comparisons against other PortInfo instances or (dir, width) tuples."""
        if isinstance(other, PortInfo):
            return (self.direction, self.width) == (other.direction, other.width)
        if isinstance(other, tuple):
            return (self.direction, self.width) == other
        return NotImplemented


@dataclass
class SignalRecord:
    is_input: bool = False
    is_output: bool = False
    width: str = ""

    def update_width(self, width: str) -> None:
        """Persist the first non-empty width that becomes available."""
        width = width.strip()
        if width and not self.width:
            self.width = width

    def mark_input(self, width: str) -> None:
        """Mark the signal as an input unless it has already become an output."""
        if self.is_output:
            return
        self.is_input = True
        self.update_width(width)

    def clear_input(self) -> None:
        """Drop the input flag (used when a signal upgrades to output)."""
        self.is_input = False

    def mark_output(self, width: str) -> None:
        """Mark the signal as an output and inherit the provided width."""
        self.is_output = True
        self.clear_input()
        self.update_width(width)

def read_module_src(mod_name: str, search_dirs: Union[Sequence[Union[str, Path]], str, Path]) -> str:
    """
    Locate `<mod_name>.sv|.v` under the provided directories and return its text.

    The search mimics `-I` behaviour: iterate the directories, pick the first
    existing match, and raise when multiple different files resolve to the same
    module name to avoid silently pulling in the wrong definition.
    """
    if isinstance(search_dirs, (str, Path)):
        search_dirs = [search_dirs]

    found = []
    for d in search_dirs:
        d = Path(d)
        for ext in ("sv", "v"):
            p = d / f"{mod_name}.{ext}"
            if p.exists():
                found.append(p)

    if len(found) == 0:
        raise FileNotFoundError(f"Module file not found for '{mod_name}' in: {', '.join(map(str, search_dirs))}")
    # 複数見つかったら衝突（意図しないファイルを拾わないようにする）
    if len(found) > 1:
        # ただし、全く同じパスが重複しただけなら1つに圧縮
        uniq = sorted(set(map(str, found)))
        if len(uniq) > 1:
            raise Exception(f"Multiple module files for '{mod_name}' found: {', '.join(uniq)}")
        found = [Path(uniq[0])]

    with open(found[0], encoding="utf-8") as f:
        return f.read()

def split_with_markers(src: str):
    """
    Split the source string into `(pre, block, post)` segments using the
    `// @extract-begin`/`// @extract-end` sentinels.

    Raises:
        ValueError: if either marker is missing or they are nested improperly.
    """
    m1 = re.search(BEGIN, src)
    m2 = re.search(END, src)
    if not m1 or not m2 or m1.end() > m2.start():
        raise ValueError("extract markers not found or malformed.")
    pre  = src[:m1.start()]
    block= src[m1.end():m2.start()]
    post = src[m2.end():]
    return pre, block, post

def parse_parent_decls(src: str):
    """
    Return `{signal: width}` for every `wire|reg|logic` declaration in `src`.

    Example:
        logic signed [7:0] data0, data1;
    produces `{"data0": "[7:0]", "data1": "[7:0]"}` which later lets us inherit
    widths when promoting signals to ports.
    """
    decls = {}
    decl_re = re.compile(
        r'^\s*(wire|reg|logic)\b(?:\s+signed\b)?\s*(\[[^\]]+\])?\s*([^;]+);\s*$',
        re.M
    )
    for m in decl_re.finditer(src):
        width = (m.group(2) or '').strip()
        names = m.group(3)
        for name in re.split(r'\s*,\s*', names):
            base = re.split(r'\s|\[|=|\{', name.strip())[0]
            if re.match(r'[A-Za-z_]\w*$', base):
                decls[base] = width
    return decls

# -- ヘルパ: "a, b[3], /*c*/ d" → ["a", "b", "d"]
def _split_ident_list(idlist: str):
    """
    Split a comma-separated identifier list (which may contain comments, bit
    slices, or initialisers) into clean base names.

    Example: `"a, b[3], /* c */ d = 1'b0"` -> `["a", "b", "d"]`.
    """
    s = strip_comments(idlist)
    names = []
    for tok in re.split(r'\s*,\s*', s.strip()):
        base = re.split(r'\s|\[|=|\{', tok.strip())[0]  # unpacked/初期化子/添字を落とす
        if re.match(r'^[A-Za-z_]\w*$', base or ''):
            names.append(base)
    return names

# -- ヘルパ: "input|output|inout ..." 形式の宣言ブロックから辞書を取る
def _collect_ports_from_decl(text: str, prefer: str = 'first') -> Tuple[Dict[str, PortInfo], List[str]]:
    """
    Parse a block of `input|output|inout` statements and build
    `(port_dir, order)` collections.

    Args:
        text: chunk of Verilog containing one or more port declarations.
        prefer: whether to keep the first occurrence (`'first'`) or allow later
            declarations to overwrite earlier ones.
    """
    port_dir: Dict[str, PortInfo] = {}
    order: List[str] = []
    decl_re = re.compile(
        r'^\s*(input|output|inout)\b'     # 方向
        r'(?:\s+\w+)*'                    # 型/キーワード（logic, wire, reg, signed など）
        r'(?:\s*(\[[^\]]+\]))?'           # packed 幅（任意）
        r'\s+([^;]+?)\s*;'                # ; までの識別子列
        r'\s*$', re.M)
    for m in decl_re.finditer(text):
        d, width, idlist = m.groups()
        width = (width or '').strip()
        for name in _split_ident_list(idlist):
            info = PortInfo(direction=d, width=width)
            if name not in port_dir:
                port_dir[name] = info
                order.append(name)
            elif prefer != 'first':
                port_dir[name] = info
                order.append(name)
    return port_dir, order

# -- ヘッダの (...) 部分だけを抜き出してパース
def _parse_ports_from_header(src: str) -> Tuple[Dict[str, PortInfo], List[str]]:
    """
    Extract ANSI-style port declarations from the `module (...) ;` header.

    The function segments the parameter list by direction keyword, appends a
    pseudo semicolon, and reuses `_collect_ports_from_decl` for the heavy
    lifting.
    """
    header_port_dir: Dict[str, PortInfo] = {}
    header_order: List[str] = []
    mod_hdr_re = re.compile(r'module\s+[A-Za-z_]\w*\s*\((?P<plist>.*?)\)\s*;', re.S)
    mh = mod_hdr_re.search(src)
    if not mh:
        return header_port_dir, header_order  # ヘッダ未検出（古い non-ANSI だけのケース）
    plist = mh.group('plist')

    # 方向キーワード境界でセグメント化
    segs = []
    tok_re = re.compile(r'(input|output|inout)\b', re.I)
    positions = [m.start() for m in tok_re.finditer(plist)]
    if positions:
        positions.append(len(plist))
        for i in range(len(positions)-1):
            seg = plist[positions[i]:positions[i+1]]
            segs.append(seg.strip() + ';')  # 疑似セミコロンを付与
    header_text = "\n".join(segs)

    if header_text.strip():
        header_port_dir, header_order = _collect_ports_from_decl(header_text, prefer='first')
    return header_port_dir, header_order

# -- 本体部（endmodule まで）から non-ANSI 宣言をパース
def _parse_ports_from_body(src: str) -> Tuple[Dict[str, PortInfo], List[str]]:
    """
    Scan the module body (after the closing `);`) for non-ANSI
    `input|output|inout` declarations and return the same `(port_dir, order)`
    tuple as the header parser.
    """
    body_port_dir: Dict[str, PortInfo] = {}
    body_order: List[str] = []
    # まず最初の module のヘッダ終端を探す
    hdr_end = re.search(r'module\s+[A-Za-z_]\w*\s*\(.*?\)\s*;', src, flags=re.S)
    if hdr_end:
        body = src[hdr_end.end():]
    else:
        # ヘッダ無し（module m;）のケースは全体を body として扱う
        m0 = re.search(r'module\s+[A-Za-z_]\w*\s*;', src)
        if m0:
            body = src[m0.end():]
        else:
            body = src

    # endmodule より先は切り落とす（最初の endmodule を想定）
    em = re.search(r'\bendmodule\b', body)
    if em:
        body = body[:em.start()]

    body = strip_comments(body)
    if body.strip():
        body_port_dir, body_order = _collect_ports_from_decl(body, prefer='first')
    return body_port_dir, body_order

def parse_module_ports(src: str) -> Tuple[Dict[str, PortInfo], List[str]]:
    """
    Parse both ANSI header ports and non-ANSI body declarations, then merge
    them into a single `{name: PortInfo}` dictionary plus an ordered list.

    The header wins when both styles declare the same port so that modern code
    does not get overridden by legacy repetitions.
    """
    header_dir, header_order = _parse_ports_from_header(src)
    body_dir,   body_order   = _parse_ports_from_body(src)

    port_dir: Dict[str, PortInfo] = {}
    order: List[str] = []
    seen: Set[str] = set()

    # ヘッダ優先で追加
    for n in header_order:
        if n not in seen:
            port_dir[n] = header_dir[n]
            order.append(n)
            seen.add(n)

    # 本体から、未定義のものだけ追加
    for n in body_order:
        if n not in seen:
            port_dir[n] = body_dir[n]
            order.append(n)
            seen.add(n)

    return port_dir, order

def find_instances(block_src: str):
    """
    Return the set of module names instantiated inside the extraction block.

    Example:
        foo u0 (...);
        bar u1 (...);
    yields `{"foo", "bar"}` which we later use to parse callee port
    definitions.
    """
    mods = set()
    cleaned = strip_comments(block_src)
    for m in re.finditer(r'^\s*([A-Za-z_]\w*)\s+[A-Za-z_]\w*\s*\(', cleaned, flags=re.M):
        mods.add(m.group(1))
    return mods

def parse_instance_conns(block_src: str, mod_name: str) -> Dict[str, Set[str]]:
    """
    Convert `.Port(expr)` connections for `mod_name` into a dictionary of
    `port -> {base_signal}`.

    Only the base identifier matters (e.g. `.AAA(aaa[3:2])` -> `"aaa"`), so
    slices, concatenations, and simple expressions are tolerated as long as we
    can find identifier tokens inside them.
    """
    out: Dict[str, Set[str]] = {}
    inst_re = re.compile(
        rf'{mod_name}\s+[A-Za-z_]\w*\s*\(\s*(?P<body>.*?)\s*\)\s*;',
        re.S
    )
    search_space = strip_comments(block_src)
    for im in inst_re.finditer(search_space):
        body = strip_comments(im.group('body'))
        # .Port(expr) を順に抽出
        for p in re.finditer(r'\.\s*([A-Za-z_]\w*)\s*\(\s*([^)]+?)\s*\)', body):
            port, expr = p.groups()
            # コメントを除去
            expr = strip_comments(expr)

            # 信号候補を抽出（識別子ベース部を取得）
            sigs = set()
            for token in re.findall(r'[A-Za-z_]\w*(?:\[[^\]]+\])?', expr):
                base = token.split('[', 1)[0]  # ビットスライス削除
                sigs.add(base)
            if sigs:
                out.setdefault(port, set()).update(sigs)
    return out

def collect_assign_rw(block_src: str) -> Tuple[Set[str], Set[str]]:
    """
    Return `(lhs_set, rhs_set)` for every `assign` statement in the block.

    Example:
        assign foo[3:0] = bar[3:0] & baz;
    yields `lhs_set={"foo"}` and `rhs_set={"bar", "baz"}` which later drive
    input/output inference.
    """
    lhs_set: Set[str] = set()
    rhs_set: Set[str] = set()
    text = strip_comments(block_src)

    # assign 行ごとに抽出（セミコロンで終わる）
    for m in re.finditer(r'^\s*assign\s+(.+?);\s*$', text, flags=re.M):
        stmt = m.group(1)
        # LHS = RHS に分割（最初の = で）
        if '=' not in stmt:
            continue
        lpart, rpart = stmt.split('=', 1)
        ltok = lpart.strip()
        # LHS ベース名（スライス/添字除去）
        lhs_base = ltok.split('[', 1)[0].strip()
        if re.match(r'[A-Za-z_]\w*$', lhs_base):
            lhs_set.add(lhs_base)

        # RHS 識別子（スライス許容） → ベース名
        for token in re.findall(r'[A-Za-z_]\w*(?:\[[^\]]+\])?', rpart):
            base = token.split('[', 1)[0]
            rhs_set.add(base)

    return lhs_set, rhs_set

def extract_used_lines(outside_text: str) -> str:
    """
    Strip comments and declaration headers from the outside text so that
    `token_used_outside` can perform a simple regex lookup.

    Declarations with initialisers keep only their RHS expressions because they
    behave more like executable logic.
    """
    text = strip_comments(outside_text)

    processed_lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            processed_lines.append('')
            continue
        if re.match(r'^(wire|reg|logic)\b', stripped):
            if '=' in line:
                _, rhs = line.split('=', 1)
                processed_lines.append(rhs)
            else:
                processed_lines.append('')
            continue
        processed_lines.append(line)
    return "\n".join(processed_lines)


def token_used_outside(name: str, used_lines: str) -> bool:
    """
    True if `name` shows up in the pre/post sections outside the extract block.

    The helper assumes `used_lines` has already had comments and declaration
    headers stripped via `extract_used_lines`, so a simple regex is sufficient
    here.
    """
    return re.search(rf'\b{re.escape(name)}\b', used_lines) is not None

# --------------------------------------------------
# Main extraction logic
# --------------------------------------------------

def resolve_width(sig: str, parent_decl: Dict[str, str], port_width: str) -> str:
    """
    Choose the best-known width string for `sig`, preferring parent declarations
    over callee port widths, and defaulting to scalar when nothing matches.
    """
    for candidate in (parent_decl.get(sig, ''), port_width or ''):
        candidate = (candidate or '').strip()
        if candidate:
            return candidate
    return ''

def gen_extracted_module_from_dirs(whole_src: str, search_dirs, new_mod_name: str = "extracted_mod") -> str:
    """
    Generate a new module body from the marked extract block.

    The routine orchestrates the entire analysis pipeline:
      1. locate the block and capture surrounding text
      2. gather parent declarations and assignment usage
      3. parse instantiated modules to infer I/O intent
      4. classify signals into inputs, outputs, or local declarations
      5. render a self-contained module named `new_mod_name`

    Returns:
        SystemVerilog source code for the extracted module.
    """
    pre, block, post = split_with_markers(whole_src)
    outside = pre + post
    parent_decl = parse_parent_decls(whole_src)
    used_lines = extract_used_lines(outside)

    # assign からの読み書き抽出
    lhs_assigned, rhs_used = collect_assign_rw(block)
    assigned: Set[str] = set(lhs_assigned)

    # ブロック内のモジュール一覧
    mods = find_instances(block)

    # 信号毎の集計テーブル
    sig_table: Dict[str, SignalRecord] = {}

    # ① モジュール入出力からの推論
    for mod in mods:
        mod_src = read_module_src(mod, search_dirs)
        port_dir, order = parse_module_ports(mod_src)
        conns = parse_instance_conns(block, mod)

        for port_name in order:
            port_info = port_dir.get(port_name)
            if not port_info:
                continue
            for sig in conns.get(port_name, set()):
                # 幅は 親宣言 > calleeポート
                width = resolve_width(sig, parent_decl, port_info.width)
                record = sig_table.setdefault(sig, SignalRecord())
                if port_info.direction == "input" and sig not in assigned:
                    record.mark_input(width)
                elif port_info.direction == "output":
                    assigned.add(sig)
                    record.update_width(width)
                    record.clear_input()
                    if token_used_outside(sig, used_lines):
                        record.mark_output(width)

    # ② assign からの推論を統合
    # 入力: RHS に現れ、ブロック内で生成されていないもの
    for sig in rhs_used:
        if sig in assigned:
            continue
        width = resolve_width(sig, parent_decl, '')
        record = sig_table.setdefault(sig, SignalRecord())
        record.mark_input(width)

    # 出力: LHS に現れ、ブロック外で使用されているもののみ
    for sig in assigned:
        if token_used_outside(sig, used_lines):
            width = resolve_width(sig, parent_decl, '')
            record = sig_table.setdefault(sig, SignalRecord())
            record.mark_output(width)

    # 最終 I/O 決定（output 優先で衝突解消）
    inputs: List[Tuple[str, str]] = []
    outputs: List[Tuple[str, str]] = []
    for sig, record in sig_table.items():
        width = record.width
        if record.is_output:
            outputs.append((sig, width))
        elif record.is_input:
            inputs.append((sig, width))

    # assign LHS のうちポート化されないものはローカル宣言
    port_names = {n for n, _ in inputs} | {n for n, _ in outputs}
    local_candidates = assigned - port_names
    local_decl = []
    for name in sorted(local_candidates):
        width = ''
        if name in sig_table and sig_table[name].width:
            width = sig_table[name].width
        elif name in parent_decl and parent_decl[name]:
            width = parent_decl[name]
        width = width.strip()
        width_part = f" {width}" if width else ""
        local_decl.append(f"logic{width_part} {name};")

    # 出力 SV 生成（ポートはヘッダに型付きで並べる）
    decls = []
    for n,w in inputs:
        decls.append(f"input {w+' ' if w else ''}{n}")
    for n,w in outputs:
        decls.append(f"output {w+' ' if w else ''}{n}")
    header = f"module {new_mod_name}(\n    " + ",\n    ".join(decls) + "\n);\n"

    body = block.strip("\n")
    parts = [header]
    if local_decl:
        parts.append("    " + "\n    ".join(local_decl) + "\n")
    parts.append(body + ("\n" if not body.endswith("\n") else ""))
    parts.append("endmodule\n")
    return "".join(parts)

# --------------------------------------------------
# CLI
# --------------------------------------------------

def main():
    """CLI entrypoint: parse args, run the extractor, and emit the new module."""
    ap = argparse.ArgumentParser(
        description="Extract a marked block into a new module. "
                    "Search module definitions in given directories."
    )
    ap.add_argument("top",
                    help="Top file (source that contains // @extract-begin / // @extract-end).")
    ap.add_argument("-I", "--moddir", action="append", default=[],
                    help="Module search directory (can be specified multiple times).")
    ap.add_argument("-o", "--output", default="-",
                    help="Output file path (default: '-' for stdout).")
    ap.add_argument("--name", default="extracted_mod",
                    help="New module name (default: extracted_mod)")

    args = ap.parse_args()

    top_path = Path(args.top)
    if not top_path.exists():
        ap.error(f"Top file not found: {top_path}")

    with open(top_path, encoding="utf-8") as f:
        top_src = f.read()

    # モジュール探索ディレクトリが未指定なら、top と同じディレクトリを既定に
    search_dirs = args.moddir[:] if args.moddir else [str(top_path.parent)]

    try:
        out_text = gen_extracted_module_from_dirs(top_src, search_dirs, new_mod_name=args.name)
    except Exception as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        sys.exit(1)

    if args.output == "-" or args.output == "":
        sys.stdout.write(out_text)
    else:
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(out_text)

if __name__ == "__main__":
    main()
