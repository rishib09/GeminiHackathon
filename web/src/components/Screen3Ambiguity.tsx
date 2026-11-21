import React, { useState } from "react";
import type { AmbiguityPrompt } from "../types";

interface Screen3AmbiguityProps {
  prompt: AmbiguityPrompt;
  currentChoice: string | null;
  onConfirm: (choiceId: string) => void;
  onBack: () => void;
}

export const Screen3Ambiguity: React.FC<Screen3AmbiguityProps> = ({
  prompt,
  currentChoice,
  onConfirm,
  onBack,
}) => {
  const [selected, setSelected] = useState<string>(
    currentChoice || prompt.options[0]?.id || "ore_instrument"
  );
  const [showWhy, setShowWhy] = useState(false);

  const selectedOption = prompt.options.find((o) => o.id === selected);

  return (
    <div className="amb">
      <p>
        Define <span className="kw-chip kw-instrument">the put</span> as the{" "}
        <span className="kw-chip kw-input_param">30-day</span>{" "}
        <span className="kw-chip kw-input_param">ATM</span>{" "}
        <mark
          style={{
            background: "var(--assume-bg)",
            color: "inherit",
            padding: "2px 6px",
            borderRadius: "4px",
          }}
        >
          {prompt.matched_phrase}
        </mark>{" "}
        from that chain.
      </p>

      <div className="choose" role="group" aria-labelledby="amb-h">
        <h3 id="amb-h">{prompt.heading}</h3>
        <p className="lead">{prompt.lead}</p>

        {prompt.options.map((opt) => (
          <label key={opt.id} className="opt">
            <input
              type="radio"
              name="amb"
              checked={selected === opt.id}
              onChange={() => setSelected(opt.id)}
            />
            <div>
              <b>{opt.title}</b>
              <span>{opt.description}</span>
              <em>{opt.brings_into_scope}</em>
            </div>
          </label>
        ))}

        <div className="act">
          <button
            type="button"
            className="btn primary"
            onClick={() => onConfirm(selected)}
          >
            Use {selectedOption?.title ?? selected}
          </button>
          <button
            type="button"
            className="ghost"
            onClick={() => setShowWhy((v) => !v)}
          >
            {showWhy ? "Hide explanation" : "Why are there two?"}
          </button>
          <button type="button" className="ghost" onClick={onBack}>
            Back to recipe
          </button>
        </div>

        {showWhy && (
          <p
            style={{
              marginTop: "12px",
              fontSize: "13px",
              color: "var(--muted)",
              fontFamily: "var(--sans)",
            }}
          >
            ORE exposes both fast in-process QuantLib objects (
            <code>ORE.VanillaOption</code>) for interactive model comparison and
            full portfolio XML trades (<code>ORE.OREApp</code>) that emit
            institutional <code>npv.csv</code> and <code>flows.csv</code> audit
            reports.
          </p>
        )}
      </div>

      <p
        style={{
          color: "var(--muted)",
          fontFamily: "var(--sans)",
          fontSize: "14px",
        }}
      >
        The question appears in place, under the sentence that caused it.
        Nothing else on the page changes until you answer, and the answer is
        remembered for this document.
      </p>
    </div>
  );
};
