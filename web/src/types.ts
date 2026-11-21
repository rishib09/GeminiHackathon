export type KeywordCategory =
  | "instrument"
  | "input_param"
  | "model"
  | "engine"
  | "calculator"
  | "adjacent_ore";

export interface CandidateHit {
  id: string;
  name: string;
  why: string;
  source_layer: string;
  kind: "root" | "verb" | "axis";
  proposed?: boolean;
}

export interface DetectedKeyword {
  phrase: string;
  canonical: string;
  category: KeywordCategory;
  ore_ref: string;
  node_id: string;
  start: number;
  end: number;
  resolved_value?: string | null;
}

export interface ModelRecommendation {
  id: string;
  name: string;
  ore_class: string;
  reason: string;
  matched_explicitly: boolean;
  selected: boolean;
}

export interface CalculatorRecommendation {
  id: string;
  name: string;
  ore_engine: string;
  outputs: string;
  reason: string;
  selected: boolean;
}

export interface KeywordAnalysisResult {
  keywords: DetectedKeyword[];
  by_category: Record<string, DetectedKeyword[]>;
  recommended_models: ModelRecommendation[];
  recommended_calculators: CalculatorRecommendation[];
  inferred_verb: "Explain" | "Price" | "Compare";
  inferred_axis?: string | null;
  inferred_contract_params: Record<string, unknown>;
  has_ore_keywords: boolean;
}

export interface GraphNode {
  id: string;
  label: string;
  category: string;
  layer: string;
  description: string;
  why: string;
  aliases: string[];
  is_data_leaf: boolean;
}

export interface ScopeSpec {
  title: string;
  slug: string;
  root_id: string;
  root_label: string;
  verb: string;
  axis?: string | null;
  disambiguation?: string | null;
  graph_version: string;
  needs: GraphNode[];
  varies: GraphNode[];
  formulas: GraphNode[];
  siblings: GraphNode[];
}

export interface AmbiguityOption {
  id: string;
  title: string;
  description: string;
  brings_into_scope: string;
}

export interface AmbiguityPrompt {
  key: string;
  matched_phrase: string;
  heading: string;
  lead: string;
  options: AmbiguityOption[];
}

export interface AnalyzeResponse {
  analysis: KeywordAnalysisResult;
  paragraph_spans: DetectedKeyword[][];
  ambiguity: AmbiguityPrompt | null;
  scope: ScopeSpec;
  category_meta: Record<string, { label: string; emoji: string; color: string }>;
}

export interface ModelRow {
  model_id: string;
  model_name: string;
  engine_name: string;
  price: number;
  delta: number;
  chain_rmse: number;
  rank: number;
  elapsed_ms: number;
  notes?: string;
}

export interface ComparisonResult {
  axis: string;
  rows: ModelRow[];
  winner_model_id: string;
  winner_model_name: string;
  provenance: {
    asof: string;
    snapshot_id: string;
    engine_name: string;
    model_name: string;
    graph_version: string;
    ore_version: string;
  };
}

export interface PriceResult {
  contract: {
    symbol: string;
    contract_symbol: string;
    option_type: string;
    exercise_style: string;
    strike: number;
    expiry: string;
    dte: number;
  };
  npv: number;
  delta?: number | null;
  cashflows: { date: string; type: string; amount: number; currency: string }[];
  assumptions: string[];
  provenance: {
    asof: string;
    snapshot_id: string;
    engine_name: string;
    model_name: string;
    graph_version: string;
    ore_version: string;
  };
}

export interface ParagraphAnnotation {
  paragraph_index: number;
  text: string;
  margin_ok: string[];
  provenance_notes: string[];
  assumptions: string[];
  ambiguity?: AmbiguityPrompt | null;
  inline_result_pill?: string | null;
  comparison_result?: ComparisonResult | null;
  price_result?: PriceResult | null;
}

export interface CompiledRecipe {
  scope: ScopeSpec;
  snapshot_id: string;
  snapshot_summary: string;
  halted_on_ambiguity: boolean;
  annotations: ParagraphAnnotation[];
  working_model?: string | null;
  comparison_result?: ComparisonResult | null;
  price_result?: PriceResult | null;
  keyword_analysis?: KeywordAnalysisResult | null;
}
