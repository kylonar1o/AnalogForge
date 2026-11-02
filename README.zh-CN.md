# AnalogForge（中文说明）

**与仿真器无关的模拟/射频 IC 设计自动化平台（Python）**：电路只描述一次（Python 或 YAML），
在本地/云端用 **ngspice + SkyWater SKY130 开源 PDK** 仿真，在学校机器上用 **Cadence Spectre（OCEAN）**
或 **Keysight ADS** 仿真；自动提取振荡器与放大器指标，并行执行参数扫描、PVT 角点和优化，
所有结果存入 SQLite，并可在 Web 界面中提交任务、查看进度、比较结果。

英文主文档见 [README.md](README.md)；架构设计见 [docs/architecture.md](docs/architecture.md)；
验证数据见 [docs/validation.md](docs/validation.md)；局限性见 [docs/limitations.md](docs/limitations.md)。

## 验证状态（如实说明）

| 部分 | 状态 |
|---|---|
| ngspice + SKY130 后端、全部流程、CLI、Web | 已实现，pytest 用真实 ngspice-42 + SKY130 测试通过 |
| Spectre 网表 / OCEAN 脚本 / PSF 解析 | 已实现；用合成 PSF 文件和“假 spectre 可执行文件”做了单元测试；**尚未在真实 Spectre 上运行** |
| ADS 网表 / Keysight Python API 适配器 | 已实现；用假的 `keysight` 模块测试；**尚未在真实 ADS 中运行** |
| ngspice 下的相位噪声 | ISF（Hajimiri–Lee，脉冲注入瞬态仿真）与 Leeson 两种**估算**，只含 1/f² 区 |

## 安装

```bash
sudo apt install ngspice                 # macOS: brew install ngspice
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
analogforge pdk install sky130           # 从 PyPI 下载 Apache-2.0 的 SkyWater 模型（不修改）
analogforge doctor --full                # 依赖检查 + RC/VCO 冒烟仿真
```

## 快速上手

```bash
analogforge designs                                   # 设计列表
analogforge simulate lc_vco                           # 频率、幅度、功耗、调谐曲线、Leeson 相噪、FoM
analogforge simulate lc_vco -o phase_noise=isf -o pss=true --report out/vco   # ISF 相噪 + PSS 交叉验证
analogforge simulate two_stage_opamp -p CC=1.5p       # 增益、UGBW、相位裕度、增益裕度、压摆率、PSRR
analogforge sweep lc_vco --var VTUNE=0:1.8:7 -o tuning_points=0
analogforge corners lc_vco --process tt,ff,ss --voltage 0.9,1,1.1 --temperature -40,27,125
analogforge optimize two_stage_opamp --objective max:ugbw --var W1 --var CC --var IBIAS \
    --constraint "phase_margin >= 60" --method bayes --budget 30
analogforge serve                                     # Web 界面 http://127.0.0.1:8000
```

默认 VCO 的实测结果（摘自 docs/validation.md）：f_osc = 3.370 GHz（FFT 3.371 GHz，ngspice PSS 3.372 GHz），
差分幅度 0.89 V，功耗 4.36 mW，调谐范围 12.2 %，L(1 MHz) = −118.9 dBc/Hz（ISF）/ −119.5 dBc/Hz（Leeson），FoM ≈ 183 dB；
ISF 的 Γ_rms(tank) = 0.718，与理论值 1/√2 = 0.707 吻合。

## 接入 Cadence（学校机器）

详见 [docs/cadence_spectre.md](docs/cadence_spectre.md)：

1. 加载 Cadence 环境，`pip install -e .`，先运行 `python scripts/spectre_selftest.py --ocean` 自检。
2. 复制 `src/analogforge/pdks/template_institute.yaml` 到 `~/.analogforge/pdks/mypdk.yaml`，
   按 ADE 中的 Model Libraries 和器件实例语法填写模型路径、corner 段名和器件模板。
3. `analogforge simulate lc_vco --backend spectre --pdk mypdk`（直接调用 spectre，解析 PSF-ASCII）；
   或用 OCEAN 驱动已有的 ADE testbench：
   `--backend-opt mode=ocean --backend-opt cellview=MyLib/vco_tb/schematic`
   （参考 `examples/cadence/virtuoso_vco_tb.yaml`，参数名=ADE 设计变量）。
4. `-o phase_noise=pnoise` 使用 Spectre 原生 `pss + pnoise` 相位噪声。

## 接入 ADS

详见 [docs/keysight_ads.md](docs/keysight_ads.md)：把 AnalogForge 安装到 ADS 自带的 Python
（`$HPEESOF_DIR/tools/python/bin/python3 -m pip install --user -e .`），运行 `scripts/ads_selftest.py`；
推荐让 ADS 从原理图生成网表、AnalogForge 只替换 VAR 变量值
（`get_backend("ads", workspace=..., cellview=(lib, cell, "schematic"))`）。

## 面向后续课题（class-F、RTWO）

核心 IR 已包含互感 `MutualInductance`（变压器耦合 class-F VCO）和传输线 `TLine`（RTWO），
ISF 相噪流程（`analogforge/flows/isf.py`）与拓扑无关，可直接复用；见架构文档第 16 节。

## 测试

```bash
pytest -q                     # 92 个测试，4 核约 80 秒
python scripts/validate.py    # 由真实仿真重新生成 docs/validation.md
```
