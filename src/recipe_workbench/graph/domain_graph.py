"""Domain-enriched ORE knowledge graph backed by Graphify's graph.json."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Dict, List, Optional

import networkx as nx

from recipe_workbench.graph.build_graphify import build_ore_graphify
from recipe_workbench.graph.schema import Axis, GraphEdge, GraphNode, NodeCategory, Verb

DEFAULT_GRAPHIFY_PATH = Path("graphify-out/graph.json")


class OREDomainGraph:
    """Combines Graphify AST nodes with ORE domain semantics for deterministic scope walks."""

    def __init__(self, graphify_path: Path | str = DEFAULT_GRAPHIFY_PATH) -> None:
        self.graphify_path = Path(graphify_path)
        if not self.graphify_path.exists():
            build_ore_graphify(output_json=self.graphify_path)

        with self.graphify_path.open("r", encoding="utf-8") as fh:
            self.raw_graphify = json.load(fh)

        self.ast_nodes_by_label: Dict[str, dict] = {}
        for node in self.raw_graphify.get("nodes", []):
            label = str(node.get("label", ""))
            if label:
                self.ast_nodes_by_label[label] = node

        self.nx_graph = nx.DiGraph()
        self.nodes: Dict[str, GraphNode] = {}
        self.edges: List[GraphEdge] = []
        self._populate_domain_graph()
        self.graph_version = self._compute_version_hash()

    def _ast_ref(self, label_hint: str, fallback: str) -> str:
        for label, node in self.ast_nodes_by_label.items():
            if label_hint.lower() in label.lower():
                src = node.get("source_file", fallback)
                return f"{src}#{node.get('id', label_hint)}"
        return fallback

    def _add_node(self, node: GraphNode) -> None:
        self.nodes[node.id] = node
        self.nx_graph.add_node(node.id, **node.model_dump())

    def _add_edge(self, edge: GraphEdge) -> None:
        self.edges.append(edge)
        self.nx_graph.add_edge(
            edge.source,
            edge.target,
            relation=edge.relation,
            axis=edge.axis,
            note=edge.note,
        )

    def _populate_domain_graph(self) -> None:
        domain_nodes = [
            # Roots
            GraphNode(
                id="EquityOption",
                label="American equity option",
                category=NodeCategory.ROOT,
                layer="ORE",
                description="ORE EquityOption trade / ORE-SWIG VanillaOption supporting American and European exercise.",
                why="Single-stock or index option with American/European exercise in ORE",
                aliases=[
                    "EquityOption",
                    "American equity option",
                    "European equity option",
                    "American option",
                    "equity option",
                ],
                code_ref=self._ast_ref("EquityOption", "ored/portfolio/equityoption.hpp"),
                xml_tag="<TradeType>EquityOption</TradeType>",
            ),
            # Disambiguation targets for "American put"
            GraphNode(
                id="OREInProcessInstrument",
                label="ORE In-Process Instrument",
                category=NodeCategory.NEEDS,
                layer="ORE-QuantLib",
                description="ORE.VanillaOption with ORE.AmericanExercise priced in-process; fastest for multi-model comparison.",
                why="Brings in: ORE FD and binomial engines, 4 volatility models",
                aliases=["ORE instrument", "VanillaOption", "in-process instrument"],
                code_ref=self._ast_ref("create_instrument", "ore_swig_manifest.py"),
                engine_key="in_process",
            ),
            GraphNode(
                id="ORETradeXML",
                label="ORE Trade (OREApp XML)",
                category=NodeCategory.NEEDS,
                layer="ORE",
                description="ORE EquityOption with Style=American executed via ORE.OREApp (portfolio.xml, pricingengine.xml, NPV/Cashflow reports).",
                why="Brings in: Envelope, pricingengine.xml, npv.csv, flows.csv",
                aliases=["ORE trade", "OREApp", "portfolio.xml", "EquityOption XML"],
                code_ref=self._ast_ref("run_ore_app", "ore_swig_manifest.py"),
                xml_tag="<EquityOptionData><OptionData><Style>American</Style>",
                engine_key="ore_app",
            ),
            # Needs leaves (always in scope)
            GraphNode(
                id="EquitySpot",
                label="Spot",
                category=NodeCategory.NEEDS,
                layer="data",
                description="Underlying equity spot price from immutable market snapshot.",
                why="Required underlying price for BlackScholesMertonProcess / HestonProcess",
                aliases=["the spot", "spot", "spot price", "SPY spot"],
                code_ref=self._ast_ref("equitySpot", "ored/marketdata/market.hpp"),
                xml_tag="EQUITY/PRICE/<NAME>/<CCY>",
                is_data_leaf=True,
            ),
            GraphNode(
                id="RiskFreeCurve",
                label="Risk-free curve",
                category=NodeCategory.NEEDS,
                layer="data",
                description="Discount yield term structure built from 3M Treasury / SOFR rate.",
                why="Discounting and drift rate in ORE YieldTermStructureHandle",
                aliases=[
                    "the risk-free rate",
                    "risk-free rate",
                    "3M Treasury rate",
                    "discount curve",
                    "3M Treasury",
                ],
                code_ref=self._ast_ref("discountCurve", "ored/marketdata/market.hpp"),
                xml_tag="ZERO/RATE/<CCY>",
                is_data_leaf=True,
            ),
            GraphNode(
                id="DividendYield",
                label="Dividend yield",
                category=NodeCategory.NEEDS,
                layer="data",
                description="Equity dividend yield / forward curve from snapshot.",
                why="Carry rate reducing drift in ORE BlackScholesMertonProcess",
                aliases=[
                    "the dividend yield",
                    "dividend yield",
                    "SPY dividend yield",
                    "dividend curve",
                ],
                code_ref=self._ast_ref("equityDividendCurve", "ored/marketdata/market.hpp"),
                xml_tag="EQUITY_DIVIDEND/RATE/<NAME>/<CCY>",
                is_data_leaf=True,
            ),
            GraphNode(
                id="OptionChain",
                label="Option chain",
                category=NodeCategory.NEEDS,
                layer="data",
                description="Immutable market option chain snapshot across strikes and expiries.",
                why="Provides contracts for definition lookup and calibration quotes for chain_rmse",
                aliases=[
                    "the option chain",
                    "option chain",
                    "SPY option chain",
                    "that chain",
                    "the chain",
                ],
                code_ref=self._ast_ref("OptionChainSnapshot", "ored/marketdata/market.hpp"),
                is_data_leaf=True,
            ),
            GraphNode(
                id="PricingEngineRouter",
                label="Pricing engine",
                category=NodeCategory.NEEDS,
                layer="ORE-QuantLib",
                description="ORE EquityOptionEngineBuilder pairing volatility model and exercise style to numerical engine.",
                why="Auto-pairs American exercise with FD / Binomial / Analytic approximation engines",
                aliases=["the pricing engine", "pricing engine", "engine builder"],
                code_ref=self._ast_ref("EquityOptionEngineBuilder", "ored/portfolio/builders/equityoption.hpp"),
            ),
            # Varies: Axis = volatility_model
            GraphNode(
                id="FlatBlackScholesVol",
                label="Flat Black–Scholes vol",
                category=NodeCategory.VARIES,
                layer="ORE-QuantLib",
                description="Constant BlackConstantVol matched to ATM implied vol, priced with ORE.FdBlackScholesVanillaEngine.",
                why="Baseline single-parameter Black-Scholes volatility model",
                aliases=["flat vol", "Flat vol", "Flat Black-Scholes vol", "constant vol"],
                code_ref=self._ast_ref("price_flat_vol", "ore_swig_manifest.py"),
                axis=Axis.VOLATILITY_MODEL.value,
                engine_key="flat_vol",
            ),
            GraphNode(
                id="VolTermStructure",
                label="Vol term structure",
                category=NodeCategory.VARIES,
                layer="ORE-QuantLib",
                description="Expiry-indexed ORE.BlackVarianceCurve of ATM implied vols, priced with ORE.FdBlackScholesVanillaEngine.",
                why="Captures maturity slope of volatility across expiries",
                aliases=[
                    "a term structure by expiry",
                    "term structure by expiry",
                    "term structure",
                    "vol term structure",
                    "BlackVarianceCurve",
                ],
                code_ref=self._ast_ref("price_term_structure_vol", "ore_swig_manifest.py"),
                axis=Axis.VOLATILITY_MODEL.value,
                engine_key="term_structure",
            ),
            GraphNode(
                id="LocalVolSurface",
                label="Local vol surface",
                category=NodeCategory.VARIES,
                layer="ORE-QuantLib",
                description="Dupire local volatility surface built from ORE.BlackVarianceSurface across strikes and expiries (localVol=True).",
                why="Exact fit to strike skew and term structure across the chain",
                aliases=["local vol", "Local vol", "local vol surface", "Dupire local vol"],
                code_ref=self._ast_ref("price_local_vol", "ore_swig_manifest.py"),
                axis=Axis.VOLATILITY_MODEL.value,
                engine_key="local_vol",
            ),
            GraphNode(
                id="HestonStochasticVol",
                label="Heston stochastic vol",
                category=NodeCategory.VARIES,
                layer="ORE-QuantLib",
                description="ORE.HestonModel calibrated to the option chain and priced via ORE.FdHestonVanillaEngine.",
                why="Models stochastic variance, mean reversion, vol-of-vol, and spot-vol correlation",
                aliases=["Heston", "heston", "Heston stochastic vol", "FdHestonVanillaEngine"],
                code_ref=self._ast_ref("price_heston", "ore_swig_manifest.py"),
                axis=Axis.VOLATILITY_MODEL.value,
                engine_key="heston",
            ),
            # Varies: Axis = pricing_engine
            GraphNode(
                id="FdBlackScholesEngine",
                label="Finite Difference (PDE)",
                category=NodeCategory.VARIES,
                layer="ORE-QuantLib",
                description="ORE.FdBlackScholesVanillaEngine Crank-Nicolson / Douglas PDE solver for American early exercise.",
                why="High-accuracy 2D PDE grid supporting continuous dividends and local vol",
                aliases=["finite difference", "FD engine", "FdBlackScholesVanillaEngine"],
                code_ref=self._ast_ref("EquityAmericanOptionFdEngineBuilder", "ored/portfolio/builders/equityoption.hpp"),
                axis=Axis.PRICING_ENGINE.value,
                engine_key="fd_bs",
            ),
            GraphNode(
                id="BinomialCRREngine",
                label="Binomial CRR tree",
                category=NodeCategory.VARIES,
                layer="ORE-QuantLib",
                description="ORE.BinomialCRRVanillaEngine Cox-Ross-Rubinstein recombining lattice (200 steps).",
                why="Classic discrete-time lattice for American early exercise",
                aliases=["binomial tree", "Binomial CRR", "BinomialCRRVanillaEngine"],
                code_ref=self._ast_ref("price_binomial_crr", "ore_swig_manifest.py"),
                axis=Axis.PRICING_ENGINE.value,
                engine_key="binomial_crr",
            ),
            GraphNode(
                id="BaroneAdesiWhaleyEngine",
                label="Barone–Adesi–Whaley",
                category=NodeCategory.VARIES,
                layer="ORE-QuantLib",
                description="ORE.BaroneAdesiWhaleyApproximationEngine quadratic analytic approximation for American options.",
                why="Fast closed-form approximation for American exercise premium",
                aliases=["Barone-Adesi-Whaley", "BAW", "BaroneAdesiWhaleyApproximationEngine"],
                code_ref=self._ast_ref("price_baw", "ore_swig_manifest.py"),
                axis=Axis.PRICING_ENGINE.value,
                engine_key="baw",
            ),
            GraphNode(
                id="BjerksundStenslandEngine",
                label="Bjerksund–Stensland",
                category=NodeCategory.VARIES,
                layer="ORE-QuantLib",
                description="ORE.BjerksundStenslandApproximationEngine analytic flat-boundary approximation.",
                why="Fast analytic American option benchmark",
                aliases=["Bjerksund-Stensland", "Bjerksund", "BjerksundStenslandApproximationEngine"],
                code_ref=self._ast_ref("price_bjerksund", "ore_swig_manifest.py"),
                axis=Axis.PRICING_ENGINE.value,
                engine_key="bjerksund",
            ),
            # Formulas (verb-gated per FR-08)
            GraphNode(
                id="formula_price",
                label="price( )",
                category=NodeCategory.FORMULA,
                layer="formula",
                description="Fair value (NPV) of the contract under the bound engine and model.",
                why="Available in Price and Compare recipes",
                aliases=["the price", "price", "fair value", "NPV", "shown below"],
                allowed_verbs=[Verb.PRICE, Verb.COMPARE],
            ),
            GraphNode(
                id="formula_delta",
                label="delta( )",
                category=NodeCategory.FORMULA,
                layer="formula",
                description="First derivative of option price with respect to underlying spot.",
                why="Available in Price and Compare recipes",
                aliases=["the delta", "delta"],
                allowed_verbs=[Verb.PRICE, Verb.COMPARE],
            ),
            GraphNode(
                id="formula_cashflows",
                label="cashflows( )",
                category=NodeCategory.FORMULA,
                layer="formula",
                description="ORE cashflow / payoff schedule emitted from OREApp flows report.",
                why="Available in Price and Compare recipes",
                aliases=["cashflows", "the cashflows", "payoff schedule"],
                allowed_verbs=[Verb.PRICE, Verb.COMPARE],
            ),
            GraphNode(
                id="formula_chain_rmse",
                label="chain_rmse( )",
                category=NodeCategory.FORMULA,
                layer="formula",
                description="Root mean squared pricing error across all contracts in the market option chain.",
                why="Available only in Compare recipes",
                aliases=["fit to the chain", "chain_rmse", "chain RMSE", "fit"],
                allowed_verbs=[Verb.COMPARE],
            ),
            GraphNode(
                id="formula_rank",
                label="rank( )",
                category=NodeCategory.FORMULA,
                layer="formula",
                description="Ranks compared models by ascending chain_rmse and binds the winner.",
                why="Available only in Compare recipes",
                aliases=["rank", "rank them", "ranking", "the winner"],
                allowed_verbs=[Verb.COMPARE],
            ),
            # Siblings (hidden behind "Show more nearby" per FR-09)
            GraphNode(
                id="EuropeanExerciseSibling",
                label="European option",
                category=NodeCategory.SIBLING,
                layer="ORE",
                description="Exercise restricted to maturity date (ORE.EuropeanExercise).",
                why="Sibling exercise style",
                aliases=["European option", "European exercise"],
            ),
            GraphNode(
                id="SABRModelSibling",
                label="SABR smile",
                category=NodeCategory.SIBLING,
                layer="ORE-QuantLib",
                description="SABR stochastic alpha-beta-rho implied vol parameterization.",
                why="Sibling volatility model (primarily IR/FX)",
                aliases=["SABR"],
            ),
            GraphNode(
                id="EquityBarrierSibling",
                label="EquityBarrierOption",
                category=NodeCategory.SIBLING,
                layer="ORE",
                description="Knock-in / knock-out equity barrier trade type.",
                why="Sibling equity derivative",
                aliases=["barrier", "EquityBarrierOption"],
            ),
            GraphNode(
                id="SensitivityAnalyticSibling",
                label="ORE Sensitivity",
                category=NodeCategory.SIBLING,
                layer="ORE",
                description="ORE sensitivity.csv Greeks across equity spot and vol buckets.",
                why="Sibling ORE analytic (Risk verb)",
                aliases=["sensitivity", "ORE Sensitivity"],
            ),
            GraphNode(
                id="XVAAnalyticSibling",
                label="CVA / XVA Engine",
                category=NodeCategory.SIBLING,
                layer="ORE",
                description="Portfolio-level exposure and credit valuation adjustment (deferred to v2).",
                why="Sibling portfolio/book node (out of scope by default)",
                aliases=["XVA", "CVA"],
            ),
            GraphNode(
                id="NettingSetSibling",
                label="NettingSet / CSA",
                category=NodeCategory.SIBLING,
                layer="ORE",
                description="Counterparty netting and collateral annex (deferred to v2).",
                why="Sibling book-level node",
                aliases=["NettingSet", "CSA"],
            ),
        ]

        for node in domain_nodes:
            self._add_node(node)

        domain_edges = [
            # Needs from EquityOption
            GraphEdge(source="EquityOption", target="EquitySpot", relation="needs"),
            GraphEdge(source="EquityOption", target="RiskFreeCurve", relation="needs"),
            GraphEdge(source="EquityOption", target="DividendYield", relation="needs"),
            GraphEdge(source="EquityOption", target="OptionChain", relation="needs"),
            GraphEdge(source="EquityOption", target="PricingEngineRouter", relation="needs"),
            GraphEdge(source="EquityOption", target="OREInProcessInstrument", relation="needs", note="in-process path"),
            GraphEdge(source="EquityOption", target="ORETradeXML", relation="needs", note="OREApp XML path"),
            # Alternatives: volatility_model
            GraphEdge(
                source="EquityOption",
                target="FlatBlackScholesVol",
                relation="alternatives",
                axis=Axis.VOLATILITY_MODEL.value,
            ),
            GraphEdge(
                source="EquityOption",
                target="VolTermStructure",
                relation="alternatives",
                axis=Axis.VOLATILITY_MODEL.value,
            ),
            GraphEdge(
                source="EquityOption",
                target="LocalVolSurface",
                relation="alternatives",
                axis=Axis.VOLATILITY_MODEL.value,
            ),
            GraphEdge(
                source="EquityOption",
                target="HestonStochasticVol",
                relation="alternatives",
                axis=Axis.VOLATILITY_MODEL.value,
            ),
            # Alternatives: pricing_engine
            GraphEdge(
                source="EquityOption",
                target="FdBlackScholesEngine",
                relation="alternatives",
                axis=Axis.PRICING_ENGINE.value,
            ),
            GraphEdge(
                source="EquityOption",
                target="BinomialCRREngine",
                relation="alternatives",
                axis=Axis.PRICING_ENGINE.value,
            ),
            GraphEdge(
                source="EquityOption",
                target="BaroneAdesiWhaleyEngine",
                relation="alternatives",
                axis=Axis.PRICING_ENGINE.value,
            ),
            GraphEdge(
                source="EquityOption",
                target="BjerksundStenslandEngine",
                relation="alternatives",
                axis=Axis.PRICING_ENGINE.value,
            ),
            # Produces (formulas)
            GraphEdge(source="EquityOption", target="formula_price", relation="produces"),
            GraphEdge(source="EquityOption", target="formula_delta", relation="produces"),
            GraphEdge(source="EquityOption", target="formula_cashflows", relation="produces"),
            GraphEdge(source="EquityOption", target="formula_chain_rmse", relation="produces"),
            GraphEdge(source="EquityOption", target="formula_rank", relation="produces"),
            # Siblings (never walked by default)
            GraphEdge(source="EquityOption", target="EuropeanExerciseSibling", relation="sibling"),
            GraphEdge(source="EquityOption", target="SABRModelSibling", relation="sibling"),
            GraphEdge(source="EquityOption", target="EquityBarrierSibling", relation="sibling"),
            GraphEdge(source="EquityOption", target="SensitivityAnalyticSibling", relation="sibling"),
            GraphEdge(source="EquityOption", target="XVAAnalyticSibling", relation="sibling"),
            GraphEdge(source="EquityOption", target="NettingSetSibling", relation="sibling"),
        ]

        for edge in domain_edges:
            self._add_edge(edge)

    def _compute_version_hash(self) -> str:
        payload = {
            "graphify_nodes": len(self.raw_graphify.get("nodes", [])),
            "nodes": [self.nodes[k].model_dump() for k in sorted(self.nodes)],
            "edges": [e.model_dump() for e in sorted(self.edges, key=lambda x: (x.source, x.target, x.relation))],
        }
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()
        return f"v0.4-{digest[:8]}"

    def get_node(self, node_id: str) -> Optional[GraphNode]:
        return self.nodes.get(node_id)

    def find_path(self, source_id: str, target_id: str) -> List[str]:
        if source_id not in self.nx_graph or target_id not in self.nx_graph:
            return []
        try:
            return nx.shortest_path(self.nx_graph, source_id, target_id)
        except nx.NetworkXNoPath:
            return []
