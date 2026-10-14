// What the operating system says the first time an unsigned build opens, and
// how to get past it. It states the current state only: these builds aren't
// signed, and no date is promised here. RELEASES.md tracks the signed release.
import { Callout } from "./primitives";

const MAC =
  typeof navigator !== "undefined" && /Mac/i.test(navigator.platform);

export function OsWarning() {
  if (MAC) {
    return (
      <Callout tone="info" title="First launch on macOS">
        This build isn&rsquo;t notarized by Apple, so macOS says it&rsquo;s from
        an unidentified developer. To open it the first time, Control-click
        OpenAdapt in Applications, choose Open, then choose Open again. macOS
        remembers your choice.
      </Callout>
    );
  }
  return (
    <Callout tone="info" title="First launch on Windows">
      This build isn&rsquo;t code-signed, so Windows SmartScreen may show
      &ldquo;Windows protected your PC&rdquo;. Choose More info, then Run anyway.
    </Callout>
  );
}
