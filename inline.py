#!/usr/bin/env python3
import re
import sys
import argparse
from pathlib import Path

# ===== マーカー（必要なら CLI で変更可能） =====
DEFAULT_BEGIN = r'// @inline-begin'
DEFAULT_END   = r'// @inline-end'

# ===== 共有ユーティリティ（extract.py と揃える） =====

def read_module_src(mod_name, search_dirs):
    """search_dirs 内から mod_name.(sv|v) を探索して読み込む。曖昧ならエラー。"""
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
    uniq = sorted(set(map(str, found)))
    if len(uniq) > 1:
        raise Exception(f"Multiple module files for '{mod_name}' found: {', '.join(uniq)}")
    with open(uniq[0], encoding="utf-8") as f:
        return f.read()

def split_with_markers(src: str, begin_pat: str, end_pat: str):
    m1 = re.search(begin_pat, src)
    m2 = re.search(end_pat, src)
    if not m1 or not m2 or m1.end() > m2.start():
        raise ValueError("inline markers not found or malformed.")
    pre  = src[:m1.start()]
    block= src[m1.end():m2.start()]
    post = src[m2.end():]
    return pre, block, post

def parse_parent_decls(src: str):
    """統合先ファイル中の logic|wire|reg 宣言名 → 幅 の辞書を取る（衝突判定にも利用）"""
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
    """モジュール定義から {port: (dir,width)} と順序を抽出（1行1宣言前提）"""
    port_dir, order = {}, []
    for m in re.finditer(r'^\s*(input|output)\s*(\[[^\]]+\])?\s*([A-Za-z_]\w*)\s*;\s*$',
                         src, flags=re.M):
        d, w, n = m.groups()
        port_dir[n] = (d, w or '')
        order.append(n)
    return port_dir, order

def parse_instance_conns_expr(block_src: str, mod_name: str):
    """
    マーカー内から mod_name のインスタンスを探し、.Port(expr) を {Port: expr} で返す。
    最初に見つかったインスタンスを対象にする。
    """
    inst_re = re.compile(
        rf'{mod_name}\s+[A-Za-z_]\w*\s*\(\s*(?P<body>.*?)\s*\)\s*;',
        re.S
    )
    m = inst_re.search(block_src)
    if not m:
        return None, None
    body = m.group('body')
    conns = {}
    for p in re.finditer(r'\.\s*([A-Za-z_]\w*)\s*\(\s*([^)]+?)\s*\)', body):
        port, expr = p.groups()
        # コメント除去
        expr = re.sub(r'/\*.*?\*/', '', expr, flags=re.S)
        expr = re.sub(r'//.*', '', expr)
        conns[port] = expr.strip()
    return conns, m.span()  # 接続辞書と、インスタンスの本文 span（置換に使う場合に備え返す）

def extract_module_body(src: str, mod_name: str):
    """
    module mod_name (...) ... endmodule から、本体（ポート宣言を除く）を抽出。
    戻り値: (body_text, port_dir, port_order, internal_declared_names)
    """
    # module 本体全体
    mod_re = re.compile(
        rf'module\s+{mod_name}\s*\(.*?\);\s*(?P<body>.*?)\s*endmodule',
        re.S
    )
    m = mod_re.search(src)
    if not m:
        raise ValueError(f"module '{mod_name}' not found in its file")
    whole_body = m.group('body')

    # ポート宣言行を除いた内側本体
    lines = whole_body.splitlines()
    body_lines = []
    port_dir, port_order = parse_module_ports(whole_body)  # 1行1宣言が前提
    port_names = set(port_dir.keys())

    # 内部宣言名（衝突検出用）
    internal_names = set()

    decl_line_re = re.compile(r'^\s*(wire|reg|logic)\b.*?;\s*$')
    # 識別子列抽出（宣言行の右半分から名前だけ拾う）
    name_token_re = re.compile(r'[A-Za-z_]\w*')

    for ln in lines:
        if re.match(r'^\s*(input|output)\b', ln):  # ポート宣言は削除
            continue
        body_lines.append(ln)

        # 内部宣言の収集（wire/logic/reg）
        if decl_line_re.match(ln):
            # 先頭の型などを削る
            # 例: logic signed [3:0] a, b, c;
            rhs = ln.split(';', 1)[0]
            # カッコ内・幅などをざっくり除去してから名前を拾う
            # （厳密にするなら AST を推奨）
            rhs = re.sub(r'^\s*(wire|reg|logic)\b.*?\s', ' ', rhs)
            # 配列添字や初期化子を粗く殺す
            rhs = re.sub(r'\[[^\]]+\]', ' ', rhs)
            rhs = re.sub(r'=[^,]+', ' ', rhs)
            for tok in name_token_re.findall(rhs):
                if tok and tok not in port_names:
                    internal_names.add(tok)

    body_text = "\n".join(body_lines).strip("\n")
    return body_text, port_dir, port_order, internal_names

