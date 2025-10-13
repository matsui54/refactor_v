#!/usr/bin/env python3
import re
import sys
import argparse
from pathlib import Path

BEGIN = r'// @extract-begin'
END   = r'// @extract-end'

# --------------------------------------------------
# Utility
# --------------------------------------------------

def read_module_src(mod_name, search_dirs):
    """
    search_dirs 内から mod_name.(sv|v) を探索して読み込む。
    - 複数のディレクトリを順に探索
    - 同名ファイルが複数の場所にあればエラー（曖昧性防止）
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
    m1 = re.search(BEGIN, src)
    m2 = re.search(END, src)
    if not m1 or not m2 or m1.end() > m2.start():
        raise ValueError("extract markers not found or malformed.")
    pre  = src[:m1.start()]
    block= src[m1.end():m2.start()]
    post = src[m2.end():]
    return pre, block, post

def parse_parent_decls(src: str):
    """親ファイル中の logic|wire|reg 宣言を抽出（幅辞書を作成）"""
    decls = {}
    decl_re = re.compile(
        r'^\s*(wire|reg|logic)\b(?:\s+signed\b)?\s*(\[[^\]]+\])?\s+([^;]+);\s*$',
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

def parse_module_ports(src: str):
    """モジュール定義から {port: (dir,width)} を抽出（1行1宣言前提）"""
    port_dir, order = {}, []
    for m in re.finditer(r'^\s*(input|output)\s*(\[[^\]]+\])?\s*([A-Za-z_]\w*)\s*;\s*$',
                         src, flags=re.M):
        d, w, n = m.groups()
        port_dir[n] = (d, w or '')
        order.append(n)
    return port_dir, order

def find_instances(block_src: str):
    """ブロック内に現れるモジュールインスタンスのモジュール名集合"""
    mods = set()
    for m in re.finditer(r'^\s*([A-Za-z_]\w*)\s+[A-Za-z_]\w*\s*\(', block_src, flags=re.M):
        mods.add(m.group(1))
    return mods

def parse_instance_conns(block_src: str, mod_name: str):
    """
    インスタンスの .Port(expr) を {Port: set(base_signals)} に変換。
    expr は識別子単体、スライス、インデックス付き、単純演算を許容。
    例:
      .AAA(aaa[3:2])
      .BBB(bbb)
      .CCC(ccc_bit2)
      .DDD(a & b)
    """
    out = {}
    inst_re = re.compile(
        rf'{mod_name}\s+[A-Za-z_]\w*\s*\(\s*(?P<body>.*?)\s*\)\s*;',
        re.S
    )
    for im in inst_re.finditer(block_src):
        body = im.group('body')
        # .Port(expr) を順に抽出
        for p in re.finditer(r'\.\s*([A-Za-z_]\w*)\s*\(\s*([^)]+?)\s*\)', body):
            port, expr = p.groups()
            # コメントを除去
            expr = re.sub(r'/\*.*?\*/', '', expr, flags=re.S)
            expr = re.sub(r'//.*', '', expr)

            # 信号候補を抽出（識別子ベース部を取得）
            sigs = set()
            for token in re.findall(r'[A-Za-z_]\w*(?:\[[^\]]+\])?', expr):
                base = token.split('[', 1)[0]  # ビットスライス削除
                sigs.add(base)
            if sigs:
                out.setdefault(port, set()).update(sigs)
    return out

def collect_assign_rw(block_src: str):
    """
    assign 文から LHS 集合・RHS 集合（ベース名）を抽出する。
    LHS: assign LHS = ...
    RHS: 右辺に現れる識別子（スライス/添字付きはベース名に還元）
    """
    lhs_set, rhs_set = set(), set()
    # コメント除去
    text = re.sub(r'/\*.*?\*/', '', block_src, flags=re.S)
    text = re.sub(r'//.*', '', text)

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

def token_used_outside(name: str, outside_text: str) -> bool:
    """
    outside_text で name が使われているかを判定。
    以下はカウントしない：
      - コメント中（//, /* ... */）
      - 宣言行の宣言部分 (wire|reg|logic)
        - ただし宣言に初期化が付いている場合は RHS 内の使用は残す
    """
    # コメント削除
    text = re.sub(r'/\*.*?\*/', '', outside_text, flags=re.S)
    text = re.sub(r'//.*', '', text)

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
    text = "\n".join(processed_lines)

    return re.search(rf'\b{re.escape(name)}\b', text) is not None

# --------------------------------------------------
# Main extraction logic
# --------------------------------------------------

def resolve_width(sig, parent_decl, port_width):
    if sig in parent_decl and parent_decl[sig]:
        return parent_decl[sig]
    if port_width:
        return port_width
    return ''

def gen_extracted_module_from_dirs(whole_src, search_dirs, new_mod_name="extracted_mod"):
    pre, block, post = split_with_markers(whole_src)
    outside = pre + post
    parent_decl = parse_parent_decls(whole_src)

    # assign からの読み書き抽出
    lhs_assigned, rhs_used = collect_assign_rw(block)
    assigned = lhs_assigned

    # ブロック内のモジュール一覧
    mods = find_instances(block)
    produced_by_module = set()

    # 信号毎の集計テーブル: rec = {"in":bool, "out":bool, "width":str}
    sig_table = {}

    # ① モジュール入出力からの推論
    for mod in mods:
        mod_src = read_module_src(mod, search_dirs)
        port_dir, order = parse_module_ports(mod_src)
        conns = parse_instance_conns(block, mod)

        for p in order:
            if p not in port_dir:
                continue
            direction, pw = port_dir[p]
            for sig in conns.get(p, []):
                # 幅は 親宣言 > calleeポート
                width = resolve_width(sig, parent_decl, pw)
                rec = sig_table.setdefault(sig, {"in": False, "out": False, "width": width})
                if direction == "input" and sig not in assigned:
                    rec["in"] = True
                elif direction == "output":
                    produced_by_module.add(sig)
                    if not rec["width"]:
                        rec["width"] = width
                    if token_used_outside(sig, outside):
                        rec["out"] = True

    # ② assign からの推論を統合
    # 入力: RHS に現れ、ブロック内で生成されていないもの
    for sig in rhs_used:
        if sig in assigned:
            continue
        width = resolve_width(sig, parent_decl, '')
        rec = sig_table.setdefault(sig, {"in": False, "out": False, "width": width})
        rec["in"] = True
        if not rec["width"]:
            rec["width"] = width

    # 出力: LHS に現れ、ブロック外で使用されているもののみ
    for sig in assigned:
        if token_used_outside(sig, outside):
            width = resolve_width(sig, parent_decl, '')
            rec = sig_table.setdefault(sig, {"in": False, "out": False, "width": width})
            rec["out"] = True
            if not rec["width"]:
                rec["width"] = width

    # 最終 I/O 決定（output 優先で衝突解消）
    inputs, outputs = [], []
    for sig, rec in sig_table.items():
        w = rec["width"]
        if rec["out"]:
            outputs.append((sig, w))
        elif rec["in"]:
            inputs.append((sig, w))

    # assign LHS のうちポート化されないものはローカル宣言
    port_names = {n for n,_ in inputs} | {n for n,_ in outputs}
    local_candidates = (assigned | produced_by_module) - port_names
    local_decl = []
    for name in sorted(local_candidates):
        width = ''
        if name in sig_table and sig_table[name]["width"]:
            width = sig_table[name]["width"]
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

    body = block.strip("\n") + "\n"
    return (
        header
        + ("    " + "\n    ".join(local_decl) + "\n" if local_decl else "")
        + "  " + body.replace("\n", "\n  ")
        + "endmodule\n"
    )

# --------------------------------------------------
# CLI
# --------------------------------------------------

def main():
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
