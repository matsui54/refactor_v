import textwrap
import re
import sys
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import verilog_port_rename as vpr


def norm(s: str) -> str:
    """空白・改行をそれなりに吸収して比較しやすくする"""
    # 行頭末尾の空白削除
    lines = [line.strip() for line in s.strip().splitlines()]
    # 空行は消す
    lines = [l for l in lines if l]
    # 連続空白を1つに
    lines = [re.sub(r"\s+", " ", l) for l in lines]
    return "\n".join(lines)


def test_basic_non_ansi_module_and_instance_rewrite():
    """質問に出てきたような素直なケースを non-ansi で変換できるか"""

    module_src = textwrap.dedent(
        r"""
        module HOGE (
          AB12,
          HB92,
          HOG1
        );

          input AB12;
          input HB92[3:0];
          output HOG1;

          wire p_abc12;
          wire p_cde34[3:0];
          wire p_efg;

          assign p_abc12 = AB12;
          assign p_cde34[3:0] = HB92[3:0];

          assign HOG1 = p_efg;
        endmodule
        """
    )

    inst_src = textwrap.dedent(
        r"""
        module TOP;
          wire sig0;
          wire [3:0] sig1;
          wire sign;

          HOGE HOGE0 (
            .AB12(sig0),
            .HB92(sig1),
            .HOG1(sign)
          );
        endmodule
        """
    )

    # モジュール解析
    name, header_ports, body, pre, post = vpr.parse_module(module_src)
    assert name == "HOGE"
    assert header_ports == ["AB12", "HB92", "HOG1"]

    port_info = vpr.parse_port_declarations(body, header_ports)
    assert port_info["AB12"].direction == "input"
    assert port_info["AB12"].msb is None
    assert port_info["HB92"].msb == 3
    assert port_info["HB92"].lsb == 0
    assert port_info["HOG1"].direction == "output"

    port_to_internal, _, _ = vpr.analyse_assigns(body, header_ports, port_info)
    # ポート名→内部信号名のマッピング
    assert port_to_internal == {
        "AB12": "p_abc12",
        "HB92": "p_cde34",
        "HOG1": "p_efg",
    }

    # non-ansi で新しいモジュールを生成
    new_mod = vpr.build_new_module_text(
        module_name=name,
        header_ports=header_ports,
        body=body,
        pre=pre,
        post=post,
        port_info=port_info,
        port_to_internal=port_to_internal,
        style="non-ansi",
    )

    expected_mod = textwrap.dedent(
        r"""
        module HOGE (
          p_abc12,
          p_cde34,
          p_efg
        );
          input p_abc12;
          input [3:0] p_cde34;
          output p_efg;

          wire p_abc12;
          wire p_cde34[3:0];
          wire p_efg;

        endmodule
        """
    )

    # whitespace をある程度無視して比較
    assert norm(new_mod) == norm(expected_mod)

    # インスタンス書き換え
    new_inst = vpr.rewrite_instantiations(inst_src, "HOGE", port_to_internal)

    expected_inst = textwrap.dedent(
        r"""
        module TOP;
          wire sig0;
          wire [3:0] sig1;
          wire sign;

          HOGE HOGE0 (
            .p_abc12(sig0),
            .p_cde34(sig1),
            .p_efg(sign)
          );
        endmodule
        """
    )

    assert norm(new_inst) == norm(expected_inst)


def test_basic_ansi_module():
    """ANSI port で出力するパターン"""

    module_src = textwrap.dedent(
        r"""
        module FOO (
          A,
          B
        );

          input [7:0] A;
          output B;

          wire [7:0] p_in;
          wire p_out;

          assign p_in = A;
          assign B = p_out;
        endmodule
        """
    )

    name, header_ports, body, pre, post = vpr.parse_module(module_src)
    port_info = vpr.parse_port_declarations(body, header_ports)
    port_to_internal, _, _ = vpr.analyse_assigns(body, header_ports, port_info)

    new_mod = vpr.build_new_module_text(
        name, header_ports, body, pre, post, port_info, port_to_internal, "ansi"
    )

    # ANSI 形式のヘッダになっているかざっくり確認
    assert "module FOO (" in new_mod
    assert "input [7:0] p_in" in new_mod
    assert "output p_out" in new_mod
    # 旧 input/output 宣言が消えていること
    assert "input [7:0] A;" not in new_mod
    assert "output B;" not in new_mod
    # ブリッジ用 assign が消えていること
    assert "assign p_in = A;" not in new_mod
    assert "assign B = p_out;" not in new_mod


