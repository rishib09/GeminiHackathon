"""ORE SWIG Python API manifest for Graphify AST extraction."""

import ORE


class ORETradePipeline:
    """Full XML + OREApp batch pipeline for EquityOption trades."""

    def build_parameters(self, ore_xml_path: str):
        params = ORE.Parameters()
        params.fromFile(ore_xml_path)
        return params

    def run_ore_app(self, params):
        app = ORE.OREApp(params)
        return app.run()


class OREInProcessEquityOption:
    """In-process ORE SWIG VanillaOption + American/European pricing pipeline."""

    def create_instrument(self, option_type, strike, start_date, expiry_date, american=True):
        payoff = ORE.PlainVanillaPayoff(option_type, strike)
        exercise = (
            ORE.AmericanExercise(start_date, expiry_date)
            if american
            else ORE.EuropeanExercise(expiry_date)
        )
        return ORE.VanillaOption(payoff, exercise)

    def create_bsm_process(self, spot, div_ts, risk_free_ts, vol_ts):
        return ORE.BlackScholesMertonProcess(spot, div_ts, risk_free_ts, vol_ts)

    def price_flat_vol(self, option, process, t_grid=100, x_grid=100):
        engine = ORE.FdBlackScholesVanillaEngine(process, t_grid, x_grid)
        option.setPricingEngine(engine)
        return option.NPV(), option.delta()

    def price_term_structure_vol(self, option, process, t_grid=100, x_grid=100):
        engine = ORE.FdBlackScholesVanillaEngine(process, t_grid, x_grid)
        option.setPricingEngine(engine)
        return option.NPV(), option.delta()

    def price_local_vol(self, option, process, t_grid=120, x_grid=120):
        engine = ORE.FdBlackScholesVanillaEngine(
            process, t_grid, x_grid, 0, ORE.FdmSchemeDesc.Douglas(), True
        )
        option.setPricingEngine(engine)
        return option.NPV(), option.delta()

    def price_heston(self, option, heston_model, t_grid=80, x_grid=80, v_grid=40):
        engine = ORE.FdHestonVanillaEngine(heston_model, t_grid, x_grid, v_grid)
        option.setPricingEngine(engine)
        return option.NPV(), option.delta()

    def price_baw(self, option, process):
        engine = ORE.BaroneAdesiWhaleyApproximationEngine(process)
        option.setPricingEngine(engine)
        return option.NPV()

    def price_bjerksund(self, option, process):
        engine = ORE.BjerksundStenslandApproximationEngine(process)
        option.setPricingEngine(engine)
        return option.NPV()

    def price_binomial_crr(self, option, process, steps=200):
        engine = ORE.BinomialCRRVanillaEngine(process, steps)
        option.setPricingEngine(engine)
        return option.NPV(), option.delta()
