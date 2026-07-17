// Honest "get past the OS warning" copy for first-run (spec §4c).
// Verbatim wording from the desktop-tray architecture spec — the installers are
// unsigned for v1, so this appears on the download page AND here on first run.
import { Callout } from "./primitives";

const MAC =
  typeof navigator !== "undefined" && /Mac/i.test(navigator.platform);

export function OsWarning() {
  if (MAC) {
    return (
      <Callout tone="info" title="First launch on macOS">
        macOS will say the app is from an unidentified developer. To open it the
        first time: right-click (or Control-click) OpenAdapt in Applications →
        Open → Open. macOS remembers your choice; you won&rsquo;t see this again.
        We&rsquo;re rolling out Apple notarization shortly.
      </Callout>
    );
  }
  return (
    <Callout tone="info" title="First launch on Windows">
      Windows SmartScreen may show a blue &ldquo;Windows protected your
      PC&rdquo; banner. Click More info → Run anyway to install. This appears
      because the installer isn&rsquo;t code-signed yet — signing is on our
      roadmap.
    </Callout>
  );
}
