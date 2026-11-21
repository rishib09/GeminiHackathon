"""Real ORE.OREApp XML batch runner for ORE Trade disambiguation path."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import List

import ORE
import pandas as pd

from recipe_workbench.engine.schemas import (
    CashflowRow,
    ConventionsSpec,
    OptionContractSpec,
    PriceResult,
    ProvenanceRecord,
)
from recipe_workbench.snapshots.store import LoadedSnapshot


def _ore_date_str(iso_str: str) -> str:
    return iso_str


def run_ore_app_trade(
    contract: OptionContractSpec,
    snapshot: LoadedSnapshot,
    conventions: ConventionsSpec,
    graph_version: str,
    run_dir: Path | str = Path("ore_runs/latest"),
) -> PriceResult:
    """Emit ORE XML inputs and run ORE.OREApp in-process to produce NPV and Cashflow reports."""
    base = Path(run_dir).resolve()
    input_dir = base / "Input"
    output_dir = base / "Output"
    input_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)

    asof = snapshot.manifest.asof
    eq_name = contract.symbol.upper()
    ccy = contract.currency.upper()
    style = contract.exercise_style or conventions.exercise_style

    # Choose matching ATM vol from snapshot
    same_exp = [c for c in snapshot.chain if c.expiry == contract.expiry]
    atm_vol = (
        min(same_exp, key=lambda c: abs(c.strike - snapshot.manifest.spot)).implied_vol
        if same_exp
        else 0.182
    )

    # 1. portfolio.xml
    portfolio_xml = f"""<?xml version="1.0"?>
<Portfolio>
  <Trade id="{contract.contract_symbol}">
    <TradeType>EquityOption</TradeType>
    <Envelope>
      <Counterparty>CPTY_RECIPE</Counterparty>
      <NettingSetId>NS_RECIPE</NettingSetId>
      <AdditionalFields/>
    </Envelope>
    <EquityOptionData>
      <OptionData>
        <LongShort>Long</LongShort>
        <OptionType>{contract.option_type}</OptionType>
        <Style>{style}</Style>
        <Settlement>{conventions.settlement}</Settlement>
        <ExerciseDates>
          <ExerciseDate>{contract.expiry}</ExerciseDate>
        </ExerciseDates>
      </OptionData>
      <Name>{eq_name}</Name>
      <Currency>{ccy}</Currency>
      <Strike>{contract.strike:.4f}</Strike>
      <Quantity>{contract.quantity:.4f}</Quantity>
    </EquityOptionData>
  </Trade>
</Portfolio>
"""
    (input_dir / "portfolio.xml").write_text(portfolio_xml, encoding="utf-8")

    # 2. pricingengine.xml
    pricingengine_xml = """<?xml version="1.0"?>
<PricingEngines>
  <Product type="EquityOption">
    <Model>BlackScholesMerton</Model>
    <ModelParameters/>
    <Engine>AnalyticEuropeanEngine</Engine>
    <EngineParameters/>
  </Product>
  <Product type="EquityOptionAmerican">
    <Model>BlackScholesMerton</Model>
    <ModelParameters/>
    <Engine>FdBlackScholesVanillaEngine</Engine>
    <EngineParameters>
      <Parameter name="Scheme">Douglas</Parameter>
      <Parameter name="TimeGridPerYear">100</Parameter>
      <Parameter name="XGrid">100</Parameter>
      <Parameter name="DampingSteps">0</Parameter>
      <Parameter name="TimeGridMinimumSize">20</Parameter>
      <Parameter name="ExerciseTimeStepsPerYear">24</Parameter>
      <Parameter name="SolverType">FD</Parameter>
    </EngineParameters>
  </Product>
</PricingEngines>
"""
    (input_dir / "pricingengine.xml").write_text(pricingengine_xml, encoding="utf-8")

    # 3. todaysmarket.xml
    todaysmarket_xml = f"""<?xml version="1.0"?>
<TodaysMarket>
  <Configuration id="default">
    <DiscountingCurvesId>default</DiscountingCurvesId>
    <EquityCurvesId>default</EquityCurvesId>
    <EquityVolatilitiesId>default</EquityVolatilitiesId>
  </Configuration>
  <DiscountingCurves id="default">
    <DiscountingCurve currency="{ccy}">Yield/{ccy}/{ccy}-DISC</DiscountingCurve>
  </DiscountingCurves>
  <EquityCurves id="default">
    <EquityCurve name="{eq_name}">Equity/{ccy}/{eq_name}</EquityCurve>
  </EquityCurves>
  <EquityVolatilities id="default">
    <EquityVolatility name="{eq_name}">EquityVolatility/{ccy}/{eq_name}</EquityVolatility>
  </EquityVolatilities>
