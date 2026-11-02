# Phase noise in AnalogForge

ngspice has no periodic small-signal noise (`pnoise`) analysis, so the
open-source flow offers two estimates; the commercial backends can use their
native analyses.  Every result records which method produced it
(`extra.phase_noise_method`).

| option `phase_noise=` | method | cost | where |
|---|---|---|---|
| `leeson` (default) | Leeson's equation with simulated amplitude, power and tank Q | free | any backend |
| `isf` | impulse sensitivity function (Hajimiri & Lee) measured by transient impulse injection | 4·K extra transients (K = `isf_points`, default 12 → 48 runs, ~25 s on 4 cores) | any time-domain backend (ngspice) |
| `pnoise` | Spectre `pss`+`pnoise` / ADS harmonic balance | simulator | Spectre, ADS |

## ISF method

For a noise source located between nodes *a* and *b* (a tank resistor between
`outp` and AC ground, the channel of M1 between `outp` and `tail`):

1. The main transient gives the steady-state period T₀ and a reference rising
   zero crossing t_ref of v(outp)−v(outn).
2. For K phases x_k = 2πk/K a short trapezoidal current pulse (width T₀/50)
   of charge ±Δq is injected at t_ref + kT₀/K (Δq = 2 % of q_max by default).
3. After 8 periods the zero crossings of the +Δq and −Δq runs are compared;
   Δφ_k = −2πf₀·(t₊ − t₋)/2.  The symmetric difference cancels the numerical
   phase error of the time-step control and second-order effects.
4. h(x_k) = Δφ_k/Δq (= Γ(x_k)/q_max).

Phase noise in the 1/f² region, for white sources with single-sided PSD
S(x) that may depend on the oscillation phase (cyclostationary):

    L(Δf) = 10·log10( Σ_sources  mean_x[ h(x)² · S(x) ] / (2·(2πΔf)²) )

* tank: S = 4kT/R_P (stationary), counted twice (outp and outn);
* cross-coupled pair: S(x) = 4kTγ·g_m(t) with g_m(t) of M1 recorded during the
  transient (BSIM4 value from ngspice), counted twice (M1, M2); γ = option `gamma` (default 1).

For a sinusoidal ISF and tank noise only this reduces exactly to Leeson's
equation with F = 1 (unit test `test_isf_matches_leeson_for_ideal_tank`).

### Validation (see docs/validation.md)

* Γ_rms of the tank site = 0.718, theory for a quasi-sinusoidal LC oscillator 1/√2 = 0.707;
* doubling Δq leaves Γ unchanged to three decimals (linear regime);
* L(1 MHz) = −118.9 dBc/Hz (ISF) vs −119.5 dBc/Hz (Leeson, F = 2) for the default VCO;
  the ISF noise split (tank 45 %, pair 55 %) corresponds to F ≈ 2.2.

### Not included (limitations)

* flicker noise up-conversion (1/f³ region) — needs the DC value of the ISF and
  device flicker parameters; only the 1/f² region is computed;
* tail-current noise (second-harmonic up-conversion through the common mode),
  varactor and bias-resistor noise;
* the γ·g_m channel-noise model is a long-channel style approximation.

Treat ngspice numbers as estimates for comparisons and trends; use Spectre
`pnoise` or ADS HB for sign-off.

## Leeson model

    L(Δf) = 10·log10[ (2FkT/P_sig) · (1 + (f₀/(2Q·Δf))²) ]

with P_sig = A_diff²/(4R_P) (power dissipated in both tank halves), Q =
R_P/(2πf₀L) and F = 1 + γ unless `noise_factor` is given.

## FoM

    FoM   = 20·log10(f₀/Δf) − L(Δf) − 10·log10(P_DC / 1 mW)     (Δf = 1 MHz)
    FoM_T = FoM + 20·log10(TR[%] / 10)
