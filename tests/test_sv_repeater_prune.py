# tests/test_sv_repeater_prune.py
import os
import sys
import subprocess
from textwrap import dedent

SCRIPT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "sv_repeater_prune.py"))
PY = sys.executable

def run_tool(verilog_src: str, lhs_pat: str, tmp_path, inplace=True):
    v = tmp_path / "dut.v"
    v.write_text(verilog_src, encoding="utf-8")

    args = [PY, SCRIPT, str(v), "--lhs-pattern", lhs_pat]
    if inplace:
        args.append("--inplace")
    # diff は stdout に出る
    res = subprocess.run(args, capture_output=True, text=True, check=True)
    return res.stdout, v.read_text(encoding="utf-8")


def test_double_invert_chain(tmp_path):
    src = dedent("""\
    module M(
      input  wire p_abc_in,
      output wire p_abc_copy
    );
      wire p_abc, m_abc;
      assign p_abc = p_abc_in;
      assign m_abc = ~p_abc;
      assign p_abc_copy = ~m_abc;
    endmodule
    """)
    # m_* と copy* を対象に含める
    pat = r"(m_.*|.*_copy\d*)"
    diff, out = run_tool(src, pat, tmp_path)

    # 期待：p_abc_copy は p_abc に置換、m_abc の assign と宣言から m_abc だけ削除
    expect = dedent("""\
    module M(
      input  wire p_abc_in,
      output wire p_abc_copy
    );
      wire p_abc;
      assign p_abc = p_abc_in;
      assign p_abc_copy = p_abc;
    endmodule
    """)
    print(out)
    assert out == expect


def test_pow_replication_and_copy_indices(tmp_path):
    src = dedent("""\
    module M(
      input  wire p_foo,
      output wire p_hoge_copy0,
      output wire p_hoge_copy3,
      output wire [1:0] p_hoge_copy6
    );
      wire [3:0] p_hoge_pow1, p_hoge_pow2;
      assign p_hoge_pow1[3:0] = {4{p_foo}};
      assign p_hoge_pow2 = {4{p_foo}};
      assign p_hoge_copy0 = p_hoge_pow1[0];
      assign p_hoge_copy3 = p_hoge_pow1[3];
      assign p_hoge_copy6[1:0] = p_hoge_pow2[1:0];
    endmodule
    """)
    pat = r"(.*_pow\d+|.*_copy\d+)"
    diff, out = run_tool(src, pat, tmp_path)

    # 期待：copy はすべて p_foo に直結、pow1/2 の宣言と assign は削除
    expect = dedent("""\
    module M(
      input  wire p_foo,
      output wire p_hoge_copy0,
      output wire p_hoge_copy3,
      output wire [1:0] p_hoge_copy6
    );
      assign p_hoge_copy0 = p_foo;
      assign p_hoge_copy3 = p_foo;
      assign p_hoge_copy6 = {2{p_foo}};
    endmodule
    """)
    print(out)
    assert out == expect


def test_slice_and_vector_copy(tmp_path):
    src = dedent("""\
    module M(
      input  wire [7:0] bus_in,
      output wire [3:0] x_cpy1
    );
      wire [3:0] x_pow1;
      assign x_pow1[3:0] = bus_in[7:4];
      assign x_cpy1 = x_pow1;
    endmodule
    """)
    pat = r"(x_pow\d+|x_cpy\d+)"
    diff, out = run_tool(src, pat, tmp_path)

    expect = dedent("""\
    module M(
      input  wire [7:0] bus_in,
      output wire [3:0] x_cpy1
    );
      assign x_cpy1 = bus_in[7:4];
    endmodule
    """)
    print(out)
    assert out == expect



def test_rhs_only_replacement_lhs_kept(tmp_path):
    src = dedent("""\
    module M(input wire a, output wire copy1);
      wire m_tmp;
      assign m_tmp = ~a;
      assign copy1 = ~m_tmp; // LHS は変えない
    endmodule
    """)
    pat = r"(m_.*|copy\d+)"
    diff, out = run_tool(src, pat, tmp_path)

    expect = dedent("""\
    module M(input wire a, output wire copy1);
      assign copy1 = a; // LHS は変えない
    endmodule
    """)
    print(out)
    assert out == expect


def test_skip_ports_on_lhs_map(tmp_path):
    # 出力ポートが copy0 でも、ポートそのものは replace_map 登録対象外（assign の LHS が対象）
    src = dedent("""\
    module M(
      input  wire a,
      output wire copy0
    );
      wire pow1;
      assign pow1 = a;
      assign copy0 = pow1;
    endmodule
    """)
    pat = r"(pow\d+|copy\d+)"
    diff, out = run_tool(src, pat, tmp_path)

    expect = dedent("""\
    module M(
      input  wire a,
      output wire copy0
    );
      assign copy0 = a;
    endmodule
    """)
    print(out)
    assert out == expect


def test_remove_only_target_from_mixed_decl(tmp_path):
    src = dedent("""\
    module M(input wire s, output wire c0);
      wire keep_me, copy0, also_keep;
      assign copy0 = s;
      assign c0 = copy0 & keep_me;
    endmodule
    """)
    pat = r"(copy\d+)"
    diff, out = run_tool(src, pat, tmp_path)

    # copy0 は展開後に未参照ではない（c0 で参照される）→ このままだと残る…に注意。
    # しかし置換で c0 = s & keep_me; になり、copy0 は未使用になり、assign/宣言から copy0 だけ消える。
    expect = dedent("""\
    module M(input wire s, output wire c0);
      wire keep_me, also_keep;
      assign c0 = s & keep_me;
    endmodule
    """)
    print(out)
    assert out == expect
