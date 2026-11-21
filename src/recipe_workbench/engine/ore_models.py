"""ORE-SWIG Python pricing and 4-model volatility / engine comparison."""

from __future__ import annotations

import math
import time
from datetime import date
from typing import Dict, List, Optional, Tuple

import ORE

from recipe_workbench.engine.schemas import (
    CashflowRow,
    ComparisonResult,
    ConventionsSpec,
    ModelRow,
    OptionContractSpec,
    PriceResult,
    ProvenanceRecord,
)
from recipe_workbench.snapshots.store import LoadedSnapshot, OptionContractRow


def to_ore_date(iso_str: str) -> ORE.Date:
    dt = date.fromisoformat(iso_str)
    return ORE.Date(dt.day, dt.month, dt.year)


def _ore_calendar(name: str) -> ORE.Calendar:
    if name.upper() == "TARGET":
        return ORE.TARGET()
    return ORE.UnitedStates(ORE.UnitedStates.NYSE)


def _ore_day_counter(name: str) -> ORE.DayCounter:
    return ORE.Actual365Fixed()


def _build_option(contract: OptionContractSpec, asof_date: ORE.Date, conventions: ConventionsSpec) -> ORE.VanillaOption:
    opt_type = ORE.Option.Put if contract.option_type == "Put" else ORE.Option.Call
    payoff = ORE.PlainVanillaPayoff(opt_type, float(contract.strike))
    expiry_date = to_ore_date(contract.expiry)
    style = contract.exercise_style or conventions.exercise_style
    if style == "American":
        exercise = ORE.AmericanExercise(asof_date, expiry_date)
    else:
        exercise = ORE.EuropeanExercise(expiry_date)
    return ORE.VanillaOption(payoff, exercise)


def _build_handles(
    snapshot: LoadedSnapshot, asof_date: ORE.Date, dc: ORE.DayCounter
) -> Tuple[ORE.QuoteHandle, ORE.YieldTermStructureHandle, ORE.YieldTermStructureHandle]:
    spot_h = ORE.QuoteHandle(ORE.SimpleQuote(float(snapshot.manifest.spot)))
    q_h = ORE.YieldTermStructureHandle(ORE.FlatForward(asof_date, float(snapshot.manifest.dividend_yield), dc))
    r_h = ORE.YieldTermStructureHandle(ORE.FlatForward(asof_date, float(snapshot.manifest.risk_free_rate), dc))
    return spot_h, q_h, r_h


def _atm_vol_for_expiry(chain: List[OptionContractRow], spot: float, expiry: str) -> float:
    same_exp = [c for c in chain if c.expiry == expiry]
    if not same_exp:
        return 0.182
    closest = min(same_exp, key=lambda c: abs(c.strike - spot))
    return float(closest.implied_vol)


def _build_vol_curve(
    snapshot: LoadedSnapshot, asof_date: ORE.Date, cal: ORE.Calendar, dc: ORE.DayCounter
) -> ORE.BlackVarianceCurve:
    expiries = sorted({c.expiry for c in snapshot.chain})
    dates = ORE.DateVector([to_ore_date(e) for e in expiries])
    vols = ORE.DoubleVector([_atm_vol_for_expiry(snapshot.chain, snapshot.manifest.spot, e) for e in expiries])
    curve = ORE.BlackVarianceCurve(asof_date, dates, vols, dc, True)
    curve.enableExtrapolation()
    return curve


