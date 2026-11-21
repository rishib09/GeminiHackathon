import React, { useEffect, useState } from "react";
import {
  fetchAxes,
  fetchCanonicalTitle,
  fetchRoots,
  fetchVerbs,
} from "../api";
import type { CandidateHit } from "../types";

interface Screen1PickerProps {
  onComplete: (result: {
    rootId: string;
    verb: string;
    axis: string | null;
    title: string;
  }) => void;
  onStartFreeform: () => void;
}

type Step = "root" | "verb" | "axis";

export const Screen1Picker: React.FC<Screen1PickerProps> = ({
  onComplete,
  onStartFreeform,
}) => {
  const [step, setStep] = useState<Step>("root");
  const [query, setQuery] = useState("");
  const [hits, setHits] = useState<CandidateHit[]>([]);
  const [selectedIdx, setSelectedIdx] = useState(0);

  const [pickedRoot, setPickedRoot] = useState<CandidateHit | null>(null);
  const [pickedVerb, setPickedVerb] = useState<CandidateHit | null>(null);

  useEffect(() => {
    let active = true;
    const load = async () => {
      try {
        let data: CandidateHit[] = [];
        if (step === "root") {
          data = await fetchRoots(query);
        } else if (step === "verb" && pickedRoot) {
          data = await fetchVerbs(pickedRoot.id, query);
        } else if (step === "axis" && pickedRoot && pickedVerb) {
          data = await fetchAxes(pickedRoot.id, pickedVerb.id, query);
        }
        if (active) {
          setHits(data);
          setSelectedIdx(0);
        }
      } catch {
        if (active) setHits([]);
      }
    };
    load();
    return () => {
      active = false;
    };
  }, [step, query, pickedRoot, pickedVerb]);

  const chooseHit = async (hit: CandidateHit) => {
    if (step === "root") {
      setPickedRoot(hit);
      setQuery("");
      setStep("verb");
      return;
    }
    if (step === "verb" && pickedRoot) {
      setPickedVerb(hit);
      setQuery("");
      if (hit.id === "Compare") {
        setStep("axis");
      } else {
        const { title } = await fetchCanonicalTitle(pickedRoot.id, hit.id, null);
        onComplete({ rootId: pickedRoot.id, verb: hit.id, axis: null, title });
      }
      return;
    }
    if (step === "axis" && pickedRoot && pickedVerb) {
      const { title } = await fetchCanonicalTitle(
        pickedRoot.id,
        pickedVerb.id,
        hit.id
      );
      onComplete({
        rootId: pickedRoot.id,
        verb: pickedVerb.id,
        axis: hit.id,
        title,
      });
    }
  };

  const handleKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setSelectedIdx((i) => Math.min(i + 1, Math.max(hits.length - 1, 0)));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setSelectedIdx((i) => Math.max(i - 1, 0));
    } else if (e.key === "Enter" && hits[selectedIdx]) {
      e.preventDefault();
      void chooseHit(hits[selectedIdx]);
    } else if (e.key === "Escape") {
      e.preventDefault();
      if (step === "axis") {
        setStep("verb");
        setPickedVerb(null);
        setQuery("");
      } else if (step === "verb") {
        setStep("root");
        setPickedRoot(null);
        setQuery("");
      }
    }
  };

  const question =
    step === "root"
      ? "What ORE product are you working on?"
      : step === "verb"
      ? "What do you want to do?"
      : "What should vary?";

  const placeholder =
    step === "root"
      ? "Type a product (e.g. equity option)…"
      : step === "verb"
      ? "Compare, Price, or Explain…"
      : "Type a keyword (e.g. vol, engine)…";

  return (
    <div className="root">
      <div className="crumbs">
        {pickedRoot && (
          <span className="crumb">
            Product: <b>{pickedRoot.name}</b>
          </span>
        )}
        {pickedVerb && (
          <span className="crumb">
            Do: <b>{pickedVerb.name}</b>
          </span>
        )}
      </div>

      <h1 className="question">{question}</h1>
      <input
        className="ask"
        type="text"
        value={query}
        placeholder={placeholder}
        onChange={(e) => setQuery(e.target.value)}
        onKeyDown={handleKeyDown}
        autoFocus
      />
      <div className="hint">
        <span>
          Type a keyword. Only valid ORE graph candidates for this step are listed.
        </span>
        <button
          type="button"
          className="freeform-link"
          onClick={onStartFreeform}
        >
          Or start with free text →
        </button>
      </div>

      <ul className="hits" role="listbox" aria-label="Matches">
        {hits.map((h, idx) => (
          <li
            key={h.id}
            role="option"
            aria-selected={idx === selectedIdx}
            onMouseEnter={() => setSelectedIdx(idx)}
            onClick={() => void chooseHit(h)}
          >
            <div>
              <div className="name">{h.name}</div>
              <div className="why">{h.why}</div>
            </div>
            <div className="src">{h.source_layer}</div>
          </li>
        ))}
        {hits.length === 0 && (
          <li>
            <div>
              <div className="name">No in-scope ORE matches</div>
              <div className="why">
                Try clearing the filter or switch to free-text mode to explore ORE keywords.
              </div>
            </div>
            <div className="src">ORE Graph</div>
          </li>
        )}
      </ul>

      <p className="keys">
        <kbd>↑</kbd> <kbd>↓</kbd> move &nbsp; <kbd>Enter</kbd> choose &nbsp;{" "}
        <kbd>Esc</kbd> back one step
      </p>
    </div>
  );
};
