// Desktop's settings for the shared program map (@openadapt/program-graph,
// packages/program-graph). The component, its layout and its styles live in
// that package; app.css maps Desktop's phosphor tokens onto its --opg-*
// variables.

export const PROGRAM_WORKBENCH_NOTES = {
  evidence:
    "These rows show the declared contract. They do not report a live verdict. Desktop binds live evidence only after the runtime retains an exact run trace.",
};

/** The class that puts the workbench on Desktop's phosphor surface. */
export const PROGRAM_WORKBENCH_SURFACE = "surface-phosphor";
