# SystemVerilog CPU Design & Autonomous Debugging Guidelines

本プロジェクトにおいてRTLの設計・解析・デバッグを行う際は、コンテキスト（トークン）の浪費を防ぎ、最短手順で根本原因（Root Cause）を特定するため、以下のワークフローとツール使用規則を厳守すること。
**波形やトレースによる事実確認を完了する前に、推測でRTLコードを編集することを固く禁ずる。**

---

## 0. プロジェクト固有パス・コマンド定義

- **トップモジュール**: `tb_top` (RTL Core: `tb_top.u_core`)
- **主要クロック / リセット**: `tb_top.clk` / `tb_top.rst_n`
- **ユニット間I/F仕様書**: `docs/interfaces/*.md`
- **MCP LSPサーバー**: `systemverilog-lsp` (`definition`, `hover`, `references`, `diagnostics`)
- **Lint / Sim 実行（出力圧縮ラッパー経由）**:
  - 高速構文・Lintチェック: `make agent-lint` (生ログ退避先: `logs/lint.log`)
  - シミュレーション実行: `make agent-sim` (生ログ退避先: `logs/sim.log`)
- **コミットログ比較（QEMU期待値 Diff）**: `python3 scripts/qemu_diff.py logs/commit.log logs/qemu.log`
- **パイプライン可視化（Konata CLI）**: `./konata.sh --cli logs/trace.kanata.zst`
- **波形採取CLI（Verisium VWDB常駐デーモン連携）**: `python3 scripts/vwdb_cli.py`

---

## 1. コード探索の優先順位とトークン保護ルール（鉄則）

未知のモジュールや信号、型定義を調査する際は、以下の順序を厳守すること。**LSPツールを試す前に `grep` やファイルの全文読み込み（`Read` / `cat`）を実行してはならない。**

### 1.1 探索の3ステップ
1. **Step 1: SystemVerilog LSPの優先使用**
   - **モジュール・ポート宣言の取得 (`definition`)**: インスタンス化されているサブモジュールやインターフェースの定義を確認する場合は `definition` を呼び出し、返却された定義抜粋のみを参照する。
   - **型・ビット幅・パラメータの解決 (`hover`)**: `parameter` / `localparam` で計算されるビット幅、`typedef struct packed`、`enum`、および `import *_pkg::*;` でインポートされた外部定義を確認する場合は、パッケージファイルを遡って全文読込せず、対象シンボルに `hover` を実行して解決済みの型・ビット幅を取得する。
   - **結線・参照箇所の特定 (`references`)**: 特定の信号やモジュールの使用箇所を追跡する場合は `references` を使用する（ただし後述の禁止事項を厳守すること）。
2. **Step 2: 行範囲を指定した部分読み込み**
   - LSPや `trace-driver` からファイル名と行番号を取得した後、周辺の実装ロジック（`always_ff` / `always_comb` 等）を確認する場合は、ファイル全文ではなく**該当行の前後30〜60行のみ**を範囲指定して読み込む。
   - 同一モジュール内の複数の内部信号を調べる場合は、`hover` を10回以上連続実行するのではなく、該当ブロックの数十行を1回だけ部分読み込みする。
3. **Step 3: `grep` へのフォールバック（最終手段）**
   - まだインスタンス化されていない新規モジュールの探索、`ifdef` でLSPのコンパイル対象から外れている領域の探索、またはLSPが空の結果・エラーを返した場合に限り `grep` の使用を許可する。

### 1.2 トークン浪費を防ぐ禁止事項（ガードレール）
- **高頻度グローバル信号への `references` 禁止**: クロック（`clk` 等）、リセット（`rst_n` 等）、汎用ハンドシェイク信号名（`valid`, `ready` 等）に対してLSPの `references` を実行してはならない。
- **巨大ファイル・生成物の直接読み込み禁止**: 以下のファイル群に対して全文 `Read` や無制限の `grep` を行うことを禁止する。
  - シミュレーション生ログ・トレース・波形（`logs/*.log`, `*.kanata*`, `*.vwdb`, `*.vcd`, `*.fsdb`）
  - 論理合成ネットリスト・ビルド成果物・ベンダIP（`syn/`, ` netlist/`, `obj_dir/`, `xsim.dir/`, `work/`, `ip/`）
- **生コマンドの直接実行禁止**: Verilatorや商用シミュレータを直接コマンドラインから叩かず、必ず出力圧縮ラッパーが組み込まれた `make agent-lint` または `make agent-sim` を使用すること。

---

## 2. 波形採取CLI (`vwdb_cli.py`) の運用ルール