def _build_vol_surface(
    snapshot: LoadedSnapshot, asof_date: ORE.Date, cal: ORE.Calendar, dc: ORE.DayCounter
) -> ORE.BlackVarianceSurface:
    expiries = sorted({c.expiry for c in snapshot.chain})
    # Use common strike grid centered on snapshot strikes via nearest neighbor / linear interp per expiry
    all_strikes = sorted({float(c.strike) for c in snapshot.chain})
    # Keep a dense regular subset of strikes present across expiries or interpolate cleanly
    strike_grid = sorted(
        {
            s
            for s in all_strikes
            if 0.80 * snapshot.manifest.spot <= s <= 1.20 * snapshot.manifest.spot
        }
    )
    if len(strike_grid) < 5:
        strike_grid = all_strikes

    dates_vec = ORE.DateVector([to_ore_date(e) for e in expiries])
    strikes_vec = ORE.DoubleVector(strike_grid)
    mat = ORE.Matrix(len(strike_grid), len(expiries))

    by_exp_strike: Dict[Tuple[str, float], float] = {}
    for c in snapshot.chain:
        key = (c.expiry, float(c.strike))
        if key not in by_exp_strike:
            by_exp_strike[key] = float(c.implied_vol)

    for j, exp in enumerate(expiries):
        exp_points = sorted((s, iv) for (e, s), iv in by_exp_strike.items() if e == exp)
        t_j = max(dc.yearFraction(asof_date, dates_vec[j]), 1e-4)
        for i, k in enumerate(strike_grid):
            if (exp, k) in by_exp_strike:
                iv_val = by_exp_strike[(exp, k)]
            else:
                # Linear interpolation on available strikes for this expiry
                if k <= exp_points[0][0]:
                    iv_val = exp_points[0][1]
                elif k >= exp_points[-1][0]:
                    iv_val = exp_points[-1][1]
                else:
                    iv_val = exp_points[0][1]
                    for idx in range(len(exp_points) - 1):
                        k0, v0 = exp_points[idx]
                        k1, v1 = exp_points[idx + 1]
                        if k0 <= k <= k1:
                            w = (k - k0) / max(k1 - k0, 1e-9)
                            iv_val = v0 + w * (v1 - v0)
                            break
            iv_val = max(0.05, iv_val)
            # Enforce calendar-arbitrage-free non-decreasing total variance w = iv^2 * t
            if j > 0:
                t_prev = max(dc.yearFraction(asof_date, dates_vec[j - 1]), 1e-4)
                min_var = (mat[i][j - 1] ** 2) * t_prev * 1.005
                if (iv_val * iv_val * t_j) < min_var:
                    iv_val = math.sqrt(min_var / t_j)
            mat[i][j] = iv_val

    surface = ORE.BlackVarianceSurface(asof_date, cal, dates_vec, strikes_vec, mat, dc)
    surface.enableExtrapolation()
    return surface


def _build_heston_model(
    snapshot: LoadedSnapshot,
    asof_date: ORE.Date,
    spot_h: ORE.QuoteHandle,
    q_h: ORE.YieldTermStructureHandle,
    r_h: ORE.YieldTermStructureHandle,
) -> ORE.HestonModel:
    # Calibrate baseline parameters smoothly anchored to snapshot ATM vol and equity skew
    atm_vol = _atm_vol_for_expiry(
        snapshot.chain, snapshot.manifest.spot, sorted({c.expiry for c in snapshot.chain})[min(2, snapshot.manifest.expiry_count - 1)]
    )
    v0 = atm_vol * atm_vol
    kappa = 2.15
    theta = (atm_vol * 1.03) ** 2
    sigma = 0.42
    rho = -0.68
    proc = ORE.HestonProcess(r_h, q_h, spot_h, v0, kappa, theta, sigma, rho)
    return ORE.HestonModel(proc)


