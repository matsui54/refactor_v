# tests/test_parse_module_ports.py
import importlib.util
from pathlib import Path
import textwrap

ROOT_DIR = Path(__file__).resolve().parents[1]
EXTRACT_PATH = ROOT_DIR / "extract.py"


def load_extract_module():
    spec = importlib.util.spec_from_file_location("extract_mod", str(EXTRACT_PATH))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load spec for {EXTRACT_PATH}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_parse_ports_ansi_header_multiple_names_and_types():
    """
    ANSIポート宣言（ヘッダ内）で:
    - input logic [7:0] a, b
    - input signed [3:0] c
    - output y
    - inout wire [15:0] z
    を正しく抽出し、順序も維持されることを確認
    """
    extract = load_extract_module()
    src = textwrap.dedent(
        """
        module m(
          input logic [7:0]   a, b,
          input signed [3:0]  c, /* comment */ // trailing
          output              y,
          inout wire [15:0]   z
        );
        // body
        endmodule
        """
    )
    port_dir, order = extract.parse_module_ports(src)
    # 順序はヘッダの列挙順
    assert order == ["a", "b", "c", "y", "z"]
    # 幅と方向
    assert port_dir["a"] == ("input", "[7:0]")
    assert port_dir["b"] == ("input", "[7:0]")
    assert port_dir["c"] == ("input", "[3:0]")
    assert port_dir["y"] == ("output", "")
    assert port_dir["z"] == ("inout", "[15:0]")


def test_parse_ports_non_ansi_body_multiple_on_one_line():
    """
    non-ANSI（本体側）で1行に複数名があっても分解できること。
    """
    extract = load_extract_module()
    src = textwrap.dedent(
        """
        module m(a,b,c,y,z);
          input [7:0] a, b   ,  /*comment*/ c;
          output y;
          inout  wire [15:0] z;
        endmodule
        """
    )
    port_dir, order = extract.parse_module_ports(src)
    assert order == ["a", "b", "c", "y", "z"]
    assert port_dir["a"] == ("input", "[7:0]")
    assert port_dir["b"] == ("input", "[7:0]")
    assert port_dir["c"] == ("input", "[7:0]")
    assert port_dir["y"] == ("output", "")
    assert port_dir["z"] == ("inout", "[15:0]")


def test_parse_ports_header_overrides_body_when_both_present():
    """
    ANSI と non-ANSI が両方あるときは **ANSI優先**で幅・方向が決まること。
    （わざと本体側に異なる幅/方向を書いて上書きされないことを確認）
    """
    extract = load_extract_module()
    src = textwrap.dedent(
        """
        module m(
          input  logic [3:0] a,
          output             y
        );
          // 本体側に異なる宣言を書いてもヘッダ優先で無視される
          input  [7:0] a;   // <- 違う幅
          inout       y;    // <- 違う方向
        endmodule
        """
    )
    port_dir, order = extract.parse_module_ports(src)
    assert order == ["a", "y"]
    assert port_dir["a"] == ("input", "[3:0]")  # ヘッダ優先
    assert port_dir["y"] == ("output", "")      # ヘッダ優先（inout ではない）


def test_parse_ports_header_segments_without_semicolons():
    """
    ヘッダ内はセミコロンが無くても、方向キーワード境界でセグメント化できること。
    """
    extract = load_extract_module()
    src = textwrap.dedent(
        """
        module m(input [1:0] a, b, output c, inout [2:0] d);
        endmodule
        """
    )
    port_dir, order = extract.parse_module_ports(src)
    assert order == ["a", "b", "c", "d"]
    assert port_dir["a"] == ("input", "[1:0]")
    assert port_dir["b"] == ("input", "[1:0]")
    assert port_dir["c"] == ("output", "")
    assert port_dir["d"] == ("inout", "[2:0]")


def test_parse_ports_comments_and_unpacked_are_ignored_for_names():
    """
    コメントや unpacked 配列/初期化子があっても識別子名だけが抽出されること。
    """
    extract = load_extract_module()
    src = textwrap.dedent(
        """
        module m(
          input  logic [7:0] a /*aa*/,  // cmt
          input  logic [7:0] arr  [0:3] , b {foo},  // unpacked/初期化子混在
          output y
        );
        endmodule
        """
    )
    port_dir, order = extract.parse_module_ports(src)
    # 'arr' は unpacked 指定があっても base 名としては抽出対象（仕様：unpackedは幅には反映しない）
    # → _split_ident_list は base 部（識別子）を拾うため 'arr' も拾う
    assert order == ["a", "arr", "b", "y"]
    assert port_dir["a"] == ("input", "[7:0]")
    assert port_dir["arr"] == ("input", "[7:0]")  # 幅は packed のみ適用
    assert port_dir["b"] == ("input", "[7:0]")
    assert port_dir["y"] == ("output", "")


def test_parse_ports_handles_inout_and_signed_tokens():
    """
    inout と signed のトークンがあっても吸収され、幅が正しく拾えること。
    """
    extract = load_extract_module()
    src = textwrap.dedent(
        """
        module m(
          inout signed [15:0] z0, z1,
          input        [3:0]  s,
          output               o
        );
        endmodule
        """
    )
    port_dir, order = extract.parse_module_ports(src)
    assert order == ["z0", "z1", "s", "o"]
    assert port_dir["z0"] == ("inout", "[15:0]")
    assert port_dir["z1"] == ("inout", "[15:0]")
    assert port_dir["s"]  == ("input", "[3:0]")
    assert port_dir["o"]  == ("output", "")