</TodaysMarket>
"""
    (input_dir / "todaysmarket.xml").write_text(todaysmarket_xml, encoding="utf-8")

    # 4. curveconfig.xml
    curveconfig_xml = f"""<?xml version="1.0"?>
<CurveConfiguration>
  <YieldCurves>
    <YieldCurve>
      <CurveId>{ccy}-DISC</CurveId>
      <CurveDescription>Flat USD Risk-Free Discount Curve</CurveDescription>
      <Currency>{ccy}</Currency>
      <DiscountCurve>{ccy}-DISC</DiscountCurve>
      <Segments>
        <Simple>
          <Type>Deposit</Type>
          <Quotes>
            <Quote>MM/RATE/{ccy}/0D/3M</Quote>
          </Quotes>
          <Conventions>{ccy}-DEPOSIT</Conventions>
        </Simple>
      </Segments>
    </YieldCurve>
  </YieldCurves>
  <EquityCurves>
    <EquityCurve>
      <CurveId>{eq_name}</CurveId>
      <CurveDescription>{eq_name} Forward Curve</CurveDescription>
      <Currency>{ccy}</Currency>
      <ForecastingCurve>{ccy}-DISC</ForecastingCurve>
      <Type>DividendYield</Type>
      <SpotQuote>EQUITY/PRICE/{eq_name}/{ccy}</SpotQuote>
      <Quotes>
        <Quote>EQUITY_DIVIDEND/RATE/{eq_name}/{ccy}/1Y</Quote>
      </Quotes>
      <DayCounter>A365</DayCounter>
    </EquityCurve>
  </EquityCurves>
  <EquityVolatilities>
    <EquityVolatility>
      <CurveId>{eq_name}</CurveId>
      <CurveDescription>{eq_name} Volatility</CurveDescription>
      <Currency>{ccy}</Currency>
      <Dimension>ATM</Dimension>
      <DayCounter>A365</DayCounter>
      <Calendar>US</Calendar>
      <Expiries>1M, 3M, 6M</Expiries>
    </EquityVolatility>
  </EquityVolatilities>
</CurveConfiguration>
"""
    (input_dir / "curveconfig.xml").write_text(curveconfig_xml, encoding="utf-8")

    # 5. conventions.xml
    conventions_xml = f"""<?xml version="1.0"?>
<Conventions>
  <Deposit>
    <Id>{ccy}-DEPOSIT</Id>
    <IndexBased>false</IndexBased>
    <Calendar>US</Calendar>
    <Convention>ModifiedFollowing</Convention>
    <EOM>false</EOM>
    <DayCounter>A365</DayCounter>
    <SettlementDays>0</SettlementDays>
  </Deposit>
</Conventions>
"""
    (input_dir / "conventions.xml").write_text(conventions_xml, encoding="utf-8")

    # 6. marketdata.txt + fixings.txt
    market_lines = [
        f"{asof} MM/RATE/{ccy}/0D/3M {snapshot.manifest.risk_free_rate:.6f}",
        f"{asof} EQUITY/PRICE/{eq_name}/{ccy} {snapshot.manifest.spot:.4f}",
        f"{asof} EQUITY_DIVIDEND/RATE/{eq_name}/{ccy}/1Y {snapshot.manifest.dividend_yield:.6f}",
        f"{asof} EQUITY_OPTION/RATE_LNVOL/{eq_name}/{ccy}/1M/ATMF {atm_vol:.6f}",
        f"{asof} EQUITY_OPTION/RATE_LNVOL/{eq_name}/{ccy}/3M/ATMF {atm_vol:.6f}",
        f"{asof} EQUITY_OPTION/RATE_LNVOL/{eq_name}/{ccy}/6M/ATMF {atm_vol:.6f}",
    ]
    (input_dir / "marketdata.txt").write_text("\n".join(market_lines) + "\n", encoding="utf-8")
    (input_dir / "fixings.txt").write_text("", encoding="utf-8")

    # 7. ore.xml
    ore_xml = f"""<?xml version="1.0"?>
