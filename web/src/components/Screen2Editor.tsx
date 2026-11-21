import React, { useCallback, useEffect, useRef, useState } from "react";
import { EditorContent, useEditor } from "@tiptap/react";
import StarterKit from "@tiptap/starter-kit";
import Placeholder from "@tiptap/extension-placeholder";
import { analyzeRecipe, compileRecipeLive } from "../api";
import {
  OreKeywordHighlight,
  oreKeywordPluginKey,
} from "../extensions/oreKeywordHighlight";
import type {
  AmbiguityPrompt,
  AnalyzeResponse,
  CompiledRecipe,
  GraphNode,
} from "../types";

interface Screen2EditorProps {
  title: string;
  onTitleChange: (next: string) => void;
  initialParagraphs: string[];
  disambiguation: string | null;
  onChooseDisambiguation: (choice: string) => void;
  onOpenAmbiguityScreen: (prompt: AmbiguityPrompt) => void;
}

function paragraphsToHtml(paragraphs: string[]): string {
  if (!paragraphs.length) return "<p></p>";
  return paragraphs
    .map((p) => `<p>${p.replace(/&/g, "&amp;").replace(/</g, "&lt;")}</p>`)
    .join("");
}

function extractParagraphsFromDoc(editor: ReturnType<typeof useEditor>): string[] {
  if (!editor) return [];
  const out: string[] = [];
  editor.state.doc.forEach((node) => {
    if (node.type.name === "paragraph") {
      out.push(node.textContent);
    }
  });
  return out;
}

