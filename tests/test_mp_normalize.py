# tests/test_mp_normalize.py
import textwrap
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
import mp_normalize as mp  # ← スクリプト名に合わせて変更してください


def _run_transform(src: str) -> str:
    """テキスト -> transform -> テキスト"""
    src = textwrap.dedent(src).lstrip("\n")
    lines = [l + "\n" for l in src.splitlines()]
    out_lines = mp.transform(lines)
    return "".join(out_lines)


def _assert_verilog_equal(actual: str, expected: str):
    """空白差などに多少寛容な比較"""
    actual_lines = [l.rstrip() for l in actual.strip().splitlines()]
    expected_lines = [
        l.rstrip()
        for l in textwrap.dedent(expected).lstrip("\n").strip().splitlines()
    ]
    assert actual_lines == expected_lines


def test_case2_pair_with_declaration_and_index():
    """m_ と p_ のペアがある場合:
       - m_ の assign は削除
       - p_ の assign に畳み込み
       - 参照 m_[...] → ~p_[...] に変換
       - 宣言から m_ を削除
    """
    src = """
    module top;
      wire [3:0] m_hoge, foo;
      assign m_hoge[3:0] = ~(a[3:0] & b[3:0]);
      assign p_hoge[3:0] = ~m_hoge[3:0];
      assign bar = m_hoge[2] | c;
    endmodule
    """
    expected = """
    module top;
      wire [3:0] foo;
      assign p_hoge[3:0] = a[3:0] & b[3:0];
      assign bar = ~p_hoge[2] | c;
    endmodule
    """
    out = _run_transform(src)
    _assert_verilog_equal(out, expected)


def test_case1_simple_rename_and_reference():
    """m_ だけある場合:
       - assign m_foo = ~(expr); → assign p_foo = expr;
       - 宣言 m_foo → p_foo
       - 参照 m_foo → ~p_foo
    """
    src = """
    module top;
      wire m_foo;
      assign m_foo = ~(a & b);
      assign out = m_foo ^ d;
    endmodule
    """
    expected = """
    module top;
      wire p_foo;
      assign p_foo = a & b;
      assign out = ~p_foo ^ d;
    endmodule
    """
    out = _run_transform(src)
    _assert_verilog_equal(out, expected)


def test_existing_p_name_means_skip_m_assign():
    """すでに p_base が存在する場合は m_base の変換をスキップする。"""
    src = """
    module top;
      wire m_bar, p_bar;
      assign m_bar = ~(a | b);
      assign out1 = m_bar;
      assign out2 = p_bar;
    endmodule
    """
    out = _run_transform(src)
    # 何も変わらないはず
    _assert_verilog_equal(out, src)


def test_multibit_with_index_rewritten_to_negated_p():
    """複数ビット・ビット指定付きの m_sig[...] も ~p_sig[...] に変換される。"""
    src = """
    module top;
      logic [7:0] m_sig;
      assign m_sig[7:0] = ~(a[7:0]);
      assign x = m_sig[3] & m_sig[4];
    endmodule
    """
    expected = """
    module top;
      logic [7:0] p_sig;
      assign p_sig[7:0] = a[7:0];
      assign x = ~p_sig[3] & ~p_sig[4];
    endmodule
    """
    out = _run_transform(src)
    _assert_verilog_equal(out, expected)


def test_mixed_declaration_rename_and_delete():
    """同じ宣言行に rename 対象と delete 対象と関係ない信号が混ざっているケース。"""
    src = """
    module top;
      wire m_foo, m_bar, keep1;
      wire keep2;
      assign m_foo = ~(a);
      assign p_bar = ~m_bar;
      assign m_bar = ~(b);
      assign y = m_foo & m_bar & keep1 & keep2;
    endmodule
    """
    # foo は case1 (m_ だけ) → p_foo にリネーム + 参照 ~p_foo
    # bar は case2 (m_ / p_ ペア) → m_bar の assign 削除 + p_bar に畳み込み + 宣言から m_bar 削除
    expected = """
    module top;
      wire p_foo, keep1;
      wire keep2;
      assign p_foo = a;
      assign p_bar = b;
      assign y = ~p_foo & ~p_bar & keep1 & keep2;
    endmodule
    """
    out = _run_transform(src)
    _assert_verilog_equal(out, expected)


def test_0():
    src = """
    module top;
      wire m_foo, m_bar;
      assign m_foo = ~((a &b) & ~(a|~b));
      assign m_bar = ~b & a;
      assign y = m_foo & m_bar;
    endmodule
    """
    expected = """
    module top;
      wire p_foo, m_bar;
      assign p_foo = (a &b) & ~(a|~b);
      assign m_bar = ~b & a;
      assign y = ~p_foo & m_bar;
    endmodule
    """
    out = _run_transform(src)
    _assert_verilog_equal(out, expected)


def test_1():
    src = """
    module top;
      logic [3:0] m_foo;
      logic [3:0] a, b;
      assign m_foo[3:2] = ~(a[3:2] & ~b[3:2]);
      assign m_foo[1:0] = ~(a[1:0] & b[1:0]);
      assign m_bar = ~b & a;
      assign y = m_foo & {4{m_bar}};
    endmodule
    """
    expected = """
    module top;
      logic [3:0] p_foo;
      logic [3:0] a, b;
      assign p_foo[3:2] = a[3:2] & ~b[3:2];
      assign p_foo[1:0] = a[1:0] & b[1:0];
      assign m_bar = ~b & a;
      assign y = ~p_foo & {4{m_bar}};
    endmodule
    """
    out = _run_transform(src)
    _assert_verilog_equal(out, expected)


# def test_2():
#     src = """
#     module top;
#       logic [3:0] p_foo;
#       logic [3:0] m_foo;
#       logic [3:0] a, x, c, d;
#       assign p_foo = c | d;
#       assign m_foo = ~p_foo;
#       assign x = a & ~m_foo;
#     endmodule
#     """
#     expected = """
#     module top;
#       logic [3:0] p_foo;
#       logic [3:0] a, x, c, d;
#       assign p_foo = c | d;
#       assign x = a & p_foo;
#     endmodule
#     """
#     out = _run_transform(src)
#     print(out)
#     _assert_verilog_equal(out, expected)


def test_3():
    src = """
    module top;
      logic [3:0] m_foo;
      logic [3:0] a, x, b, c;
      assign m_foo = ~(c & b);
      assign x = a & ~m_foo;
    endmodule
    """
    expected = """
    module top;
      logic [3:0] p_foo;
      logic [3:0] a, x, b, c;
      assign p_foo = c & b;
      assign x = a & p_foo;
    endmodule
    """
    out = _run_transform(src)
    print(out)
    _assert_verilog_equal(out, expected)


def test_4():
    src = """
    module top;
      logic [3:0] m_hoge;
      logic [3:0] p_hoge;
      logic [3:0] a, b;
      assign m_hoge[55:30] = ~(a[55:30] | b[55:30]);
      assign m_hoge[25:0] = ~(a[25:0] | b[25:0]);
      assign p_hoge[25:0] = ~m_hoge[20:0];
      assign p_hoge[55:30] = ~m_hoge[55:30];
    endmodule
    """
    expected = """
    module top;
      logic [3:0] p_hoge;
      logic [3:0] a, b;
      assign p_hoge[55:30] = a[55:30] | b[55:30];
      assign p_hoge[25:0] = a[25:0] | b[25:0];
    endmodule
    """
    out = _run_transform(src)
    print(out)
    _assert_verilog_equal(out, expected)
