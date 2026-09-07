# AgentG Logo System

## Selected primary logo

The primary AgentG product logo is `researcher-v2/researcher-v2-logo.svg`, concept 01 **Researcher Core**, based on DeskPet `01-researcher-v2`. The same vector paths are integrated into the Web sidebar. It uses a transparent `viewBox="0 0 100 100"` canvas and `currentColor` so product surfaces can control contrast without maintaining theme-specific artwork.

The remaining concepts are retained as exploration and extension assets rather than competing primary marks.

## Two reference collections

| Collection | Reference | Personality | Primary candidate | Assets |
|---|---|---|---|---|
| Pei / Identity 01 | DeskPet `01-researcher-v3` | Bright, friendly, AI-native, distinctive | `agentg-logo.svg` | Root SVGs, `showcase.html`, `logo-preview.png` |
| Researcher / Identity 02 | DeskPet `01-researcher-v2` | Warm, grounded, dependable, developer-oriented | `researcher-v2/researcher-v2-logo.svg` | `researcher-v2/` SVGs, showcase, preview, and use-case guide |

Use the Pei collection when character distinction and a modern AI personality matter most. Use the v2 Researcher collection when reliability, engineering discipline, and a warmer developer-tool identity matter most.

## Pei collection brand brief

- **Brand**: AgentG
- **Category**: Multi-Agent / Agentic Workflow developer platform
- **Core idea**: an Orchestrator routes work to specialist Workers, gathers the results, and closes the loop with structured review
- **Visual reference**: the head silhouette of DeskPet `01-researcher-v3` — a high cyan bun, side-swept fringe, rounded face, large eyes, and a clear dark outline
- **Mood**: precise, dependable, technical, and friendly
- **UI palette**: blue `#7185ff`, violet `#9c72ed`, and cyan `#2cd4d9`

The vectors deliberately abstract the supplied Pei reference instead of tracing the game artwork. The bun becomes the Orchestrator, the fringe becomes a routing path, and small nodes become specialist Workers. Every production SVG uses `currentColor`, so the application controls its color.

## Pei collection concepts

| File | Direction | Design rationale |
|---|---|---|
| `agentg-logo.svg` | Pei Orchestrator | Recommended starting point. The smallest, friendliest head abstraction; the sweeping fringe reads as a routed task path. |
| `variant-02-worker-glyph.svg` | Worker Glyph | A dotted `G` and elevated Orchestrator node communicate modular Workers and distributed execution. |
| `variant-03-flow-fringe.svg` | Flow Fringe | A dense line system turns Pei's fringe into parallel task streams converging on one result. |
| `variant-04-fanout-halo.svg` | Fan-out Halo | Four Worker nodes sit inside a stable head boundary while the bun anchors orchestration. |
| `variant-05-graph-monogram.svg` | Graph Monogram | A rounded graph container and `G` route emphasize developer tooling and structured execution. |
| `variant-06-review-loop.svg` | Review Loop | An asymmetric node network forms a face while a return arc represents Reviewer-directed revision. |

Open `showcase.html` to compare all six concepts in dark and light themes, inspect 16/32/64 px sizes, and download the source SVGs.

## Pei selection history

`agentg-logo.svg` was the initial Identity 01 recommendation because it remains recognizable at small sizes and connects directly to the DeskPet character. The final product selection instead uses the v2 Researcher Core direction above.

The second collection is documented in `researcher-v2/README.md`. It includes a suitable-scenario explanation for every concept and records the selected Web identity.
