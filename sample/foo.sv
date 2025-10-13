// ======================================================
// foo.sv : 1行1宣言のポート宣言（前提どおり）
// ======================================================
module foo(
  AAA,
  BBB,
  CCC,
  DDD,
  EEE
);
  input [3:0] AAA;
  input BBB;
  input CCC;
  input DDD;
  output EEE;

  // 適当なロジック（テスト用途）
  wire en = BBB & DDD;
  assign EEE = en ? (|AAA) ^ CCC : 1'b0;
endmodule