- **信号数の制限**: 1回の `slice` で指定する信号数は **最大 8〜10 個まで** とする。最初は制御信号（`valid`, `ready`, `state`, `wen`, `flush` 等）に絞って怪しいサイクルを特定し、その後データ信号（`addr`, `data`, `op` 等）をスライスすること。
- **サイクル窓の制限**: 1回の `slice` のサイクル幅（`--cycles`）は **原則 5〜15 サイクル** とする。長期間からイベント発生時刻を探す際は `slice` ではなく必ず `find` を使用すること。
- **再シミュレーション後のリロード**: `make agent-sim` で波形を更新した後は、常駐デーモンが古いDBハンドルを参照し続けないよう、必ず最初に `vwdb_cli.py reload` を実行すること。

```bash
# vwdb_cli.py 基本構文
python3 scripts/vwdb_cli.py reload
python3 scripts/vwdb_cli.py list --scope <階層パス>
python3 scripts/vwdb_cli.py find --sig <信号パス> --val <目標値>
python3 scripts/vwdb_cli.py slice --clk tb_top.clk --center <中心時刻> --cycles <前後サイクル数> --sigs <カンマ区切り信号名>
python3 scripts/vwdb_cli.py trace-driver --sig <信号パス> --time <時刻>
```

---

## 3. CPUトップダウン・デバッグワークフロー（Level 1 〜 Level 4）

テストの失敗（QEMU期待値不一致、Assertion Error、Timeout）が発生した場合は、いきなり波形やRTLをランダムに調査せず、必ず以下の **Level 1 → Level 4** の順序で「時間軸（サイクル）」と「空間軸（担当ユニット）」を絞り込むこと。

### Level 1: QEMU期待値比較による「最初の不一致命令」の特定 [命令の特定]
1. `python3 scripts/qemu_diff.py logs/commit.log logs/qemu.log` を実行し、リファレンス（QEMU）とRTLのコミット結果が最初に食い違った命令（First Mismatch Instruction）を特定する。
2. 以下の4点を特定して記録する：
   - 不一致命令の `Op ID`（または `Retire ID` / `ROB Index`）、`PC`、ニーモニック
   - 期待値（Expected）とRTL実測値（Actual）の差分（化けたレジスタ番号やメモリアドレス）
   - その命令のソースオペランドやメモリ値を生成した**直近の先行命令（Producer命令）**の `Op ID`

### Level 2: Konata CLIによる「ステージ通過時刻（サイクル窓）」の特定 [時間軸の絞り込み]
不一致が発覚したRetireサイクルではなく、**その誤った値が計算・転送された過去の実行サイクル**を特定する。
1. Konata CLIをワンショット形式で呼び出し、対象命令（`<Op_ID>`）とその前後の命令のパイプラインステージ履歴を取得する：
   ```bash
   printf "detail id <Op_ID>\nshow <Op_ID-10> <Op_ID+2>\nquit\n" | ./konata.sh --cli logs/trace.kanata.zst
   ```
2. 出力されたステージ遷移（`IF -> ID -> RN -> IS -> EX/MEM -> WB -> RET`）から以下を特定する：
   - 対象命令が演算やメモリアクセスを行ったステージ（`EX` / `MEM` 等）の**正確な開始〜終了サイクル（$T_{exec}$）**
   - そのサイクル帯におけるストール、キャッシュミス、またはフラッシュ再実行（Replay）の有無
   - Producer命令のWritebackサイクルと対象命令のIssueサイクルの位置関係（バイパス網経由か物理レジスタファイル経由か）

### Level 3: インターフェース仕様書に基づく「ユニット境界」の切り分け [空間軸の二分探索]
いきなりユニット内部の信号やRTLを見ず、Level 2で特定したサイクル $T_{exec}$ における**ユニット間の境界信号（Interface）**を確認して犯人ユニットを1つに特定する。
1. `docs/interfaces/` 配下の該当ユニット間インターフェース仕様書を `Read` し、境界信号名とハンドシェイク規約を確認する（構造体メンバのビット幅等はLSPの `hover` で確認する）。
2. `vwdb_cli.py slice` を実行し、時刻 $T_{exec}$ 前後の**対象ユニットの入力I/F信号と出力I/F信号のみ**を観測する。
3. **二分探索による判定**：
   - **入力I/Fの時点で既に値が誤っている場合**: バグは上流ユニットにある。インターフェース仕様書を辿って1つ前段のユニット境界（例：LSU入力が異常ならISU出力、ISU入力が異常ならRNU出力）へ移動し、Level 2のステージ通過時刻に合わせて再度境界スライスを行う。
   - **入力I/Fが仕様通り正しく、出力I/Fの値が誤っている（またはプロトコル違反がある）場合**: **このユニットの内部ロジックが犯人（Root Causeの所在）である**と確定する。Level 4へ進む。