def test_bit_range_split_with_different_ports_is_error():
    """
    p_da[31:18] = A20B[31:18];
    p_da[4:0]   = A20D[4:0];
    みたいに、同じ内部信号が別ポートに分割されている場合はエラーになること
    """

    module_src = textwrap.dedent(
        r"""
        module FOO (
          A20B,
          A20D
        );

          input [31:18] A20B;
          input [4:0] A20D;

          wire [31:0] p_da;

          assign p_da[31:18] = A20B[31:18];
          assign p_da[4:0]   = A20D[4:0];
        endmodule
        """
    )

    name, header_ports, body, pre, post = vpr.parse_module(module_src)
    port_info = vpr.parse_port_declarations(body, header_ports)

    with pytest.raises(SystemExit):
        vpr.analyse_assigns(body, header_ports, port_info)


def test_bit_mapping_mismatch_is_error():
    """
    assign a[1:0] = ABC[5:4];
    のようにビット位置対応が違う場合はエラー
    """

    module_src = textwrap.dedent(
        r"""
        module FOO (
          ABC
        );

          input [5:0] ABC;

          wire [1:0] p_a;

          assign p_a[1:0] = ABC[5:4];
        endmodule
        """
    )

    name, header_ports, body, pre, post = vpr.parse_module(module_src)
    port_info = vpr.parse_port_declarations(body, header_ports)

    with pytest.raises(SystemExit):
        vpr.analyse_assigns(body, header_ports, port_info)


def test_port_not_fully_covered_is_error():
    """
    ポート幅が [5:0] なのに assign が [5:4] しかない場合は
    「全ビットが assign されていない」としてエラー
    """

    module_src = textwrap.dedent(
        r"""
        module FOO (
          ABC
        );

          input [5:0] ABC;

          wire [5:0] p_a;

          assign p_a[5:4] = ABC[5:4];
        endmodule
        """
    )

    name, header_ports, body, pre, post = vpr.parse_module(module_src)
    port_info = vpr.parse_port_declarations(body, header_ports)

    with pytest.raises(SystemExit):
        vpr.analyse_assigns(body, header_ports, port_info)


def test_port_covered_by_multiple_consistent_segments_ok():
    """
    assign a[1:0] = ABC[1:0];
    assign a[5:4] = ABC[5:4];
    のように、同じ内部信号かつビット位置も一致していて、
    全ビットがカバーされていれば OK
    """

    module_src = textwrap.dedent(
        r"""
        module FOO (
          ABC
        );

          input [5:0] ABC;

          wire [5:0] p_a;

          assign p_a[1:0] = ABC[1:0];
          assign p_a[5:4] = ABC[5:4];
          // 真ん中の [3:2] は使わない仕様とする（幅6だけど実際は4bit相当など）
        endmodule
        """
    )

    name, header_ports, body, pre, post = vpr.parse_module(module_src)
    port_info = vpr.parse_port_declarations(body, header_ports)

    # ABC の宣言幅は [5:0] なので、分析関数的には
    # 「全ビットカバー」まではチェックしない（msb/lsb は分かるが、セグメントは[1:0]と[5:4]だけ）
    # -> 現実の仕様には依るが、このスクリプトではエラーにしない想定。
    port_to_internal, _, _ = vpr.analyse_assigns(body, header_ports, port_info)

    assert port_to_internal["ABC"] == "p_a"


def test_instance_with_parameter_rewrite():
    """
    HOGE #(...) inst (...) のようにパラメータ付きインスタンス
    でも named port の書き換えができるか
    """

    port_to_internal = {
        "AB12": "p_abc12",
        "HB92": "p_cde34",
        "HOG1": "p_efg",
    }

    inst_src = textwrap.dedent(
        r"""
        module TOP;
          wire sig0;
          wire [3:0] sig1;
          wire sign;

          HOGE #(
            .WIDTH(4)
          ) HOGE0 (
            .AB12(sig0),
            .HB92(sig1),
            .HOG1(sign)
          );
        endmodule
        """
    )

    new_inst = vpr.rewrite_instantiations(inst_src, "HOGE", port_to_internal)

    assert ".p_abc12(sig0)" in new_inst
    assert ".p_cde34(sig1)" in new_inst
    assert ".p_efg(sign)" in new_inst
    # パラメータ部分はそのまま残っていること
    assert ".WIDTH(4)" in new_inst
