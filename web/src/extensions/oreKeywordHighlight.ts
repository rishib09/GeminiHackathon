import { Extension } from "@tiptap/core";
import { Plugin, PluginKey } from "@tiptap/pm/state";
import { Decoration, DecorationSet } from "@tiptap/pm/view";
import type { DetectedKeyword } from "../types";

export const oreKeywordPluginKey = new PluginKey<DetectedKeyword[][]>(
  "oreKeywordHighlight"
);

/**
 * ProseMirror decoration plugin that renders every detected ORE keyword
 * as a visually separate, category-colored inline chip without mutating
 * document text or disrupting the user's cursor while typing.
 */
export const OreKeywordHighlight = Extension.create({
  name: "oreKeywordHighlight",

  addProseMirrorPlugins() {
    return [
      new Plugin<DetectedKeyword[][]>({
        key: oreKeywordPluginKey,
        state: {
          init: () => [],
          apply(tr, prev) {
            const next = tr.getMeta(oreKeywordPluginKey) as
              | DetectedKeyword[][]
              | undefined;
            return next !== undefined ? next : prev;
          },
        },
        props: {
          decorations(state) {
            const spansByParagraph =
              oreKeywordPluginKey.getState(state) ?? [];
            const decorations: Decoration[] = [];
            let paraIdx = 0;

            state.doc.forEach((node, offset) => {
              if (node.type.name !== "paragraph") return;
              const spans = spansByParagraph[paraIdx] ?? [];
              paraIdx += 1;
              const contentStart = offset + 1;
              const maxLen = node.textContent.length;

              for (const sp of spans) {
                if (sp.start < 0 || sp.end > maxLen || sp.start >= sp.end) {
                  continue;
                }
                decorations.push(
                  Decoration.inline(
                    contentStart + sp.start,
                    contentStart + sp.end,
                    {
                      class: `kw-chip kw-${sp.category}`,
                      title: `${sp.canonical} · ORE: ${sp.ore_ref}`,
                    }
                  )
                );
              }
            });

            return DecorationSet.create(state.doc, decorations);
          },
        },
      }),
    ];
  },
});
