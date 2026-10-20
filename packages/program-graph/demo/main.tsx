import { createRoot } from "react-dom/client";
import { ProgramWorkbench, type FoldSpec, type ProgramGraphSpec, type RunTrace, type StopExit } from "../src/index.js";
import "../src/styles.css";
import spec from "../fixtures/openemr-referral/spec.json";
import folds from "../fixtures/openemr-referral/folds.json";
import trace from "../fixtures/openemr-referral/trace.json";
import { processRecords } from "../test/fixtures.js";

// ?case=referral|records  &cursor=N  &theme=light  &view=evidence|stops  &unfold=1  &play=1
const q = new URLSearchParams(location.search);
const theme = q.get("theme") === "light" ? "light" : "dark";
document.body.dataset.theme = theme;
const view = (q.get("view") ?? "program") as "program" | "evidence" | "stops";
const cursor = q.get("cursor") === "end" ? (trace as RunTrace).events.length : Number(q.get("cursor") ?? 0);
const referral = (
  <ProgramWorkbench
    spec={spec as ProgramGraphSpec}
    title="New referral, for each referral in the list"
    kicker="Compiled program"
    folds={q.get("unfold") ? null : (folds as FoldSpec)}
    stopExits={(folds as { stops: StopExit[] }).stops}
    trace={trace as RunTrace}
    iterationNoun="referral"
    initialCursor={cursor}
    autoPlay={q.get("play") === "1"}
    initialView={view}
    theme={theme}
    titleFor={(node) =>
      node.kind === "loop" ? "For each referral in the list" : node.kind === "terminal" && node.index < 43 ? "List done" : node.title
    }
  />
);
const records = <ProgramWorkbench spec={processRecords} initialView={view} theme={theme} />;
createRoot(document.getElementById("root")!).render(q.get("case") === "records" ? records : referral);
