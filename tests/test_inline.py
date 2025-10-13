# tests/test_inline.py
import sys
import textwrap
from pathlib import Path
import subprocess
import importlib.util

ROOT_DIR = Path(__file__).resolve().parents[1]
INLINE_PATH = ROOT_DIR / "inline.py"

def load_inline_module():
    spec = importlib.util.spec_from_file_location("inline_mod", str(INLINE_PATH))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load spec for {INLINE_PATH}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

# --- 共通のテンプレ ---
FOO_SV = """\
module foo(
  AAA,
  BBB,
  EEE
);
  input [3:0] AAA;
  input       BBB;
  output      EEE;

  // 内部宣言
  logic [3:0] tmp;
  logic       en;

  // 本体
  assign en  = BBB;
  assign tmp = AAA ^ 4'h9;
  assign EEE = en ? |tmp : 1'b0;
endmodule
"""

TOP_INLINE_MARKED = """\
module top;
  logic [3:0] aaa;
  logic       bbb;
  logic       eee;

  // @inline-begin
  foo u0(
    .AAA(aaa[3:0]),
    .BBB(bbb),
    .EEE(eee)
  );
  // @inline-end

  always @* if (eee) $display("hi");
endmodule
"""

TOP_INLINE_COLLISION = """\
module top;
  logic [3:0] aaa;
  logic       bbb;
  logic       eee;

  // ここが衝突: foo 内の 'en' と 'tmp' を既に宣言している
  logic en;
  logic [3:0] tmp;

  // @inline-begin
  foo u0(
    .AAA(aaa),
    .BBB(bbb),
    .EEE(eee)
  );
  // @inline-end
endmodule
"""

BAR_SV = """\
module bar(
  X,
  Y
);
  input X;
  output Y;
  logic t;
  assign t = X;
  assign Y = t;
endmodule
"""

TOP_NEED_BAR = """\
module top;
  logic x, y;
  // @inline-begin
  bar u1(
    .X(x),
    .Y(y)
  );
  // @inline-end
endmodule
"""

TOP_NO_INSTANCE = """\
module top;
  // @inline-begin
  // インスタンスがありません
  // @inline-end
endmodule
"""


def write(p: Path, s: str):
    p.write_text(textwrap.dedent(s), encoding="utf-8")


def test_inline_basic_ok(tmp_path: Path):
    """基本ケース: foo を inline、置換結果に (aaa[3:0]) / (bbb) / (|tmp) が現れることを確認"""
    # 配置
    (tmp_path / "rtl").mkdir()
    write(tmp_path / "rtl" / "foo.sv", FOO_SV)
    top = tmp_path / "top.sv"
    write(top, TOP_INLINE_MARKED)

    # import inline.py
    inline_mod = load_inline_module()

    new_src = inline_mod.inline_module(
        (tmp_path / "top.sv").read_text(encoding="utf-8"),
        mod_name="foo",
        search_dirs=[tmp_path / "rtl"],
        begin_pat=r"// @inline-begin",
        end_pat=r"// @inline-end",
    )
    assert new_src is not None
    # モジュール本体が展開され、ポート名が (expr) で置換されていること
    assert "module foo" not in new_src
    assert "endmodule" in new_src  # top の endmodule
    assert "(aaa[3:0])" in new_src
    assert "(bbb)" in new_src
    # |tmp は展開後の式の一部（EEE の式）に残る
    assert "|tmp" in new_src
    # 置換は識別子単位なので AAA/BBB が残存しないこと
    assert " AAA " not in new_src
    assert " BBB " not in new_src


def test_inline_detect_collision_and_abort_cli(tmp_path: Path):
    """衝突検出: foo の内部宣言 en/tmp が top で既に宣言 → 名前一覧を出力し非0終了"""
    (tmp_path / "rtl").mkdir()
    write(tmp_path / "rtl" / "foo.sv", FOO_SV)
    write(tmp_path / "top.sv", TOP_INLINE_COLLISION)

    # inline.py を CLI として実行
    script = Path(__file__).resolve().parents[1] / "inline.py"
    assert script.exists(), "inline.py がリポジトリ直下にある想定です"

    proc = subprocess.run(
        [
            sys.executable, str(script),
            str(tmp_path / "top.sv"),
            "-I", str(tmp_path / "rtl"),
            "--module", "foo",
            "-o", str(tmp_path / "out.sv"),
        ],
        capture_output=True, text=True
    )
    # 衝突なので失敗終了（仕様では exit code 2 を使用）
    assert proc.returncode != 0
    # 標準出力に衝突名（行区切り）を全て出す
    out = proc.stdout.strip().splitlines()
    # 順不同可。集合で比較
    assert set(out) == {"en", "tmp"}
    # 出力ファイルは生成されない（または空）
    assert not (tmp_path / "out.sv").exists()


def test_inline_multiple_moddirs(tmp_path: Path):
    """探索ディレクトリが複数でも正しく解決されること"""
    d1 = tmp_path / "ip"
    d2 = tmp_path / "rtl"
    d1.mkdir(); d2.mkdir()
    # bar.sv は ip/ に置く
    write(d1 / "bar.sv", BAR_SV)
    write(tmp_path / "top.sv", TOP_NEED_BAR)

    script = Path(__file__).resolve().parents[1] / "inline.py"
    proc = subprocess.run(
        [
            sys.executable, str(script),
            str(tmp_path / "top.sv"),
            "-I", str(d2),
            "-I", str(d1),
            "--module", "bar",
            "-o", str(tmp_path / "out.sv"),
        ],
        capture_output=True, text=True
    )
    assert proc.returncode == 0
    txt = (tmp_path / "out.sv").read_text(encoding="utf-8")
    assert "module bar" not in txt
    assert "(x)" in txt and "(y)" in txt  # ポート置換済み


def test_inline_handles_slices_and_ops(tmp_path: Path):
    """スライス・演算を含む接続式でも (expr) で置換されること"""
    (tmp_path / "rtl").mkdir()
    write(tmp_path / "rtl" / "foo.sv", FOO_SV)
    # AAA にスライス、BBB に演算
    src = TOP_INLINE_MARKED.replace(".BBB(bbb)", ".BBB(aaa[0] & bbb)")
    write(tmp_path / "top.sv", src)

    inline_mod = load_inline_module()

    new_src = inline_mod.inline_module(
        (tmp_path / "top.sv").read_text(encoding="utf-8"),
        mod_name="foo",
        search_dirs=[tmp_path / "rtl"],
        begin_pat=r"// @inline-begin",
        end_pat=r"// @inline-end",
    )
    assert new_src is not None
    assert "(aaa[3:0])" in new_src
    assert "(aaa[0] & bbb)" in new_src  # 演算式も括弧で展開


def test_inline_instance_not_found(tmp_path: Path):
    """マーカー内に対象モジュールのインスタンスが無ければエラー"""
    (tmp_path / "rtl").mkdir()
    write(tmp_path / "rtl" / "foo.sv", FOO_SV)
    write(tmp_path / "top.sv", TOP_NO_INSTANCE)

    inline_mod = load_inline_module()

    try:
        inline_mod.inline_module(
            (tmp_path / "top.sv").read_text(encoding="utf-8"),
            mod_name="foo",
            search_dirs=[tmp_path / "rtl"],
            begin_pat=r"// @inline-begin",
            end_pat=r"// @inline-end",
        )
        assert False, "例外が投げられるべき"
    except ValueError as e:
        assert "Instance of 'foo' not found" in str(e)