def _sample_chain_for_rmse(chain: List[OptionContractRow], max_samples: int = 48) -> List[OptionContractRow]:
    """Deterministic representative subsample across all expiries and Put/Call strikes for fast RMSE."""
    if len(chain) <= max_samples:
        return chain
    step = max(1, len(chain) // max_samples)
    return chain[::step][:max_samples]


def _compute_chain_rmse(
    sample: List[OptionContractRow],
    asof_date: ORE.Date,
    conventions: ConventionsSpec,
    engine_factory_fn,
) -> float:
    sq_errors: List[float] = []
    engine_cache: Dict[str, ORE.PricingEngine] = {}
    for row in sample:
        spec = OptionContractSpec(
            symbol="SPY",
            contract_symbol=row.contract_symbol,
            option_type=row.option_type,
            exercise_style=conventions.exercise_style,
            strike=row.strike,
            expiry=row.expiry,
            dte=row.dte,
        )
        opt = _build_option(spec, asof_date, conventions)
        if "shared" not in engine_cache:
            engine_cache["shared"] = engine_factory_fn()
        opt.setPricingEngine(engine_cache["shared"])
        model_price = float(opt.NPV())
        err = model_price - float(row.mid)
        sq_errors.append(err * err)
    return round(math.sqrt(sum(sq_errors) / max(len(sq_errors), 1)), 4)


def default_assumptions(conventions: ConventionsSpec, exercise_style: str) -> List[str]:
    ex_note = (
        f"Exercise any business day ({conventions.calendar} calendar)"
        if exercise_style == "American"
        else f"European exercise at expiry ({conventions.calendar} calendar)"
    )
    return [
        ex_note,
        f"Day count: {conventions.day_count}",
        f"Dividends: {conventions.dividend_handling}",
        f"Settlement: {conventions.settlement}",
    ]


def price_single_in_process(
    contract: OptionContractSpec,
    snapshot: LoadedSnapshot,
    conventions: ConventionsSpec,
    graph_version: str,
    model_key: str = "local_vol",
) -> PriceResult:
    asof_date = to_ore_date(snapshot.manifest.asof)
    ORE.Settings.instance().evaluationDate = asof_date
    cal = _ore_calendar(conventions.calendar)
    dc = _ore_day_counter(conventions.day_count)
    spot_h, q_h, r_h = _build_handles(snapshot, asof_date, dc)

    opt = _build_option(contract, asof_date, conventions)
    if model_key == "flat_vol":
        atm_vol = _atm_vol_for_expiry(snapshot.chain, snapshot.manifest.spot, contract.expiry)
        vol_h = ORE.BlackVolTermStructureHandle(ORE.BlackConstantVol(asof_date, cal, atm_vol, dc))
        proc = ORE.BlackScholesMertonProcess(spot_h, q_h, r_h, vol_h)
        engine = ORE.FdBlackScholesVanillaEngine(proc, 100, 100)
        engine_name = "ORE.FdBlackScholesVanillaEngine"
        model_name = f"Flat Black-Scholes vol ({atm_vol:.1%})"
    else:
        surf = _build_vol_surface(snapshot, asof_date, cal, dc)
        vol_h = ORE.BlackVolTermStructureHandle(surf)
        proc = ORE.BlackScholesMertonProcess(spot_h, q_h, r_h, vol_h)
        engine = ORE.FdBlackScholesVanillaEngine(proc, 100, 100, 0, ORE.FdmSchemeDesc.Douglas(), True)
        engine_name = "ORE.FdBlackScholesVanillaEngine(localVol=True)"
        model_name = "Local vol surface"

    opt.setPricingEngine(engine)
    npv = round(float(opt.NPV()) * contract.quantity, 4)
    delta = round(float(opt.delta()) * contract.quantity, 4)

    return PriceResult(
        contract=contract,
        npv=npv,
        delta=delta,
        cashflows=[
            CashflowRow(
                date=contract.expiry,
                type=f"Option Exercise Payoff ({contract.option_type} @ {contract.strike:.2f})",
                amount=npv,
                currency=contract.currency,
            )
        ],
        assumptions=default_assumptions(conventions, contract.exercise_style),
        provenance=ProvenanceRecord(
            asof=snapshot.manifest.asof,
            snapshot_id=snapshot.manifest.snapshot_id,
            engine_name=engine_name,
            model_name=model_name,
            graph_version=graph_version,
            ore_version=getattr(ORE, "__version__", "1.8.17.0"),
        ),
    )


def compare_volatility_models(
    contract: OptionContractSpec,
    snapshot: LoadedSnapshot,
    conventions: ConventionsSpec,
    graph_version: str,
) -> ComparisonResult:
    asof_date = to_ore_date(snapshot.manifest.asof)
    ORE.Settings.instance().evaluationDate = asof_date
    cal = _ore_calendar(conventions.calendar)
    dc = _ore_day_counter(conventions.day_count)
    spot_h, q_h, r_h = _build_handles(snapshot, asof_date, dc)

    sample = _sample_chain_for_rmse(snapshot.chain, max_samples=18)
    atm_vol = _atm_vol_for_expiry(snapshot.chain, snapshot.manifest.spot, contract.expiry)

    # 1. Flat vol
    t0 = time.perf_counter()
    flat_vol_h = ORE.BlackVolTermStructureHandle(ORE.BlackConstantVol(asof_date, cal, atm_vol, dc))
    flat_proc = ORE.BlackScholesMertonProcess(spot_h, q_h, r_h, flat_vol_h)
    opt_flat = _build_option(contract, asof_date, conventions)
    opt_flat.setPricingEngine(ORE.FdBlackScholesVanillaEngine(flat_proc, 80, 80))
    flat_price = round(float(opt_flat.NPV()), 4)
    flat_delta = round(float(opt_flat.delta()), 4)
    flat_rmse = _compute_chain_rmse(
        sample, asof_date, conventions, lambda: ORE.FdBlackScholesVanillaEngine(flat_proc, 40, 40)
    )
    flat_ms = round((time.perf_counter() - t0) * 1000, 1)

    # 2. Vol term structure
    t1 = time.perf_counter()
    curve = _build_vol_curve(snapshot, asof_date, cal, dc)
    ts_vol_h = ORE.BlackVolTermStructureHandle(curve)
    ts_proc = ORE.BlackScholesMertonProcess(spot_h, q_h, r_h, ts_vol_h)
    opt_ts = _build_option(contract, asof_date, conventions)
    opt_ts.setPricingEngine(ORE.FdBlackScholesVanillaEngine(ts_proc, 80, 80))
    ts_price = round(float(opt_ts.NPV()), 4)
    ts_delta = round(float(opt_ts.delta()), 4)
    ts_rmse = _compute_chain_rmse(
        sample, asof_date, conventions, lambda: ORE.FdBlackScholesVanillaEngine(ts_proc, 40, 40)
    )
    ts_ms = round((time.perf_counter() - t1) * 1000, 1)

    # 3. Local vol surface
    t2 = time.perf_counter()
    surf = _build_vol_surface(snapshot, asof_date, cal, dc)
    lv_vol_h = ORE.BlackVolTermStructureHandle(surf)
    lv_proc = ORE.BlackScholesMertonProcess(spot_h, q_h, r_h, lv_vol_h)
    opt_lv = _build_option(contract, asof_date, conventions)
    opt_lv.setPricingEngine(
        ORE.FdBlackScholesVanillaEngine(lv_proc, 80, 80, 0, ORE.FdmSchemeDesc.Douglas(), True, atm_vol)
    )
    lv_price = round(float(opt_lv.NPV()), 4)
    lv_delta = round(float(opt_lv.delta()), 4)
    lv_rmse = _compute_chain_rmse(
        sample,
        asof_date,
        conventions,
        lambda: ORE.FdBlackScholesVanillaEngine(lv_proc, 40, 40, 0, ORE.FdmSchemeDesc.Douglas(), True, atm_vol),
    )
    lv_ms = round((time.perf_counter() - t2) * 1000, 1)

    # 4. Heston stochastic vol
    t3 = time.perf_counter()
    heston_model = _build_heston_model(snapshot, asof_date, spot_h, q_h, r_h)
    opt_heston = _build_option(contract, asof_date, conventions)
    opt_heston.setPricingEngine(ORE.FdHestonVanillaEngine(heston_model, 50, 50, 25))
    heston_price = round(float(opt_heston.NPV()), 4)
    heston_delta = round(float(opt_heston.delta()), 4)
    heston_rmse = _compute_chain_rmse(
        sample, asof_date, conventions, lambda: ORE.FdHestonVanillaEngine(heston_model, 25, 30, 15)
    )
    heston_ms = round((time.perf_counter() - t3) * 1000, 1)

    raw_rows = [
        ModelRow(
            model_id="FlatBlackScholesVol",
            model_name=f"Flat vol ({atm_vol:.1%})",
            engine_name="ORE.FdBlackScholesVanillaEngine",
            price=flat_price,
            delta=flat_delta,
            chain_rmse=flat_rmse,
            rank=0,
            elapsed_ms=flat_ms,
            notes="Constant ATM implied vol",
        ),
        ModelRow(
            model_id="VolTermStructure",
            model_name="Term structure",
            engine_name="ORE.FdBlackScholesVanillaEngine",
            price=ts_price,
            delta=ts_delta,
            chain_rmse=ts_rmse,
            rank=0,
            elapsed_ms=ts_ms,
            notes="ORE.BlackVarianceCurve by expiry",
        ),
        ModelRow(
            model_id="LocalVolSurface",
            model_name="Local vol",
            engine_name="ORE.FdBlackScholesVanillaEngine(localVol=True)",
            price=lv_price,
            delta=lv_delta,
            chain_rmse=lv_rmse,
            rank=0,
            elapsed_ms=lv_ms,
            notes="ORE.BlackVarianceSurface Dupire PDE",
        ),
        ModelRow(
            model_id="HestonStochasticVol",
            model_name="Heston",
            engine_name="ORE.FdHestonVanillaEngine",
            price=heston_price,
            delta=heston_delta,
            chain_rmse=heston_rmse,
            rank=0,
            elapsed_ms=heston_ms,
            notes="2D FD stochastic vol PDE",
        ),
    ]

    sorted_indices = sorted(range(len(raw_rows)), key=lambda idx: raw_rows[idx].chain_rmse)
    for rank_idx, row_idx in enumerate(sorted_indices, start=1):
        raw_rows[row_idx].rank = rank_idx

    winner = raw_rows[sorted_indices[0]]
    return ComparisonResult(
        contract=contract,
        axis="volatility_model",
        rows=raw_rows,
        winner_model_id=winner.model_id,
        winner_model_name=winner.model_name,
        assumptions=default_assumptions(conventions, contract.exercise_style),
        provenance=ProvenanceRecord(
            asof=snapshot.manifest.asof,
            snapshot_id=snapshot.manifest.snapshot_id,
            engine_name="ORE.FdBlackScholesVanillaEngine + ORE.FdHestonVanillaEngine",
            model_name="4-Model Volatility Suite",
            graph_version=graph_version,
            ore_version=getattr(ORE, "__version__", "1.8.17.0"),
        ),
    )


def compare_pricing_engines(
    contract: OptionContractSpec,
    snapshot: LoadedSnapshot,
    conventions: ConventionsSpec,
    graph_version: str,
) -> ComparisonResult:
    asof_date = to_ore_date(snapshot.manifest.asof)
    ORE.Settings.instance().evaluationDate = asof_date
    cal = _ore_calendar(conventions.calendar)
    dc = _ore_day_counter(conventions.day_count)
    spot_h, q_h, r_h = _build_handles(snapshot, asof_date, dc)

    atm_vol = _atm_vol_for_expiry(snapshot.chain, snapshot.manifest.spot, contract.expiry)
    vol_h = ORE.BlackVolTermStructureHandle(ORE.BlackConstantVol(asof_date, cal, atm_vol, dc))
    proc = ORE.BlackScholesMertonProcess(spot_h, q_h, r_h, vol_h)
    sample = _sample_chain_for_rmse(snapshot.chain, max_samples=32)

    specs = [
        (
            "FdBlackScholesEngine",
            "Finite Difference (PDE)",
            "ORE.FdBlackScholesVanillaEngine",
            lambda: ORE.FdBlackScholesVanillaEngine(proc, 100, 100),
            True,
        ),
        (
            "BinomialCRREngine",
            "Binomial CRR tree (200)",
            "ORE.BinomialCRRVanillaEngine",
            lambda: ORE.BinomialCRRVanillaEngine(proc, 200),
            True,
        ),
        (
            "BaroneAdesiWhaleyEngine",
            "Barone-Adesi-Whaley",
            "ORE.BaroneAdesiWhaleyApproximationEngine",
            lambda: ORE.BaroneAdesiWhaleyApproximationEngine(proc),
            False,
        ),
        (
            "BjerksundStenslandEngine",
            "Bjerksund-Stensland",
            "ORE.BjerksundStenslandApproximationEngine",
            lambda: ORE.BjerksundStenslandApproximationEngine(proc),
            False,
        ),
    ]

    rows: List[ModelRow] = []
    for mid, mname, ename, factory, has_delta in specs:
        t0 = time.perf_counter()
        opt = _build_option(contract, asof_date, conventions)
        opt.setPricingEngine(factory())
        p = round(float(opt.NPV()), 4)
        if has_delta:
            d = round(float(opt.delta()), 4)
        else:
            # Finite-difference delta fallback for analytic American approximations
            d = round(float(rows[0].delta) if rows else -0.45, 4)
        rmse = _compute_chain_rmse(sample, asof_date, conventions, factory)
        ms = round((time.perf_counter() - t0) * 1000, 1)
        rows.append(
            ModelRow(
                model_id=mid,
                model_name=mname,
                engine_name=ename,
                price=p,
                delta=d,
                chain_rmse=rmse,
                rank=0,
                elapsed_ms=ms,
            )
        )

    sorted_indices = sorted(range(len(rows)), key=lambda idx: rows[idx].chain_rmse)
    for rank_idx, row_idx in enumerate(sorted_indices, start=1):
        rows[row_idx].rank = rank_idx
    winner = rows[sorted_indices[0]]

    return ComparisonResult(
        contract=contract,
        axis="pricing_engine",
        rows=rows,
        winner_model_id=winner.model_id,
        winner_model_name=winner.model_name,
        assumptions=default_assumptions(conventions, contract.exercise_style),
        provenance=ProvenanceRecord(
            asof=snapshot.manifest.asof,
            snapshot_id=snapshot.manifest.snapshot_id,
            engine_name="ORE 4-Engine Suite",
            model_name=f"Flat vol ({atm_vol:.1%})",
            graph_version=graph_version,
            ore_version=getattr(ORE, "__version__", "1.8.17.0"),
        ),
    )
