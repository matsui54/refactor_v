#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
verilog_port_rename.py

短いポート名を、モジュール内部で使われている長い信号名（assignで直結されているもの）に
置き換えるスクリプト。

・moduleを含むファイルを解析して、ポート名→内部信号名の対応を構築
・安全に 1:1 対応していることをチェック（ビット幅・ビット位置・部分代入の矛盾を検出）
・安全と判断できれば、モジュールのポートを長い名前に差し替える
・2つ目のファイル中のインスタンスのポート名も同様に置き換える
"""

import argparse
import re
import sys
from collections import defaultdict
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple, Set


@dataclass
class PortInfo:
    direction: str  # 'input' / 'output' / 'inout'
    msb: Optional[int]
    lsb: Optional[int]


MODULE_RE = re.compile(
    r'module\s+(\w+)\s*\((.*?)\);\s*(.*?)endmodule',
    re.S
)

ASSIGN_RE = re.compile(
    r'^\s*assign\s+([^=]+?)\s*=\s*([^;]+?)\s*;',
    re.M
)

# 1 行の assign を判定する用（body から行単位で削除するときに使う）
ASSIGN_LINE_RE = re.compile(
    r'^\s*assign\s+([^=]+?)\s*=\s*([^;]+?)\s*;\s*(?://.*)?$'
)


def parse_module(text: str):
    m = MODULE_RE.search(text)
    if not m:
        raise SystemExit("Error: module が見つかりませんでした。")
    name = m.group(1)
    header_ports_str = m.group(2)
    body = m.group(3)
    pre = text[:m.start()]
    post = text[m.end():]

    # ヘッダのポート名一覧
    ports: List[str] = []
    for part in header_ports_str.split(','):
        name_part = part.split('//')[0].strip()
        if not name_part:
            continue
        # ANSI っぽい書き方の場合もあるので最後の識別子を取る
        tokens = re.findall(r'[A-Za-z_]\w*', name_part)
        if not tokens:
            continue
        ports.append(tokens[-1])

    return name, ports, body, pre, post


def parse_port_declarations(body: str, header_ports: List[str]) -> Dict[str, PortInfo]:
    """
    非 ANSI スタイルの input/output/inout 宣言をざっくりパースする。
    一行に複数シグナルがある場合も対応。
    """
    port_info: Dict[str, PortInfo] = {}
    # header に出てくるポートだけ対象
    header_set = set(header_ports)

    # 行単位で input/output/inout を探す
    decl_re = re.compile(r'^\s*(input|output|inout)\b([^;]*);', re.M)
    for m in decl_re.finditer(body):
        direction = m.group(1)
        rest = m.group(2)

        # 幅 [msb:lsb] を一つだけ拾う（複数あっても最初のだけ）
        range_m = re.search(r'\[(\d+)\s*:\s*(\d+)\]', rest)
        msb = lsb = None
        if range_m:
            msb = int(range_m.group(1))
            lsb = int(range_m.group(2))
            # 名前から幅の表記を消しておく
            rest = rest[:range_m.start()] + rest[range_m.end():]

        # 残りから名前を抽出（カンマ区切り）
        names = [n.strip() for n in rest.split(',') if n.strip()]
        for name in names:
            # 余分な単語（wire, reg, logic など）を排除して最後の識別子を採用
            tokens = re.findall(r'[A-Za-z_]\w*', name)
            if not tokens:
                continue
            ident = tokens[-1]
            if ident in header_set:
                port_info[ident] = PortInfo(direction=direction, msb=msb, lsb=lsb)

    # header にあるのに宣言が見つからないものはエラー
    missing = [p for p in header_ports if p not in port_info]
    if missing:
        raise SystemExit(
            "Error: 次のポートの宣言が見つかりませんでした: " + ", ".join(missing)
        )
    return port_info


def parse_signal_ref(expr: str):
    """
    'name', 'name[idx]', 'name[msb:lsb]' をパースして
    (name, msb, lsb) を返す。ビット指定が無い場合は (name, None, None)。
    それ以外の複雑な式は None を返す。
    """
    expr = expr.strip()
    m = re.match(r'^([A-Za-z_]\w*)\s*(\[[^]]+\])?$', expr)
    if not m:
        return None
    name = m.group(1)
    bracket = m.group(2)
    if not bracket:
        return name, None, None
    inside = bracket[1:-1].strip()
    m2 = re.match(r'^(\d+)\s*:\s*(\d+)$', inside)
    if m2:
        msb = int(m2.group(1))
        lsb = int(m2.group(2))
        return name, msb, lsb
    m3 = re.match(r'^(\d+)$', inside)
    if m3:
        idx = int(m3.group(1))
        return name, idx, idx
    # その他は対象外
    return None


def analyse_assigns(
    body: str,
    header_ports: List[str],
    port_info: Dict[str, PortInfo],
):
    """
    assign 文からポートと内部信号の対応を構築し、矛盾がないかチェックする。
    戻り値:
        port_to_internal: ポート名 -> 内部信号名
        internal_to_ports: 内部信号名 -> {ポート名,...}
        port_segments: ポート名 -> List[ (msb, lsb) ]  (インデックス無し assign は None を格納)
    """
    ports_set: Set[str] = set(header_ports)
    port_to_internal: Dict[str, str] = {}
    internal_to_ports: Dict[str, Set[str]] = defaultdict(set)
    port_segments: Dict[str, List[Optional[Tuple[int, int]]]] = defaultdict(list)

    problem_signals: Set[str] = set()
    other_errors: List[str] = []

    for m in ASSIGN_RE.finditer(body):
        lhs_raw, rhs_raw = m.group(1), m.group(2)
        lhs = parse_signal_ref(lhs_raw)
        rhs = parse_signal_ref(rhs_raw)
        if lhs is None or rhs is None:
            continue  # 複雑な式なのでスキップ

        lhs_name, lhs_msb, lhs_lsb = lhs
        rhs_name, rhs_msb, rhs_lsb = rhs

        lhs_is_port = lhs_name in ports_set
        rhs_is_port = rhs_name in ports_set

        # 両方ポート or 両方内部信号は無視
        if lhs_is_port == rhs_is_port:
            continue

        if lhs_is_port:
            port_name = lhs_name
            p_msb, p_lsb = lhs_msb, lhs_lsb
            internal_name = rhs_name
            i_msb, i_lsb = rhs_msb, rhs_lsb
        else:
            port_name = rhs_name
            p_msb, p_lsb = rhs_msb, rhs_lsb
            internal_name = lhs_name
            i_msb, i_lsb = lhs_msb, lhs_lsb

        # 幅チェック
        # どちらもビット指定が無ければ「全ビット」を一括とみなす
        if p_msb is not None or i_msb is not None:
            if (p_msb is None) != (i_msb is None):
                problem_signals.add(internal_name)
                other_errors.append(
                    f"assign のビット指定が片側だけです: {lhs_raw} = {rhs_raw}"
                )
                continue
            # 幅が違う
            w_port = abs(p_msb - p_lsb) + 1
            w_int = abs(i_msb - i_lsb) + 1
            if w_port != w_int:
                problem_signals.add(internal_name)
                other_errors.append(
                    f"assign のビット幅が一致しません: {lhs_raw} = {rhs_raw}"
                )
                continue
            # ビット位置の対応が違う（例: a[1:0] = ABC[5:4]）
            if p_msb != i_msb or p_lsb != i_lsb:
                problem_signals.add(internal_name)
                other_errors.append(
                    f"ビット位置の対応が一致しません: {lhs_raw} = {rhs_raw}"
                )
                continue

        # port -> internal の 1:1 対応チェック
        prev = port_to_internal.get(port_name)
        if prev is None:
            port_to_internal[port_name] = internal_name
        elif prev != internal_name:
            problem_signals.add(internal_name)
            problem_signals.add(prev)
            other_errors.append(
                f"ポート {port_name} が複数の内部信号 ({prev}, {internal_name}) に接続されています"
            )
            continue

        # internal -> ports の追跡（1:多 も検出する）
        internal_to_ports[internal_name].add(port_name)

        # ポートのカバレッジ用にセグメントを記録
        if p_msb is None:
            # インデックス指定無し = 全ビットとみなす（後で特別扱い）
            port_segments[port_name].append(None)
        else:
            hi, lo = p_msb, p_lsb
            port_segments[port_name].append((hi, lo))

    # internal が複数のポートに分割されている場合を検出
    for internal_name, ports in internal_to_ports.items():
        if len(ports) > 1:
            problem_signals.add(internal_name)
            other_errors.append(
                f"内部信号 {internal_name} が複数のポートに分割接続されています: "
                + ", ".join(sorted(ports))
            )

    # 各ポートが全部覆われているかを確認
    for port_name in ports_set:
        if port_name not in port_to_internal:
            other_errors.append(f"ポート {port_name} に対応する assign が見つかりません。")
            continue

        info = port_info.get(port_name)
        if not info:
            other_errors.append(f"ポート {port_name} の宣言情報が不足しています。")
            continue

        segs = port_segments.get(port_name, [])
        if not segs:
            other_errors.append(f"ポート {port_name} に対応する assign が見つかりません。")
            continue

        # インデックス無し assign が一つでもあれば「全ビット割り当て」とみなす
        if any(s is None for s in segs):
            continue

        if info.msb is None or info.lsb is None:
            # ビット幅が分からないので細かいチェックはしない
            continue

        indexed_segs = [s for s in segs if s is not None]
        if not indexed_segs:
            # 念のため
            continue

        bits_needed = set(range(min(info.msb, info.lsb), max(info.msb, info.lsb) + 1))
        bits_mapped: Set[int] = set()
        for hi, lo in indexed_segs:
            for b in range(min(hi, lo), max(hi, lo) + 1):
                bits_mapped.add(b)

        # 単一セグメントの場合は従来通り「全ビットカバー」を要求し、
        # 複数セグメントで一部が未使用なケースは許容する。
        if len(indexed_segs) == 1 and bits_mapped != bits_needed:
            other_errors.append(
                f"ポート {port_name} のビットが assign で全てカバーされていません。"
            )
        elif bits_mapped - bits_needed:
            # assign 側で宣言幅を越えている（ありえないが保険）
            other_errors.append(
                f"ポート {port_name} の assign が宣言範囲外のビットを参照しています。"
            )

    if problem_signals or other_errors:
        if problem_signals:
            sys.stderr.write(
                "Error: 安全に置き換えできない内部信号があります。"
                "以下の信号について、ポート側の名前を手動で整えてから再実行してください:\n"
            )
            for s in sorted(problem_signals):
                sys.stderr.write(f"  - {s}\n")
        if other_errors:
            sys.stderr.write("詳細:\n")
            for e in other_errors:
                sys.stderr.write("  * " + e + "\n")
        raise SystemExit(1)

    return port_to_internal, internal_to_ports, port_segments


def build_new_module_text(
    module_name: str,
    header_ports: List[str],
    body: str,
    pre: str,
    post: str,
    port_info: Dict[str, PortInfo],
    port_to_internal: Dict[str, str],
    style: str,
) -> str:
    """
    新しいポート名（内部信号名）に基づいて module テキストを組み立てる。
    style: 'ansi' or 'non-ansi'
    """
    # 新しいポートリスト（順序は元の header に合わせる）
    new_ports: List[Tuple[str, PortInfo]] = []
    used_internal: Set[str] = set()
    for old_name in header_ports:
        internal = port_to_internal.get(old_name)
        if internal is None:
            raise SystemExit(f"内部エラー: ポート {old_name} の対応が見つかりません。")
        info = port_info[old_name]
        if internal in used_internal:
            raise SystemExit(
                f"内部信号 {internal} が複数のポートに対応してしまいました。（検査漏れ）"
            )
        used_internal.add(internal)
        new_ports.append((internal, info))

    # body から元の input/output/inout 宣言と、ポート ↔ 内部信号の単純 assign を削除
    new_body_lines: List[str] = []
    ports_set = set(header_ports)
    for line in body.splitlines():
        stripped = line.lstrip()
        if stripped.startswith(("input ", "output ", "inout ")):
            # 既存のポート宣言は全部削除して作り直す
            continue

        m = ASSIGN_LINE_RE.match(line)
        if m:
            lhs_raw, rhs_raw = m.group(1), m.group(2)
            lhs = parse_signal_ref(lhs_raw)
            rhs = parse_signal_ref(rhs_raw)
            if lhs is not None and rhs is not None:
                lhs_name, _, _ = lhs
                rhs_name, _, _ = rhs
                lhs_is_port = lhs_name in ports_set
                rhs_is_port = rhs_name in ports_set
                if lhs_is_port != rhs_is_port:
                    # ポートと内部信号の単純な橋渡し assign なので削除
                    continue

        new_body_lines.append(line)

    new_body = "\n".join(new_body_lines).rstrip() + "\n"

    # 新しい module ヘッダとポート宣言を生成
    if style == "ansi":
        port_lines = []
        for name, info in new_ports:
            rng = ""
            if info.msb is not None and info.lsb is not None:
                rng = f" [{info.msb}:{info.lsb}]"
            port_lines.append(f"  {info.direction}{rng} {name}")
        header = f"module {module_name} (\n" + ",\n".join(port_lines) + "\n);\n"
        module_text = header + new_body + "endmodule\n"
    else:  # non-ansi
        # ヘッダは名前のみ
        header_names = [f"  {name}" for name, _ in new_ports]
        header = f"module {module_name} (\n" + ",\n".join(header_names) + "\n);\n"
        # ボディ先頭にポート宣言を追加
        decl_lines = []
        for name, info in new_ports:
            rng = ""
            if info.msb is not None and info.lsb is not None:
                rng = f" [{info.msb}:{info.lsb}]"
            decl_lines.append(f"  {info.direction}{rng} {name};")
        decl_block = "\n".join(decl_lines) + "\n\n"
        module_text = header + decl_block + new_body + "endmodule\n"

    return pre + module_text + post


def rewrite_instantiations(
    text: str,
    module_name: str,
    port_to_internal: Dict[str, str],
) -> str:
    """
    ファイル2中の module_name のインスタンスについて、named port 接続のポート名を
    port_to_internal に基づいて書き換える。
    """
    port_map = port_to_internal

    inst_re = re.compile(
        rf'(?P<full>'
        rf'(?P<mod>{module_name})'
        rf'\s*'
        rf'(?P<params>#\s*\([^;]*?\)\s*)?'
        rf'\s+'
        rf'(?P<inst>\w+)'
        rf'\s*\('
        rf'(?P<ports>[^;]*?)'
        rf'\);)',
        re.S,
    )

    def repl(m: re.Match) -> str:
        full = m.group("full")
        ports_str = m.group("ports")

        def repl_port(pm: re.Match) -> str:
            pname = pm.group(1)
            new_name = port_map.get(pname, pname)
            return f".{new_name}("

        new_ports = re.sub(r'\.(\w+)\s*\(', repl_port, ports_str)

        # full の中で ports 部分だけ差し替える
        start_ports = m.start("ports") - m.start("full")
        end_ports = m.end("ports") - m.start("full")
        prefix = full[:start_ports]
        suffix = full[end_ports:]
        return prefix + new_ports + suffix

    new_text = inst_re.sub(repl, text)
    return new_text


def main():
    parser = argparse.ArgumentParser(
        description="Verilog モジュールの短いポート名を内部の長い信号名に置き換えるスクリプト"
    )
    parser.add_argument(
        "module_file",
        nargs="?",
        help="ポートを置き換えるモジュールがある Verilog ファイル。省略時は標準入力を使用。",
    )
    parser.add_argument(
        "inst_file",
        help="変換対象モジュールのインスタンスがある Verilog ファイル。",
    )
    parser.add_argument(
        "--style",
        choices=["ansi", "non-ansi"],
        default="non-ansi",
        help="出力するポート宣言のスタイル（デフォルト: non-ansi）",
    )
    args = parser.parse_args()

    # module_file の読み込み
    if args.module_file:
        with open(args.module_file, "r", encoding="utf-8") as f:
            module_text = f.read()
        module_path = args.module_file
    else:
        # ファイル名が無い場合は標準入力から読む
        if not sys.stdin.isatty():
            # ユーザ指定: isatty() でなければ help を出して終了
            parser.print_help(sys.stderr)
            raise SystemExit(1)
        module_text = sys.stdin.read()
        module_path = None

    # インスタンスファイル
    with open(args.inst_file, "r", encoding="utf-8") as f:
        inst_text = f.read()

    # モジュール解析
    module_name, header_ports, body, pre, post = parse_module(module_text)
    port_info = parse_port_declarations(body, header_ports)
    port_to_internal, internal_to_ports, port_segments = analyse_assigns(
        body, header_ports, port_info
    )

    # 新しいモジュールのテキスト生成
    new_module_text = build_new_module_text(
        module_name,
        header_ports,
        body,
        pre,
        post,
        port_info,
        port_to_internal,
        style=args.style,
    )

    # インスタンスの書き換え
    new_inst_text = rewrite_instantiations(inst_text, module_name, port_to_internal)

    # 出力: module_file がある場合は上書き、無い場合は標準出力へ
    if module_path:
        with open(module_path, "w", encoding="utf-8") as f:
            f.write(new_module_text)
        # インスタンスファイルも上書き
        with open(args.inst_file, "w", encoding="utf-8") as f:
            f.write(new_inst_text)
    else:
        # module は標準出力、インスタンスはそのまま上書き
        sys.stdout.write(new_module_text)
        with open(args.inst_file, "w", encoding="utf-8") as f:
            f.write(new_inst_text)


if __name__ == "__main__":
    main()