export const Screen2Editor: React.FC<Screen2EditorProps> = ({
  title,
  onTitleChange,
  initialParagraphs,
  disambiguation,
  onChooseDisambiguation,
  onOpenAmbiguityScreen,
}) => {
  const [paragraphs, setParagraphs] = useState<string[]>(initialParagraphs);
  const [selectedModels, setSelectedModels] = useState<string[]>([]);
  const [selectedCalculators, setSelectedCalculators] = useState<string[]>([]);
  const [analysisResp, setAnalysisResp] = useState<AnalyzeResponse | null>(null);
  const [compiled, setCompiled] = useState<CompiledRecipe | null>(null);
  const [compiling, setCompiling] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [dragOver, setDragOver] = useState(false);
  const [showSiblings, setShowSiblings] = useState(false);
  const [dragPhrase, setDragPhrase] = useState<string | null>(null);

  const editor = useEditor({
    extensions: [
      StarterKit,
      Placeholder.configure({
        placeholder:
          "Start typing freely (e.g. Price a 30-day ATM American put on SPY using Heston and Local Vol, check delta)…",
      }),
      OreKeywordHighlight,
    ],
    content: paragraphsToHtml(initialParagraphs),
    onUpdate: ({ editor: ed }) => {
      setParagraphs(extractParagraphsFromDoc(ed));
    },
  });

  // Sync TipTap decorations whenever live keyword spans arrive
  useEffect(() => {
    if (!editor || !analysisResp) return;
    const tr = editor.state.tr.setMeta(
      oreKeywordPluginKey,
      analysisResp.paragraph_spans
    );
    editor.view.dispatch(tr);
  }, [editor, analysisResp]);

  // Debounced real-time ORE keyword analysis as user types
  const analyzeTimer = useRef<number | null>(null);
  useEffect(() => {
    if (analyzeTimer.current) window.clearTimeout(analyzeTimer.current);
    analyzeTimer.current = window.setTimeout(async () => {
      try {
        const resp = await analyzeRecipe({
          title,
          paragraphs,
          disambiguation,
          selected_models: selectedModels,
          selected_calculators: selectedCalculators,
        });
        setAnalysisResp(resp);
        setError(null);
      } catch (err) {
        setError(err instanceof Error ? err.message : String(err));
      }
    }, 200);
    return () => {
      if (analyzeTimer.current) window.clearTimeout(analyzeTimer.current);
    };
  }, [title, paragraphs, disambiguation, selectedModels, selectedCalculators]);

  const runCompile = useCallback(
    async (
      overrideModels?: string[],
      overrideCalcs?: string[],
      overrideDisambig?: string | null
    ) => {
      setCompiling(true);
      setError(null);
      try {
        const res = await compileRecipeLive({
          title,
          paragraphs,
          disambiguation:
            overrideDisambig !== undefined ? overrideDisambig : disambiguation,
          selected_models: overrideModels ?? selectedModels,
          selected_calculators: overrideCalcs ?? selectedCalculators,
        });
        setCompiled(res);
      } catch (err) {
        setError(err instanceof Error ? err.message : String(err));
      } finally {
        setCompiling(false);
      }
    },
    [title, paragraphs, disambiguation, selectedModels, selectedCalculators]
  );

  // Auto-run ORE once on initial load so results are immediately visible
  const didInitialCompile = useRef(false);
  useEffect(() => {
    if (!didInitialCompile.current) {
      didInitialCompile.current = true;
      void runCompile();
    }
  }, [runCompile]);

  const toggleModel = (modelId: string) => {
    const base =
      selectedModels.length > 0
        ? selectedModels
        : (analysisResp?.analysis.recommended_models
            .filter((m) => m.selected)
            .map((m) => m.id) ?? []);
    const next = base.includes(modelId)
      ? base.filter((m) => m !== modelId)
      : [...base, modelId];
    const finalModels = next.length > 0 ? next : [modelId];
    setSelectedModels(finalModels);
    void runCompile(finalModels, selectedCalculators);
  };

  const selectCalculator = (calcId: string) => {
    const next = [calcId];
    setSelectedCalculators(next);
    void runCompile(selectedModels, next);
  };

  const insertPhraseIntoEditor = (phrase: string) => {
    if (!editor) return;
    editor.chain().focus().insertContent(` ${phrase} `).run();
  };

  const handleDrop = (e: React.DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    setDragOver(false);
    const phrase = dragPhrase || e.dataTransfer.getData("text/plain");
    if (phrase) {
      insertPhraseIntoEditor(phrase);
    }
    setDragPhrase(null);
  };

  const scope = analysisResp?.scope ?? compiled?.scope;
  const analysis = analysisResp?.analysis;
  const boundNodeIds = new Set(
    (analysis?.keywords ?? []).map((kw) => kw.node_id)
  );

  const renderRailNode = (node: GraphNode, variant: "need" | "alt" | "formula") => {
    const isUsed = boundNodeIds.has(node.id);
    const phrase = node.aliases[0] || node.label;
    const cls = [
      "node",
      node.is_data_leaf ? "data" : "",
      variant === "alt" ? "alt" : "",
      isUsed ? "used" : "",
    ]
      .filter(Boolean)
      .join(" ");

    return (
      <button
        key={node.id}
        type="button"
        className={cls}
        draggable
        title={`Drag or click to insert "${phrase}" (${node.description})`}
        onDragStart={(e) => {
          setDragPhrase(phrase);
          e.dataTransfer.setData("text/plain", phrase);
          e.dataTransfer.effectAllowed = "copy";
        }}
        onClick={() => insertPhraseIntoEditor(phrase)}
      >
        <span className="dot" />
        <span className="nm">{node.label}</span>
        <span className="st">{isUsed ? "bound" : node.layer}</span>
      </button>
    );
  };

  return (
    <div className="work">
      <article className="doc" aria-label="Recipe document">
        <input
          className="doc-title-input"
          value={title}
          onChange={(e) => onTitleChange(e.target.value)}
          aria-label="Recipe title"
        />

        <p className="scopeline">
          {scope ? (
            <>
              Scope: <b>{scope.root_label}</b> · needs {scope.needs.length},
              varies {scope.varies.length}, {scope.siblings.length} hidden ·
              Snapshot <b>{compiled?.snapshot_summary ?? "SPY 2026-09-10"}</b> ·
              Graph {scope.graph_version}
            </>
          ) : (
            "Loading ORE scope…"
          )}
        </p>

        {/* Free-text TipTap Editor + Live Margin Column */}
        <div className="editor-row">
          <div
            className={`tiptap-shell ${dragOver ? "drag-over" : ""}`}
            onDragOver={(e) => {
              e.preventDefault();
              setDragOver(true);
            }}
            onDragLeave={() => setDragOver(false)}
            onDrop={handleDrop}
          >
            <EditorContent editor={editor} />
          </div>

          <aside className="margin-col" aria-label="Live margin notes">
            {analysis && (
              <div className="m-ok">
                {analysis.keywords.length} ORE keyword
                {analysis.keywords.length === 1 ? "" : "s"} detected ✓
              </div>
            )}
            {compiled?.annotations
              .flatMap((a) => a.margin_ok)
              .slice(0, 3)
              .map((ok, idx) => (
                <div key={idx} className="m-ok">
                  {ok}
                </div>
              ))}
            {compiled?.annotations
              .flatMap((a) => a.assumptions)
              .slice(0, 1)
              .map((warn, idx) => (
                <div key={idx} className="m-warn">
                  {warn}
                </div>
              ))}
            {compiled?.working_model && (
              <div className="m-prov">
                Working model → <b>{compiled.working_model}</b>
              </div>
            )}
            {error && <div className="m-warn">{error}</div>}
          </aside>
        </div>

        {/* Separated Input Keywords Strip */}
        {analysis && analysis.keywords.length > 0 && (
          <div className="kw-strip">
            <div className="kw-strip-title">
              Detected ORE Input Keywords (Separated by Category)
            </div>
            <div className="kw-groups">
              {analysis.keywords.map((kw, idx) => {
                const meta = analysisResp?.category_meta[kw.category];
                return (
                  <span
                    key={`${kw.node_id}-${idx}`}
                    className={`kw-chip kw-pill-standalone kw-${kw.category}`}
                    title={`ORE: ${kw.ore_ref}`}
                  >
                    <span>{kw.phrase}</span>
                    <small>{meta?.label ?? kw.category}</small>
                  </span>
                );
              })}
            </div>
          </div>
        )}

        {/* In-Place Ambiguity Choice Card (Screen 3 inline) */}
        {analysisResp?.ambiguity && (
          <div className="choose" role="group">
            <h3>{analysisResp.ambiguity.heading}</h3>
            <p className="lead">{analysisResp.ambiguity.lead}</p>
            {analysisResp.ambiguity.options.map((opt) => (
              <div
                key={opt.id}
                className="opt"
                onClick={() => {
                  onChooseDisambiguation(opt.id);
                  void runCompile(selectedModels, selectedCalculators, opt.id);
                }}
              >
                <input
                  type="radio"
                  name="inline-amb"
                  checked={disambiguation === opt.id}
                  onChange={() => {
                    onChooseDisambiguation(opt.id);
                    void runCompile(selectedModels, selectedCalculators, opt.id);
                  }}
                />
                <div>
                  <b>{opt.title}</b>
                  <span>{opt.description}</span>
                  <em>{opt.brings_into_scope}</em>
                </div>
              </div>
            ))}
            <div className="act">
              <button
                type="button"
                className="ghost"
                onClick={() =>
                  analysisResp.ambiguity &&
                  onOpenAmbiguityScreen(analysisResp.ambiguity)
                }
              >
                Open full Screen 3 resolver →
              </button>
            </div>
          </div>
        )}

        {/* Interactive Choice Panel: Recommended ORE Models & Calculators */}
        {analysis && (
          <div className="choice-panel">
            <h3>Recommended ORE Models & Calculators</h3>
            <p className="lead">
              Choices tailored to your detected ORE keywords. Click any model or
              calculator to run ORE live.
            </p>

            <div className="choice-section-label">
              ORE Models (click to toggle)
            </div>
            <div className="choice-chips">
              {analysis.recommended_models.map((m) => (
                <button
                  key={m.id}
                  type="button"
                  className={`choice-btn ${m.selected ? "active" : ""}`}
                  onClick={() => toggleModel(m.id)}
                >
                  <span className="cb-title">
                    {m.name} {m.matched_explicitly ? "★" : ""}
                  </span>
                  <span className="cb-sub">{m.ore_class}</span>
                </button>
              ))}
            </div>

            <div className="choice-section-label">
              ORE Calculators & Pricing Engines (click to run)
            </div>
            <div className="choice-chips">
              {analysis.recommended_calculators.map((c) => (
                <button
                  key={c.id}
                  type="button"
                  className={`choice-btn ${c.selected ? "active" : ""}`}
                  onClick={() => selectCalculator(c.id)}
                >
                  <span className="cb-title">{c.name}</span>
                  <span className="cb-sub">{c.outputs}</span>
                </button>
              ))}
            </div>

            <div className="choice-actions">
              <button
                type="button"
                className="btn primary"
                disabled={compiling}
                onClick={() => void runCompile()}
              >
                {compiling ? "Running ORE…" : "Run ORE Calculation"}
              </button>
              {compiled?.price_result && (
                <span className="r-pill">
                  ▸ NPV {compiled.price_result.npv.toFixed(4)} USD
                </span>
              )}
              {compiled?.comparison_result && (
                <span className="r-pill">
                  ▸ Winner: {compiled.comparison_result.winner_model_name}
                </span>
              )}
            </div>
          </div>
        )}

        {/* Live ORE Comparison Table */}
        {compiled?.comparison_result && (
          <div className="cell" aria-label="ORE Comparison Results">
            <div className="cell-head">
              <h3>
                Live ORE Comparison ({compiled.comparison_result.rows.length}{" "}
                models)
              </h3>
              <span className="prov">
                ORE {compiled.comparison_result.provenance.ore_version} ·{" "}
                {compiled.comparison_result.provenance.snapshot_id}
              </span>
            </div>
            <table>
              <thead>
                <tr>
                  <th>Model</th>
                  <th>ORE Engine</th>
                  <th>Price</th>
                  <th>Delta</th>
                  <th>Chain RMSE</th>
                </tr>
              </thead>
              <tbody>
                {compiled.comparison_result.rows.map((row) => (
                  <tr key={row.model_id} className={row.rank === 1 ? "best" : ""}>
                    <td>
                      {row.model_name} {row.rank === 1 ? "🏆" : ""}
                    </td>
                    <td>{row.engine_name}</td>
                    <td>{row.price.toFixed(4)}</td>
                    <td>{row.delta.toFixed(4)}</td>
                    <td>
                      {row.chain_rmse.toFixed(4)} (#{row.rank})
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        {/* Live ORE Single-Contract Pricing Table */}
        {compiled?.price_result && (
          <div className="cell" aria-label="ORE Single Price Result">
            <div className="cell-head">
              <h3>
                Live ORE Valuation — {compiled.price_result.provenance.model_name}
              </h3>
              <span className="prov">
                {compiled.price_result.provenance.engine_name}
              </span>
            </div>
            <table>
              <thead>
                <tr>
                  <th>Contract</th>
                  <th>Style / Strike</th>
                  <th>NPV (USD)</th>
                  <th>Delta</th>
                  <th>Expiry</th>
                </tr>
              </thead>
              <tbody>
                <tr className="best">
                  <td>{compiled.price_result.contract.contract_symbol}</td>
                  <td>
                    {compiled.price_result.contract.exercise_style}{" "}
                    {compiled.price_result.contract.option_type} @{" "}
                    {compiled.price_result.contract.strike.toFixed(2)}
                  </td>
                  <td>{compiled.price_result.npv.toFixed(4)}</td>
                  <td>
                    {compiled.price_result.delta != null
                      ? compiled.price_result.delta.toFixed(4)
                      : "N/A"}
                  </td>
                  <td>{compiled.price_result.contract.expiry}</td>
                </tr>
              </tbody>
            </table>
          </div>
        )}
      </article>

      {/* Right Scope Rail */}
      <aside className="railwrap" aria-label="In Scope Rail">
        <div className="rail">
          <h2>In scope</h2>
          <p className="sub">
            Walked from <b>{scope?.root_label ?? "American option"}</b>. Drag or
            click a node to insert it into your recipe.
          </p>

          {scope && (
            <>
              <div className="grp">
                <h3>
                  Needs <small>· always in</small>
                </h3>
                {scope.needs.map((n) => renderRailNode(n, "need"))}
              </div>

              {scope.varies.length > 0 && (
                <div className="grp">
                  <h3>
                    Varies <small>· {scope.axis ?? "axis"}</small>
                  </h3>
                  {scope.varies.map((n) => renderRailNode(n, "alt"))}
                </div>
              )}

              <div className="grp">
                <h3>Formulas</h3>
                {scope.formulas.map((n) => renderRailNode(n, "formula"))}
              </div>

              {scope.siblings.length > 0 && (
                <div className="grp">
                  <button
                    type="button"
                    className="more"
                    onClick={() => setShowSiblings((s) => !s)}
                  >
                    {showSiblings
                      ? "Hide nearby ORE nodes"
                      : `Show ${scope.siblings.length} more nearby (${scope.siblings
                          .slice(0, 3)
                          .map((s) => s.label)
                          .join(", ")}…)`}
                  </button>
                  {showSiblings &&
                    scope.siblings.map((n) => renderRailNode(n, "alt"))}
                </div>
              )}
            </>
          )}

          <div className="railnote">
            Every recognized ORE keyword in your prose separates visually into
            its own color-coded chip and unlocks matching ORE models and
            calculators.
          </div>
        </div>
      </aside>
    </div>
  );
};
