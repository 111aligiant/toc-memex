from __future__ import annotations

import pytest

from app.textnorm import normalize_text


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # Math delimiters are removed, content kept.
        (r"温度范围 $ -55 \pm 2 $ ℃", "温度范围 -55 ± 2 ℃"),
        (r"$ T_{e} $ 与 $ f_{1} $", "T_e 与 f_1"),
        (r"$ G^{{2}}/Hz $", "G^2/Hz"),
        (r"$^{\circ}$C", "°C"),
        (r"m^{3}/s", "m^3/s"),
        # Standard designations join the acronym subscript.
        (r"GJB_{4}.1—83", "GJB4.1—83"),
        (r"GB_{2423}.15", "GB2423.15"),
        # Standalone superscripts are footnote markers.
        (r"本标准规定了 $ ^{*} $总技术条件", "本标准规定了 总技术条件"),
        (r"见注 $ ^{a} $", "见注 "),
        # Commands without delimiters.
        (r"\pm 0.5", "± 0.5"),
        (r"\Delta T \le 3", "Δ T ≤ 3"),
        (r"\frac{1}{2}", "1/2"),
        (r"\sqrt{x}", "√x"),
        (r"\mathrm{dB}", "dB"),
        (r"\text{温度}", "温度"),
        # Literal escapes and spacing artifacts.
        ("a\\nHz", "a Hz"),
        (r"50\%", "50%"),
        # Unknown commands are left alone rather than deleted.
        (r"\unknown{x}", r"\unknown{x}"),
    ],
)
def test_normalize_math_artifacts(raw: str, expected: str):
    assert normalize_text(raw) == expected


def test_currency_is_not_treated_as_math():
    text = "价格 $5 - $10，另计运费"
    assert normalize_text(text) == text


def test_ordinary_text_is_unchanged():
    for text in ("1 范围", "本文件规定了温度试验的要求。", "a + b = c"):
        assert normalize_text(text) == text


@pytest.mark.parametrize(
    "raw",
    [
        r"$ -55 \pm 2 $",
        r"GJB_{4}.1—83",
        r"$ G^{{2}}/Hz $",
        r"$ ^{a} $",
        r"\frac{1}{2}",
        r"\unknown{x}",
    ],
)
def test_normalize_is_idempotent(raw: str):
    once = normalize_text(raw)
    assert normalize_text(once) == once


@pytest.mark.parametrize(
    "raw, expected",
    [
        # 2026-11-12 residual sweep: quantities, chemistry, formulas.
        ("$ 10 GHz $", "10 GHz"),
        ("$ 1ms $", "1ms"),
        ("$ 9 m/s $", "9 m/s"),
        ("$ 0.2m/s $", "0.2m/s"),
        ("$ NaCl $", "NaCl"),
        ("$ MgO $", "MgO"),
        ("$ A+B $", "A+B"),
        ("$ log/log $", "log/log"),
        ("$ T(-4.5dB) $", "T(-4.5dB)"),
        ("培养 $ 10 $d～ $ 21 $d。", "培养 10d～ 21d。"),
        ("$ (T $，单位为 °C)", "(T，单位为 °C)"),
        ("……:5000 $\n", "……:5000 \n"),
        ("$。在试件附近", "。在试件附近"),
        ("M_dB\sup", "M_dB^sup"),
        (r"N_\scriptsize~x^′", "N_ x^′"),
        (r"N,~\boldmath~β~,~\boldmath~γ~", "N, β, γ "),
        (r"101.33\Big((288-6.5H)/288\Big)^5.255", "101.33 ((288-6.5H)/288 )^5.255"),
        (r")^2\bigg/P_f", ")^2 /P_f"),
        ("+\\\\M_prRE", "+ M_prRE"),
    ],
)
def test_normalize_residual_artifacts(raw: str, expected: str):
    assert normalize_text(raw) == expected


@pytest.mark.parametrize(
    "raw, expected",
    [
        # 2026-11-13: font/width encoding damage in text-layer PDFs.
        ("犌犅／犜３８９２４．２—２０２０", "GB/T38924.2—2020"),
        ("０．２５ｋｇ～１５０ｋｇ", "0.25kg～150kg"),
        ("防水性 狑犪狋犲狉狆狉狅狅犳狀犲狊狊", "防水性 waterproofness"),
        ("２４ｈ", "24h"),
        ("犡、犢、犣 三个轴向", "X、Y、Z 三个轴向"),
        ("Ｈｚ ３＜犳狀≤１０", "Hz 3<fn≤10"),
        # Chinese punctuation and real ideographs stay untouched.
        ("中文标点：（１）结果≤３℃", "中文标点：（1）结果≤3℃"),
        ("环境、振动与冲击", "环境、振动与冲击"),
    ],
)
def test_normalize_encoding_artifacts(raw: str, expected: str):
    from app.textnorm import normalize_encoding_artifacts

    assert normalize_encoding_artifacts(raw) == expected


def test_query_side_normalization_meets_halfwidth_index():
    """A damaged query must find text that was normalized at ingest."""
    from app.textnorm import normalize_encoding_artifacts

    assert normalize_encoding_artifacts("犌犅犜３８９２４") == "GBT38924"
