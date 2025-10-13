# tests/test_extract.py
import sys
import textwrap
from pathlib import Path
import subprocess
import importlib.util

# ==== helper: load extract.py as a module from repo root ====
ROOT_DIR = Path(__file__).resolve().parents[1]
EXTRACT_PATH = ROOT_DIR / "extract.py"

def load_extract_module():
    spec = importlib.util.spec_from_file_location("extract_mod", str(EXTRACT_PATH))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load spec for {EXTRACT_PATH}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

# ==== fixtures (Verilog sources) ====

FOO_SV = """\
module foo(
  AAA,
  BBB,
  CCC,
  DDD,
  EEE
);
  input [3:0] AAA;
  input       BBB;
  input       CCC;
  input       DDD;
  output      EEE;
  wire en = BBB & DDD;
  assign EEE = en ? (|AAA) ^ CCC : 1'b0;
endmodule
"""

BAR_SV = """\
module bar(
  P,
  Q,
  R,
  S
);
  input  [15:0] P;
  input         Q;
  output [15:0] R;
  output [7:0]  S;
  assign R = Q ? {P[7:0], P[15:8]} : P;
  assign S = P[7:0] ^ P[15:8];
endmodule
"""

# トップ（複合ケース：assign/スライス/複数インスタンス/外部使用有無）
TOP_COMPLEX = """\
module top(input logic clk, input logic rst);
  // 親宣言（幅継承チェック用）
  logic signed [7:0] data0, data1;
  logic [15:0] bus_in, bus_out;
  logic [31:0] X, Y, Z;
  logic [3:0]  aaa;
  logic        bbb, ddd, eee, fff, ggg;
  logic [3:0]  bus_in_hi;
  logic        ccc_bit2;
  logic [7:0]  w0, w1, w2;
  reg          flag;

  // @extract-begin
  // LHS なので外部ポート化しない（ローカル or 外部使用なら output）
  assign bus_in_hi = bus_in[7:4];
  assign ccc[0]    = data0[0] & bbb;
  assign ccc[3:1]  = {3{bbb}} & aaa[3:1];
  assign ccc_bit2  = ccc[2];

  foo u_foo0(
    .AAA(aaa[3:2]),
    .BBB(bbb),
    .CCC(ccc_bit2),
    .DDD(ddd),
    .EEE(eee)
  );

  foo u_foo1(
    .AAA(bus_in_hi),
    .BBB(flag),
    .CCC(bbb),
    .DDD(ggg),
    .EEE(fff)
  );

  bar u_bar0(
    .P(bus_in),
    .Q(eee),
    .R(bus_out),
    .S(w0)   // ← これが宣言とコメントに現れても外部使用とはみなさない
  );

  assign w1 = {4{bbb}} << 2;
  assign w2 = w0 ^ w1;
  // @extract-end

  // 外部使用判定：eee と bus_out は外で使用
  always_ff @(posedge clk) begin
    if (eee) begin
      Z <= X + Y + bus_out;
    end
  end
  // コメントに w0 があっても使用ではない: // w0 used?
endmodule
"""

# assign のみ（インスタンス無し）でも入出力が推論されるか
TOP_ASSIGN_ONLY = """\
module top;
  logic [15:0] bus_in;
  logic [3:0]  bus_in_hi;

  // @extract-begin
  assign bus_in_hi = bus_in[7:4];
  // @extract-end

  // 外部使用：bus_in_hi を使ってみる
  wire t = |bus_in_hi;
endmodule
"""

# コメント・宣言にだけ出てくる名前は外部使用に数えないか（w0）
TOP_COMMENT_DECL_ONLY = """\
module top;
  logic [7:0] w0;  // 宣言のみ
  // @extract-begin
  bar u1(
    .P(p),
    .Q(q),
    .R(r),
    .S(w0) // コメントにも w0
  );
  // @extract-end
endmodule
"""

def write(p: Path, s: str):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(s), encoding="utf-8")

# ==== tests ====

