// ======================================================
// top.sv : 切り出し元のサンプル（@extract-begin/@extract-end）
// ======================================================
module top(input logic clk, input logic rst);

  // --- 親側の宣言（幅継承テスト用） --------------------
  // ・カンマ区切り宣言
  logic signed [7:0] data0, data1;
  logic [15:0] bus_in, bus_out;
  logic [31:0] X, Y, Z;

  // ・スカラとベクタ混在
  logic [3:1] aaa;
  logic       bbb, ddd, eee, fff, ggg;

  // ・LHSローカル化のうち、幅を与えたいものは親に宣言しておく
  //   （例：bus_in_hi は foo.AAA [3:0] に繋げるため 4bit で親に宣言）
  logic [3:0] bus_in_hi;
  logic       ccc_bit2;

  // ・packed/unpacked 配列（パーサは packed のみ拾う想定）
  logic [7:0] w0, w2;
  logic [7:0] arr [0:3];  // unpacked（この行は幅辞書には載らない想定）

  // ・reg 宣言も混ぜる
  reg flag;

  // ・外部利用テスト用（extractブロックの出力のうち一部だけ参照）
  //   → eee と bus_out はブロック外で使う（出力ポートに採用されるべき）
  //   → fff と w0 はブロック外では使わない（出力ポートから落ちるべき）

  // ============================
  // ここからが切り出しブロック
  // ============================

  // @extract-begin
  // --- ブロック内の中間信号（LHS） --------------------
  // これらは「assign の左辺」なので外部ポートにしない前提
  // bus_in_hi は親で [3:0] 宣言済み（幅継承の練習）
  assign bus_in_hi = bus_in[7:4];

  // ccc はビット選択で代入、ccc_bit2 にもビットを抜き出しておく
  // ccc は親でスカラ宣言されていないが、ここではベース名 ccc を LHS に含める例
  assign ccc[0]   = data0[0] & bbb;
  assign ccc[3:1] = {3{bbb}} & aaa[3:1];
  assign ccc_bit2 = ccc[2];

  // @inline-begin
  my_slice my_slice0(
    .bus_in_hi
    ,.aaa
    ,.flag
    ,.bbb
    ,.ccc_bit2
    ,.ggg
    ,.ddd
    ,.bus_in
    ,.eee
    ,.fff
    ,.bus_out
    ,.w0
    ,.w2
  );
  // @inline-end

  // @extract-end
  // ============================
  // ここまでが切り出しブロック
  // ============================

  // --- ブロック外での「外部利用」 ----------------------
  // ・eee と bus_out は参照される → 新モジュールの output に残すべき
  always_ff @(posedge clk) begin
    if (eee) begin
        Z <= X + Y + bus_out + w2;
    end
  end

  // ・fff, w0 はここでは未使用 → 新モジュールの output から落ちるはず

endmodule
