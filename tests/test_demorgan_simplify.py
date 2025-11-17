# tests/test_demorgan_simplify.py
import textwrap
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import demorgan_simplify as d  # スクリプト名に合わせて変更


def _run_transform(src: str) -> str:
    """テキスト -> transform_lines -> テキスト"""
    src = textwrap.dedent(src).lstrip("\n")
    lines = [l + "\n" for l in src.splitlines()]
    out_lines = d.transform_lines(lines)
    return "".join(out_lines)


def _assert_verilog_equal(actual: str, expected: str):
    """空白やインデント差にやや寛容な比較"""
    actual_lines = [l.rstrip() for l in actual.strip().splitlines()]
    expected_lines = [
        l.rstrip()
        for l in textwrap.dedent(expected).lstrip("\n").strip().splitlines()
    ]
    assert actual_lines == expected_lines


# ========= 基本パターン (スカラ) =========

def test_scalar_and_to_or():
    # ~(~foo & ~bar) → foo | bar
    src = """
    module top;
      assign p_hoge = ~(~foo & ~bar);
    endmodule
    """
    expected = """
    module top;
      assign p_hoge = foo | bar;
    endmodule
    """
    out = _run_transform(src)
    _assert_verilog_equal(out, expected)


def test_scalar_or_to_and():
    # ~(~a | ~b | ~c) → a & b & c
    src = """
    module top;
      assign x = ~(~a | ~b | ~c);
    endmodule
    """
    expected = """
    module top;
      assign x = a & b & c;
    endmodule
    """
    out = _run_transform(src)
    _assert_verilog_equal(out, expected)


def test_scalar_keeps_comment_and_indent():
    # コメントとインデントが保持されるか
    src = """
    module top;
        assign y = ~(~a & ~b);  // comment
    endmodule
    """
    expected = """
    module top;
        assign y = a | b;  // comment
    endmodule
    """
    out = _run_transform(src)
    _assert_verilog_equal(out, expected)


# ========= 複数ビット (bit select / range) =========

def test_bitsel_and_to_or():
    # ~(~foo[3] & ~bar[3]) → foo[3] | bar[3]
    src = """
    module top;
      assign p_hoge = ~(~foo[3] & ~bar[3]);
    endmodule
    """
    expected = """
    module top;
      assign p_hoge = foo[3] | bar[3];
    endmodule
    """
    out = _run_transform(src)
    _assert_verilog_equal(out, expected)


def test_vector_range_and_to_or():
    # ~(~foo[7:0] & ~bar[7:0]) → foo[7:0] | bar[7:0]
    src = """
    module top;
      assign p_vec = ~(~foo[7:0] & ~bar[7:0]);
    endmodule
    """
    expected = """
    module top;
      assign p_vec = foo[7:0] | bar[7:0];
    endmodule
    """
    out = _run_transform(src)
    _assert_verilog_equal(out, expected)


def test_vector_or_to_and():
    # ~(~a[3:0] | ~b[3:0] | ~c[3:0]) → a[3:0] & b[3:0] & c[3:0]
    src = """
    module top;
      assign z = ~(~a[3:0] | ~b[3:0] | ~c[3:0]);
    endmodule
    """
    expected = """
    module top;
      assign z = a[3:0] & b[3:0] & c[3:0];
    endmodule
    """
    out = _run_transform(src)
    _assert_verilog_equal(out, expected)


def test_nested_expression_inside_term():
    # ~(~(foo[3] & baz) & ~bar[3]) → (foo[3] & baz) | bar[3]
    src = """
    module top;
      assign y = ~(~(foo[3] & baz) & ~bar[3]);
    endmodule
    """
    expected = """
    module top;
      assign y = (foo[3] & baz) | bar[3];
    endmodule
    """
    out = _run_transform(src)
    _assert_verilog_equal(out, expected)


# ========= 変換しないパターン (ガード) =========

def test_mixed_ops_not_simplified():
    # & と | がトップレベルで混在している → 変換しない
    src = """
    module top;
      assign y = ~(~a & ~b | ~c);
    endmodule
    """
    out = _run_transform(src)
    _assert_verilog_equal(out, src)


def test_term_without_not_not_simplified():
    # ~a & b のように ~ じゃない項を含む → 変換しない
    src = """
    module top;
      assign y = ~(~a & b);
    endmodule
    """
    out = _run_transform(src)
    _assert_verilog_equal(out, src)


def test_non_candidate_assign_unchanged():
    # そもそも "~(...)" ではない assign は手を触れない
    src = """
    module top;
      assign y = a & b;
      assign z = ~(a & b);  // 外側が ~(~...) ではないので変換しない
    endmodule
    """
    out = _run_transform(src)
    _assert_verilog_equal(out, src)


def test_multiple_lines_mixed():
    # ファイル中に変換対象と非対象が混ざっているケース
    src = """
    module top;
      assign a1 = ~(~x & ~y);
      assign a2 = ~(~p | ~q);
      assign a3 = ~(~m & n);  // これは変換しない
      assign a4 = m & n;
    endmodule
    """
    expected = """
    module top;
      assign a1 = x | y;
      assign a2 = p & q;
      assign a3 = ~(~m & n);  // これは変換しない
      assign a4 = m & n;
    endmodule
    """
    out = _run_transform(src)
    _assert_verilog_equal(out, expected)