def test_extract_complex_function_direct(tmp_path: Path):
    """複合ケース：.AAA(aaa[3:2]) のスライス→aaa が input、外部使用フィルタ、幅継承、LHS ローカル宣言"""
    rtl = tmp_path / "rtl"
    write(rtl / "foo.sv", FOO_SV)
    write(rtl / "bar.sv", BAR_SV)
    top = tmp_path / "top.sv"
    write(top, TOP_COMPLEX)

    extract = load_extract_module()
    out = extract.gen_extracted_module_from_dirs(
        whole_src=top.read_text(encoding="utf-8"),
        search_dirs=[rtl],
        new_mod_name="my_slice",
    )

    # ヘッダの input/output を含むか
    # 入力（スライス/親宣言の継承も確認）
    assert "input [3:0] aaa" in out            # .AAA(aaa[3:2]) → base 'aaa'
    assert "input bbb" in out
    assert "input ddd" in out
    assert "input ggg" in out
    assert "input flag" in out
    assert "input [15:0] bus_in" in out        # 親幅の継承
    # 出力（外で使われるもののみ）
    assert "output eee" in out
    assert "output [15:0] bus_out" in out
    # 外で使われない fff, w0 は出力に含まれない
    assert "output fff" not in out
    # ローカル宣言（assign LHS でポート化されなかったもの）
    assert "logic [3:0] bus_in_hi" in out or "logic bus_in_hi" in out
    assert "assign bus_in_hi = bus_in[7:4];" in out
    # モジュール本文がコピーされている（抜粋）
    assert "foo u_foo0(" in out and "bar u_bar0(" in out

def test_extract_assign_only_block(tmp_path: Path):
    """assign だけでも RHS→input, LHS(外部使用)→output が推論される"""
    rtl = tmp_path / "rtl"
    write(rtl / "foo.sv", FOO_SV)  # 使わないが探索先として存在させておく
    top = tmp_path / "top.sv"
    write(top, TOP_ASSIGN_ONLY)

    extract = load_extract_module()
    out = extract.gen_extracted_module_from_dirs(
        whole_src=top.read_text(encoding="utf-8"),
        search_dirs=[rtl],
        new_mod_name="slice_assign_only",
    )
    # 入力：RHS の bus_in、出力：LHS の bus_in_hi（外で使用されているので）
    assert "input [15:0] bus_in" in out
    assert "output [3:0] bus_in_hi" in out or "output bus_in_hi" in out

def test_extract_multiple_moddirs_and_cli(tmp_path: Path):
    """複数 -I の検索と CLI 実行の両方を検証"""
    ip = tmp_path / "ip"
    rtl = tmp_path / "rtl"
    ip.mkdir(); rtl.mkdir()
    # bar は ip 側に、foo は rtl 側に
    write(ip / "bar.sv", BAR_SV)
    write(rtl / "foo.sv", FOO_SV)
    top = tmp_path / "top.sv"
    write(top, TOP_COMPLEX)

    # CLI 実行
    proc = subprocess.run(
        [
            sys.executable, str(EXTRACT_PATH),
            str(top),
            "-I", str(rtl),
            "-I", str(ip),
            "-o", str(tmp_path / "out.sv"),
            "--name", "my_slice",
        ],
        capture_output=True, text=True
    )
    assert proc.returncode == 0
    txt = (tmp_path / "out.sv").read_text(encoding="utf-8")
    # 代表的な成果物
    assert "module my_slice(" in txt
    assert "input [15:0] bus_in" in txt
    assert "output [15:0] bus_out" in txt
    assert "output fff" not in txt

def test_extract_comment_and_decl_not_counted_as_use(tmp_path: Path):
    """コメント・宣言に現れるだけのシンボルは外部使用と見なされない"""
    # bar は使う（w0 を S に接続）
    write(tmp_path / "rtl/bar.sv", BAR_SV)
    write(tmp_path / "top.sv", TOP_COMMENT_DECL_ONLY)

    extract = load_extract_module()
    out = extract.gen_extracted_module_from_dirs(
        whole_src=(tmp_path / "top.sv").read_text(encoding="utf-8"),
        search_dirs=[tmp_path / "rtl"],
        new_mod_name="blk",
    )
    # w0 は宣言とコメントにしか出ないので output に入らない
    # 入出力に w0 が含まれていないことを確認
    header = out.split(");", 1)[0]
    assert "w0" not in header

def test_extract_slice_input_detected(tmp_path: Path):
    """インスタンス接続 .AAA(aaa[3:2]) で base 'aaa' が input に入るかの単体チェック"""
    write(tmp_path / "rtl/foo.sv", FOO_SV)
    # 最小の top
    src = """\
    module top;
      logic [3:0] aaa; logic bbb, ddd, eee; logic ccc_bit2;
      // @extract-begin
      foo u0(
        .AAA(aaa[3:2]),
        .BBB(bbb),
        .CCC(ccc_bit2),
        .DDD(ddd),
        .EEE(eee)
      );
      // @extract-end
      always @* if (eee) $display("x");
    endmodule
    """
    write(tmp_path / "top.sv", src)

    extract = load_extract_module()
    out = extract.gen_extracted_module_from_dirs(
        whole_src=(tmp_path / "top.sv").read_text(encoding="utf-8"),
        search_dirs=[tmp_path / "rtl"],
        new_mod_name="blk",
    )
    assert "input [3:0] aaa" in out
    assert "output eee" in out
