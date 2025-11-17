#!/usr/bin/env python

"""
負論理 m_/正論理 p_ の Verilog を書き換えるスクリプト。

ルール:
1) assign m_hoge[...] = ~(expr); があり、かつ p_hoge が存在しない場合:
   - assign p_hoge[...] = expr; に書き換え（LHS の m_→p_、~削除）
   - 設計中の m_hoge[...] 参照はすべて ~p_hoge[...] に置換
   - 宣言行の m_hoge[...] も p_hoge[...] にリネーム

2) assign m_hoge[...] = ~(expr); と assign p_hoge[...] = ~m_hoge[...]; が両方ある場合:
   - m_hoge[...] の assign 行は削除
   - p_hoge[...] の assign 行は assign p_hoge[...] = expr; に書き換え
   - 設計中の m_hoge[...] 参照はすべて ~p_hoge[...] に置換
   - 宣言行から m_hoge[...] を削除（同じ行に他の信号があれば残す）

複数ビット ( [3:0], [i] ) に対応。

使い方:
    python3 mp_normalize.py input.v > output.v
    # もしくは -o で出力先を指定
"""

import argparse
import re
from typing import Dict, List, Tuple, Set

# assign m_hoge[... ] = ~( ... );
RE_ASSIGN_M = re.compile(
    r'^\s*assign\s+'
    r'(?P<lhs>m_(?P<base>[A-Za-z_]\w*)(?P<idx>\[[^\]]+\])?)\s*=\s*'
    r'~\s*(?P<rhs>.+?)\s*;\s*(?P<comment>//.*)?\s*$'
)

# assign p_hoge[...] = ~m_hoge[...];
RE_ASSIGN_P_FROM_M = re.compile(
    r'^\s*assign\s+'
    r'(?P<lhs>p_(?P<base>[A-Za-z_]\w*)(?P<idx>\[[^\]]+\])?)\s*=\s*'
    r'~\s*(?P<mrhs>m_[A-Za-z_]\w*(?:\[[^\]]+\])?)\s*;\s*(?P<comment>//.*)?\s*$'
)

# p_hoge の存在判定用（ビット指定がついていてもマッチする）
RE_P_NAME = re.compile(r'\b(p_[A-Za-z_]\w*)\b')

# 宣言行 (wire/reg/logic/tri/bit) をざっくり検出
RE_DECL = re.compile(
    r'^(?P<indent>\s*)'
    r'(?P<kw>(?:wire|reg|logic|tri|bit)\b[^;]*?)\s+'
    r'(?P<names>[^;]+);'
    r'\s*(?P<comment>//.*)?\s*$'
)


def strip_outer_parens(expr: str) -> str:
    """Remove redundant surrounding parentheses from an expression."""
    expr = expr.strip()
    while expr.startswith("(") and expr.endswith(")"):
        depth = 0
        balanced = True
        for i, ch in enumerate(expr):
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            if depth == 0:
                if i != len(expr) - 1:
                    balanced = False
                break
        if not balanced or depth != 0:
            break
        expr = expr[1:-1].strip()
    return expr


def collect_info(lines: List[str]):
    """1パス目: m_/p_ の情報を収集する。"""
    # base -> (line_idx, rhs_expr)
    m_assigns: Dict[str, Tuple[int, str]] = {}
    # base -> (line_idx, m_name_in_rhs)
    p_from_m: Dict[str, Tuple[int, str]] = {}
    # p_hoge の base 集合
    existing_p_bases: Set[str] = set()

    for idx, line in enumerate(lines):
        m = RE_ASSIGN_M.match(line)
        if m:
            base = m.group('base')
            rhs = m.group('rhs')
            m_assigns[base] = (idx, rhs)

        m2 = RE_ASSIGN_P_FROM_M.match(line)
        if m2:
            base = m2.group('base')
            mrhs = m2.group('mrhs')
            p_from_m[base] = (idx, mrhs)

        # p_ の存在検出
        for m3 in RE_P_NAME.finditer(line):
            pname = m3.group(1)      # 例: p_hoge
            base = pname[2:]         # "hoge"
            existing_p_bases.add(base)

    return m_assigns, p_from_m, existing_p_bases


