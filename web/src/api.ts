import type {
  AnalyzeResponse,
  CandidateHit,
  CompiledRecipe,
} from "./types";

async function jsonFetch<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(url, {
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  if (!res.ok) {
    const detail = await res.text();
    throw new Error(`API ${res.status}: ${detail}`);
  }
  return res.json() as Promise<T>;
}

export function fetchRoots(q = ""): Promise<CandidateHit[]> {
  return jsonFetch(`/api/picker/roots?q=${encodeURIComponent(q)}`);
}

export function fetchVerbs(rootId: string, q = ""): Promise<CandidateHit[]> {
  return jsonFetch(
    `/api/picker/verbs?root_id=${encodeURIComponent(rootId)}&q=${encodeURIComponent(q)}`
  );
}

export function fetchAxes(
  rootId: string,
  verb: string,
  q = ""
): Promise<CandidateHit[]> {
  return jsonFetch(
    `/api/picker/axes?root_id=${encodeURIComponent(rootId)}&verb=${encodeURIComponent(verb)}&q=${encodeURIComponent(q)}`
  );
}

export function fetchCanonicalTitle(
  rootId: string,
  verb: string,
  axis?: string | null
): Promise<{ title: string }> {
  const params = new URLSearchParams({ root_id: rootId, verb });
  if (axis) params.set("axis", axis);
  return jsonFetch(`/api/picker/title?${params.toString()}`);
}

export function analyzeRecipe(payload: {
  title: string;
  paragraphs: string[];
  disambiguation?: string | null;
  selected_models?: string[];
  selected_calculators?: string[];
}): Promise<AnalyzeResponse> {
  return jsonFetch("/api/analyze", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function compileRecipeLive(payload: {
  title: string;
  paragraphs: string[];
  asof?: string;
  symbol?: string;
  disambiguation?: string | null;
  selected_models?: string[];
  selected_calculators?: string[];
}): Promise<CompiledRecipe> {
  return jsonFetch("/api/compile", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}
