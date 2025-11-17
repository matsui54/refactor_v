#!/usr/bin/env python

"""
ド・モルガンの定理を使って Verilog の assign を簡略化するスクリプト。

例:
    assign p_hoge = ~(~foo & ~bar);
        → assign p_hoge = foo | bar;

    assign x = ~(~a | ~b | ~c);
        → assign x = a & b & c;

制約:
  - 対象は 1 行で完結した assign 文のみ。
  - 右辺が
        ~ ( ~term1 <op> ~term2 <op> ... )
    という形で、<op> が & または | で統一されている場合にだけ変換する。
  - 各 term は `~<単純な式>` とみなせるものに限る。
    ここでは単純に、「演算子 (&,|)の外側で ~ が先頭に付いたトークン」
    として扱う。

使い方:
    python3 demorgan_simplify.py input.v > output.v
    cat input.v | python3 demorgan_simplify.py > output.v
"""

import argparse
import re
from typing import List, Optional, Tuple


# assign LHS = ~( body );
RE_DEMORGAN_CAND = re.compile(
    r'^(?P<indent>\s*)assign\s+'
    r'(?P<lhs>[^=]+?)\s*=\s*'
    r'~\(\s*(?P<body>.+?)\s*\)\s*;'
    r'\s*(?P<comment>//.*)?\s*$'
)


def _split_top_level(body: str) -> Tuple[Optional[str], List[str]]:
    """
    body をトップレベルの & / | で分割する。
    例:
        "~foo & ~bar & ~baz" → ("&", ["~foo", "~bar", "~baz"])
        "~a | ~b"           → ("|", ["~a", "~b"])
    もし & と | が混在していれば op=None, terms=[body] を返す。
    カッコのネストは depth で追跡し、depth==0 の演算子だけを分割対象にする。
    """
    terms: List[str] = []
    cur = []
    op: Optional[str] = None
    depth = 0

    for ch in body:
        if ch == '(':
            depth += 1
            cur.append(ch)
        elif ch == ')':
            depth -= 1
            cur.append(ch)
        elif depth == 0 and ch in ('&', '|'):
            # トップレベルの & or |
            if op is None:
                op = ch
            elif op != ch:
                # & と | が混在している
                return None, [body]
            terms.append(''.join(cur).strip())
            cur = []
        else:
            cur.append(ch)

    if cur:
        terms.append(''.join(cur).strip())

    if op is None:
        # 演算子なし (単項) → 変換対象外とみなす
        return None, [body]

    return op, terms


def _try_demorgan_simplify(body: str) -> Optional[str]:
    """
    body が ~(~a & ~b & ...) あるいは ~(~a | ~b | ...) の中味として
    ド・モルガン簡略化できるなら、簡略化後の RHS 文字列を返す。
    できない場合は None を返す。
    """
    op, terms = _split_top_level(body)
    if op is None:
        return None

    # 各項が ~<expr> になっているかチェック
    simplified_terms: List[str] = []
    for t in terms:
        t = t.strip()
        if not t.startswith('~'):
            return None
        inner = t[1:].strip()
        if not inner:
            return None
        simplified_terms.append(inner)

    # ド・モルガン:
    #  ~(~a & ~b & ...) → a | b | ...
    #  ~(~a | ~b | ...) → a & b & ...
    new_op = '|' if op == '&' else '&'
    return f" {new_op} ".join(simplified_terms)


def transform_lines(lines: List[str]) -> List[str]:
    """
    入力行リストに対して、ド・モルガンで簡略化可能な assign を変換する。
    """
    new_lines = []
    for line in lines:
        m = RE_DEMORGAN_CAND.match(line)
        if not m:
            new_lines.append(line)
            continue

        indent = m.group('indent') or ''
        lhs = m.group('lhs').strip()
        body = m.group('body').strip()
        comment = m.group('comment') or ''

        simplified = _try_demorgan_simplify(body)
        if simplified is None:
            # 変換できない → 元の行をそのまま残す
            new_lines.append(line)
            continue

        # 簡略化成功
        new_line = f"{indent}assign {lhs} = {simplified};"
        if comment:
            # keep two spaces before line comment (matches typical style)
            new_line += f"  {comment}"
        new_line += "\n"
        new_lines.append(new_line)

    return new_lines


def main():
    ap = argparse.ArgumentParser(description="ド・モルガンの定理を使って assign を簡略化するスクリプト")
    ap.add_argument("input", nargs="?", help="入力 Verilog ファイル。省略時はパイプ入力を受け付ける。")
    ap.add_argument("-o", "--output", help="出力ファイル（省略時は stdout）")
    args = ap.parse_args()

    import sys
    stdin_is_tty = sys.stdin.isatty()

    # 入力決定
    if args.input:
        with open(args.input, encoding="utf-8") as f:
            lines = f.readlines()
    else:
        if not stdin_is_tty:
            lines = sys.stdin.readlines()
        else:
            ap.print_usage()
            print("error: no input file, and no piped input", file=sys.stderr)
            sys.exit(1)

    new_lines = transform_lines(lines)
    out_str = "".join(new_lines)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(out_str)
    else:
        sys.stdout.write(out_str)


if __name__ == "__main__":
    main()