def rewrite_declarations(lines: List[str],
                         rename_bases: Set[str],
                         delete_bases: Set[str]) -> List[str]:
    """
    宣言行を書き換える:
      - rename_bases: m_base → p_base にリネーム
      - delete_bases: m_base だけ宣言から削除
    """
    new_lines = list(lines)

    for idx, line in enumerate(new_lines):
        m = RE_DECL.match(line)
        if not m:
            continue

        indent = m.group('indent') or ''
        kw = m.group('kw').rstrip()
        names_part = (m.group('names') or '').strip()
        comment = m.group('comment') or ''

        # Move packed dimensions like "[3:0]" from names_part back into kw.
        while names_part.startswith('['):
            closing = names_part.find(']')
            if closing == -1:
                break
            dim = names_part[:closing + 1]
            kw = f"{kw} {dim}"
            names_part = names_part[closing + 1:].lstrip()

        # "m_hoge, foo, m_bar[3:0]" みたいなのをカンマで割る
        entries = [e.strip() for e in names_part.split(',') if e.strip()]
        new_entries: List[str] = []

        for entry in entries:
            # 各エントリ (名前 +  optional init) をパース
            # 例: "m_hoge", "m_hoge[3:0]", "m_hoge = 1'b0"
            em = re.match(
                r'(?P<name>(?:m_|p_)?[A-Za-z_]\w*(?:\[[^\]]+\])?)'
                r'\s*(?P<init>=\s*.+)?$',
                entry
            )
            if not em:
                # よくわからない形はそのまま残す
                new_entries.append(entry)
                continue

            name = em.group('name')          # 例: m_hoge[3:0]
            init = em.group('init') or ''    # 例: "= 1'b0"

            if not name.startswith('m_'):
                # m_ 以外はそのまま
                new_entries.append(entry)
                continue

            # name = "m_hoge[3:0]" から base="hoge" を取り出す
            name_core = name[2:]  # "hoge[3:0]"
            base = name_core.split('[', 1)[0]

            if base in rename_bases:
                # m_hoge → p_hoge にリネーム
                idx_part = ''
                if '[' in name_core:
                    idx_part = name_core[name_core.index('['):]  # "[3:0]" など
                new_name = f"p_{base}{idx_part}"
                new_entries.append(new_name + (f" {init}" if init else ''))
            elif base in delete_bases:
                # 宣言から m_hoge を削除（何もしない＝追加しない）
                continue
            else:
                # 対象外の m_ は触らない
                new_entries.append(entry)

        if not new_entries:
            # 全部消えたらこの宣言行自体を削除
            new_lines[idx] = ""
        else:
            names_str = ", ".join(new_entries)
            new_line = f"{indent}{kw} {names_str};"
            if comment:
                new_line += f" {comment}"
            new_line += "\n"
            new_lines[idx] = new_line

    return new_lines


def transform(lines: List[str]) -> List[str]:
    m_assigns, p_from_m, existing_p_bases = collect_info(lines)

    # case2 対象: m_assign と p_from_m の両方を持つ base
    pair_bases = set(m_assigns.keys()) & set(p_from_m.keys())

    # case1 で「m_ から p_ に生まれ変わる base」
    rename_bases: Set[str] = set()

    new_lines = list(lines)

    # --- case1: m_assign のうち pair_bases でない & p がまだ存在しないもの ---
    for base, (idx, rhs) in m_assigns.items():
        if base in pair_bases:
            continue  # case2 で処理する

        if base in existing_p_bases:
            # すでに p_hoge があるなら何もしない
            continue

        line = lines[idx]
        m = RE_ASSIGN_M.match(line)
        if not m:
            continue

        comment = m.group('comment') or ''
        indent = line[:line.index('assign')] if 'assign' in line else ''
        idx_part = m.group('idx') or ''  # "[3:0]" など

        new_lhs = f"p_{base}{idx_part}"
        new_rhs = strip_outer_parens(rhs)
        new_line = f"{indent}assign {new_lhs} = {new_rhs};"
        if comment:
            new_line += f" {comment}"
        new_line += "\n"
        new_lines[idx] = new_line

        rename_bases.add(base)

    # --- case2: m & p のペアがある base ---
    for base in pair_bases:
        m_idx, m_rhs = m_assigns[base]
        p_idx, m_name_in_p = p_from_m[base]

        # m_idx 行を削除
        new_lines[m_idx] = ""

        # p_idx 行を書き換え
        line_p = lines[p_idx]
        m2 = RE_ASSIGN_P_FROM_M.match(line_p)
        if not m2:
            continue

        comment = m2.group('comment') or ''
        indent = line_p[:line_p.index('assign')] if 'assign' in line_p else ''
        idx_part = m2.group('idx') or ''

        new_lhs = f"p_{base}{idx_part}"
        new_rhs = strip_outer_parens(m_rhs)  # m_hoge[...] = ~(m_rhs) → p_hoge[...] = m_rhs;
        new_line = f"{indent}assign {new_lhs} = {new_rhs};"
        if comment:
            new_line += f" {comment}"
        new_line += "\n"
        new_lines[p_idx] = new_line

    # --- 宣言行の書き換え ---
    # rename_bases: m_base → p_base にリネーム
    # pair_bases: m_base を削除
    new_lines = rewrite_declarations(new_lines,
                                     rename_bases=rename_bases,
                                     delete_bases=pair_bases)

    # --- m_hoge[...] 参照を ~p_hoge[...] に置換 ---
    # 対象 base: rename_bases（case1） + pair_bases（case2）
    elim_bases = rename_bases | pair_bases

    for base in elim_bases:
        # m_hoge[...]
        pattern = re.compile(
            rf'\b(m_{re.escape(base)})(\[[^\]]+\])?'
        )

        def repl(match: re.Match) -> str:
            idx_part = match.group(2) or ''
            return f"~p_{base}{idx_part}"

        for i, line in enumerate(new_lines):
            if not line:
                continue
            new_lines[i] = pattern.sub(repl, line)

    return new_lines


def main():
    ap = argparse.ArgumentParser(description="m_/p_ 負論理→正論理変換スクリプト")
    ap.add_argument("input", help="入力 Verilog ファイル ('-' で stdin)", nargs='?', default='-')
    ap.add_argument("-o", "--output", help="出力ファイル（省略時は stdout）")
    args = ap.parse_args()

    if args.input == '-' or args.input is None:
        import sys
        lines = sys.stdin.readlines()
    else:
        with open(args.input, encoding="utf-8") as f:
            lines = f.readlines()

    new_lines = transform(lines)

    out_str = "".join(new_lines)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(out_str)
    else:
        import sys
        sys.stdout.write(out_str)


if __name__ == "__main__":
    main()