<ORE>
  <Setup>
    <Parameter name="asofDate">{asof}</Parameter>
    <Parameter name="inputPath">{input_dir.as_posix()}</Parameter>
    <Parameter name="outputPath">{output_dir.as_posix()}</Parameter>
    <Parameter name="logFile">ore.log</Parameter>
    <Parameter name="logMask">31</Parameter>
    <Parameter name="marketDataFile">marketdata.txt</Parameter>
    <Parameter name="fixingDataFile">fixings.txt</Parameter>
    <Parameter name="implyTodaysFixings">N</Parameter>
    <Parameter name="curveConfigFile">curveconfig.xml</Parameter>
    <Parameter name="conventionsFile">conventions.xml</Parameter>
    <Parameter name="marketConfigFile">todaysmarket.xml</Parameter>
    <Parameter name="pricingEnginesFile">pricingengine.xml</Parameter>
    <Parameter name="portfolioFile">portfolio.xml</Parameter>
    <Parameter name="observationModel">None</Parameter>
  </Setup>
  <Markets>
    <Parameter name="lgmcalibration">default</Parameter>
    <Parameter name="fxcalibration">default</Parameter>
    <Parameter name="eqcalibration">default</Parameter>
    <Parameter name="pricing">default</Parameter>
    <Parameter name="simulation">default</Parameter>
  </Markets>
  <Analytics>
    <Analytic type="npv">
      <Parameter name="active">Y</Parameter>
      <Parameter name="baseCurrency">{ccy}</Parameter>
      <Parameter name="outputFileName">npv.csv</Parameter>
    </Analytic>
    <Analytic type="cashflow">
      <Parameter name="active">Y</Parameter>
      <Parameter name="outputFileName">flows.csv</Parameter>
    </Analytic>
  </Analytics>
</ORE>
"""
    ore_xml_path = input_dir / "ore.xml"
    ore_xml_path.write_text(ore_xml, encoding="utf-8")

    params = ORE.Parameters()
    params.fromFile(str(ore_xml_path))
    app = ORE.OREApp(params, False)
    app.run()

    npv_file = output_dir / "npv.csv"
    flows_file = output_dir / "flows.csv"

    npv_val: float | None = None
    if npv_file.exists():
        df_npv = pd.read_csv(npv_file)
        if not df_npv.empty:
            for col in ("NPV", "NPV(Base)", "#NPV"):
                if col in df_npv.columns:
                    npv_val = round(float(df_npv[col].iloc[0]), 4)
                    break

    if npv_val is None:
        log_path = output_dir / "ore.log"
        tail = log_path.read_text(encoding="utf-8", errors="ignore")[-1500:] if log_path.exists() else ""
        raise RuntimeError(f"OREApp.run() did not produce npv.csv. Log tail:\n{tail}")

    cashflows: List[CashflowRow] = []
    if flows_file.exists() and flows_file.stat().st_size > 0:
        df_flows = pd.read_csv(flows_file)
        for _, row in df_flows.iterrows():
            cashflows.append(
                CashflowRow(
                    date=str(row.get("PayDate", contract.expiry)),
                    type=str(row.get("FlowType", "OptionExercise")),
                    amount=round(float(row.get("Amount", npv_val)), 4),
                    currency=str(row.get("Currency", ccy)),
                )
            )
    if not cashflows:
        cashflows.append(
            CashflowRow(
                date=contract.expiry,
                type=f"ORE EquityOption ({style} {contract.option_type})",
                amount=npv_val,
                currency=ccy,
            )
        )

    return PriceResult(
        contract=contract,
        npv=npv_val,
        delta=None,
        cashflows=cashflows,
        assumptions=[
            f"OREApp XML TradeType=EquityOption (Style={style}, {conventions.calendar} calendar)",
            f"Day count: {conventions.day_count}",
            f"Discounting: {ccy}-DISC from MM/RATE/{ccy}/3M",
            f"Settlement: {conventions.settlement}",
        ],
        provenance=ProvenanceRecord(
            asof=asof,
            snapshot_id=snapshot.manifest.snapshot_id,
            engine_name="ORE.OREApp (EquityOption XML + pricingengine.xml)",
            model_name="BlackScholesMerton",
            graph_version=graph_version,
            ore_version=getattr(ORE, "__version__", "1.8.17.0"),
        ),
        ore_xml_dir=str(base),
    )