def replace_ports_with_expr(body_text: str, port_to_expr: dict):
    """
    本体テキスト中のポート識別子を (expr) で置換（単語境界一致）。
    コメントも置換対象に含まれる（簡易実装）。必要なら強化可能。
    """
    out = body_text
    # 長い識別子から置換すると誤置換を減らせる
    for port in sorted(port_to_expr.keys(), key=len, reverse=True):
        expr = port_to_expr[port]
        out = re.sub(rf'\b{re.escape(port)}\b', f'({expr})', out)
    return out

# ===== インライン実装 =====

def inline_module(whole_src: str, mod_name: str, search_dirs, begin_pat: str, end_pat: str):
    # 1) マーカーで分割
    pre, block, post = split_with_markers(whole_src, begin_pat, end_pat)

    # 2) マーカー内から対象モジュールのインスタンスを抽出（.Port(expr)）
    conns, _span = parse_instance_conns_expr(block, mod_name)
    if conns is None:
        raise ValueError(f"Instance of '{mod_name}' not found between markers.")

    # 3) モジュール定義をロードし、本体とポート一覧、内部宣言名を取得
    mod_src = read_module_src(mod_name, search_dirs)
    body_text, port_dir, port_order, internal_names = extract_module_body(mod_src, mod_name)

    # 4) 衝突検出：内部宣言名 vs 統合先の既存宣言名
    outside_text = pre + post
    parent_decl = parse_parent_decls(outside_text)  # 既存の宣言名
    collisions = sorted(n for n in internal_names if n in parent_decl)

    if collisions:
        # 要求仕様：衝突名をすべて標準出力に出して終了（コピーはしない）
        print("\n".join(collisions))
        return None  # 呼び出し元で非0終了にする

    # 5) ポート置換マップ（存在しないポートは無視）
    port_to_expr = {p: conns[p] for p in port_order if p in conns}

    # 6) 本体テキスト内のポート識別子を、インスタンスの expr で置換
    inlined_body = replace_ports_with_expr(body_text, port_to_expr)

    # 7) 組み立て：pre + インライン化テキスト + post
    #    マーカーは残さず、インライン済みコードを挿入
    new_src = pre + "\n" + inlined_body + "\n" + post
    return new_src

# ===== CLI =====

def main():
    ap = argparse.ArgumentParser(
        description="Inline a module instance: copy the module body into the marked region, "
                    "replace ports with instance expressions, and abort if local names collide."
    )
    ap.add_argument("top", help="Target file that contains the inline markers.")
    ap.add_argument("--module", "-m", required=True, help="Module name to inline (e.g., foo).")
    ap.add_argument("-I", "--moddir", action="append", default=[],
                    help="Module search directory (can be specified multiple times).")
    ap.add_argument("-o", "--output", default="-", help="Output file (default: stdout).")
    ap.add_argument("--begin", default=DEFAULT_BEGIN,
                    help=f"Begin marker regex (default: {DEFAULT_BEGIN!r})")
    ap.add_argument("--end", default=DEFAULT_END,
                    help=f"End marker regex (default: {DEFAULT_END!r})")

    args = ap.parse_args()
    top_path = Path(args.top)
    if not top_path.exists():
        ap.error(f"Top file not found: {top_path}")

    with open(top_path, encoding="utf-8") as f:
        top_src = f.read()

    search_dirs = args.moddir[:] if args.moddir else [str(top_path.parent)]

    new_src = inline_module(top_src, args.module, search_dirs, args.begin, args.end)
    if new_src is None:
        # 衝突あり：仕様により「標準出力に衝突名を出した上で失敗終了」
        sys.exit(2)

    if args.output == "-" or args.output == "":
        sys.stdout.write(new_src)
    else:
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(new_src, encoding="utf-8")

if __name__ == "__main__":
    main()