### Level 4: ユニット内部のミクロ追跡（VWDB `trace-driver` ⇄ LSP の反復） [真因行の特定]
犯人ユニットと異常発生サイクル（$T_{bad}$）、誤った出力信号（$S_{bad}$）が確定したら、真因となるRTL行に到達するまで以下の **Step A → Step B → Step C** を再帰的に繰り返す。

- **Step A: ドライバー箇所の特定**
  `vwdb_cli.py trace-driver --sig <S_bad> --time <T_bad>` を実行し、時刻 $T_{bad}$ において $S_{bad}$ をドライブしているファイル名・行番号・インスタンス階層を取得する（`trace-driver` 対象外の信号はLSPの `definition` / `references` で代入元を特定する）。
- **Step B: 局所RTLの部分読み込みとLSPによる型・定数解決**
  特定されたファイルの該当行前後30〜50行のみを部分読み込みし、`always_ff` または `always_comb`（`assign`）の代入式を確認する。右辺の `struct packed` や `enum`、`parameter` はLSPの `hover` で正確なビット幅と定義値を確認する。
- **Step C: 右辺（入力信号）の波形検証と遡上判定**
  特定した代入式の右辺にある内部信号群を `vwdb_cli.py slice`（中心時刻 $T_{bad}$、前後5サイクル）で観測する：
  - **【判定1：Root Cause到達】** 右辺の入力信号はすべて正しいにもかかわらず、この代入式の演算・条件分岐・ビット幅切り出し・1サイクル遅延（FF受けの有無）のミスによって誤った左辺 $S_{bad}$ が生成されている → **セクション5（修正・検証手順）へ進む**。
  - **【判定2：さらに上流へ遡上】** 右辺の内部信号のいずれかが時刻 $T_{bad}$（FF出力なら1サイクル前の $T_{bad}-1$）で既に誤っている → その信号を新たな $S_{bad}$ とし、**Step Aへ戻ってさらに上流の論理へ遡る**。

---

## 4. バグ類型別の調査ショートカット（Playbooks）

- **不定値（`X`）伝播バグ**:
  テストベンチのエラー検知時刻で調査してはならない（既に回路全体に `X` が伝播しているため）。必ず `vwdb_cli.py find --sig <S_bad> --val X` で最初に `X` が発生した時刻 $T_x$ を求め、`trace-driver` で遡って「右辺に `X` がないのに左辺が `X` になっている最初の箇所（非リセットFF、未初期化配列、範囲外インデックス参照、マルチドライバー）」を特定すること。
- **パイプラインストール / デッドロック**:
  停止後の波形を何十サイクルもスライスしてはならない。Konata CLIの `find stage <STAGE> min 20` で詰まっている最古の命令（ヘッド命令）を特定するか、`ready` / `stall` 信号に対して `vwdb_cli.py find` を実行し、「最後に `1 -> 0`（または `0 -> 1`）に遷移して戻らなくなった最初のサイクル $T_{lock}$」の前後5サイクルだけをスライスすること。
- **1サイクルずれ（Off-by-one）とビット幅バグ**:
  `vwdb_cli.py slice` で制御信号（`valid`, `we`）とデータ信号を並べて確認し、`always_ff`（1サイクル遅延）と `always_comb`（0サイクル遅延）の混合ミスがないか、またLSPの `hover` で確認した左辺・右辺のビット幅と符号（`signed` / `unsigned`）に暗黙の切り捨てやゼロ拡張ミスがないかを照合すること。

---

## 5. RTL修正・検証プロトコル（Sign-off）

根本原因を特定した後、以下の手順で修正と検証を完了させること。

1. **根本原因の要約出力（コード編集前）**:
   コードを書き換える前に、①発生サイクルと波形の事実、②真因のファイル名・行番号、③バグのメカニズム、④修正方針の4点を簡潔に言語化する。
2. **最小差分のRTL編集**:
   特定した箇所のみを最小限の差分で編集する。
3. **LSP `diagnostics` による即時静的チェック（必須）**:
   シミュレータを回す前に、必ず編集したファイルに対してLSPの `diagnostics` を実行し、未定義信号、ポート幅・代入幅の不一致（Width mismatch）、ラッチ推論がゼロであることを確認する。
4. **Lintおよびシミュレーションの実行**:
   `make agent-lint` がPASSすることを確認した上で、`make agent-sim` を実行する。
5. **波形リロードと再検証**:
   テストがPASSした場合も、必要に応じて `vwdb_cli.py reload` を実行した上で、問題が発生していたサイクル帯の `slice` を1回実行し、該当信号が意図通りのタイミングで遷移していることを確認して完了とする。