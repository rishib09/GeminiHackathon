import React, { useState } from "react";
import { Screen1Picker } from "./components/Screen1Picker";
import { Screen2Editor } from "./components/Screen2Editor";
import { Screen3Ambiguity } from "./components/Screen3Ambiguity";
import type { AmbiguityPrompt } from "./types";

type ScreenId = "s1" | "s2" | "s3";

const DEFAULT_PARAGRAPHS = [
  "Load the SPY option chain as of 2026-09-10. Use the 3M Treasury rate and SPY dividend yield from the snapshot.",
  "Price a 30-day ATM American put on SPY using Flat Vol, Term Structure, Local Vol, and Heston, and rank their fit to the chain by delta and chain RMSE.",
];

const DEFAULT_AMBIGUITY: AmbiguityPrompt = {
  key: "american_put",
  matched_phrase: "American put",
  heading: "“American put” matches two things",
  lead: "Pick one. The scope on the right will rebuild from your choice.",
  options: [
    {
      id: "ore_instrument",
      title: "ORE In-Process Instrument",
      description:
        "ORE.VanillaOption with ORE.AmericanExercise. Price a single contract in-process; fastest for model comparison.",
      brings_into_scope: "Brings in: ORE FD and binomial engines, 4 volatility models",
    },
    {
      id: "ore_trade",
      title: "ORE Trade (OREApp XML)",
      description:
        "ORE EquityOption with Style = American. Goes into portfolio.xml and ORE reports (NPV, cashflows).",
      brings_into_scope: "Brings in: Envelope, pricingengine.xml, npv.csv, flows.csv",
    },
  ],
};

export const App: React.FC = () => {
  const [screen, setScreen] = useState<ScreenId>("s2");
  const [title, setTitle] = useState(
    "Compare volatility models on an American equity option"
  );
  const [editorKey, setEditorKey] = useState(0);
  const [initialParagraphs, setInitialParagraphs] =
    useState<string[]>(DEFAULT_PARAGRAPHS);
  const [disambiguation, setDisambiguation] = useState<string | null>(
    "ore_instrument"
  );
  const [activeAmbiguity, setActiveAmbiguity] =
    useState<AmbiguityPrompt>(DEFAULT_AMBIGUITY);

  return (
    <>
      <nav className="switch" aria-label="Workbench screens">
        <button
          role="tab"
          aria-selected={screen === "s1"}
          onClick={() => setScreen("s1")}
        >
          1 · Pick the root
        </button>
        <button
          role="tab"
          aria-selected={screen === "s2"}
          onClick={() => setScreen("s2")}
        >
          2 · Write the recipe
        </button>
        <button
          role="tab"
          aria-selected={screen === "s3"}
          onClick={() => setScreen("s3")}
        >
          3 · Resolve ambiguity
        </button>
        <span className="status-tag">
          <span className="dot-live" />
          Live ORE + Graphify + TipTap
        </span>
      </nav>

      {screen === "s1" && (
        <Screen1Picker
          onComplete={({ title: pickedTitle }) => {
            setTitle(pickedTitle);
            setScreen("s2");
          }}
          onStartFreeform={() => {
            setTitle("Interactive ORE Keyword Workbench");
            setInitialParagraphs([
              "Price a 30-day ATM American put on SPY using Heston and Local Vol, and check delta and NPV.",
            ]);
            setEditorKey((k) => k + 1);
            setScreen("s2");
          }}
        />
      )}

      {screen === "s2" && (
        <Screen2Editor
          key={editorKey}
          title={title}
          onTitleChange={setTitle}
          initialParagraphs={initialParagraphs}
          disambiguation={disambiguation}
          onChooseDisambiguation={setDisambiguation}
          onOpenAmbiguityScreen={(prompt) => {
            setActiveAmbiguity(prompt);
            setScreen("s3");
          }}
        />
      )}

      {screen === "s3" && (
        <Screen3Ambiguity
          prompt={activeAmbiguity}
          currentChoice={disambiguation}
          onConfirm={(choice) => {
            setDisambiguation(choice);
            setScreen("s2");
          }}
          onBack={() => setScreen("s2")}
        />
      )}
    </>
  );
};
