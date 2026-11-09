// What the operating system does when someone opens a new, unsigned version,
// and how to get past it. It states the current state only: these builds
// aren't signed, and no date is promised here. RELEASES.md tracks the signed
// release. Each platform gets its own steps; Linux never gets Windows text.
import { Callout } from "./primitives";

export type DesktopPlatform = "mac" | "windows" | "linux" | "other";

export function detectPlatform(
  nav: { platform?: string; userAgent?: string } | undefined =
    typeof navigator !== "undefined" ? navigator : undefined,
): DesktopPlatform {
  const hint = `${nav?.platform ?? ""} ${nav?.userAgent ?? ""}`;
  if (/Mac/i.test(hint)) return "mac";
  if (/Win/i.test(hint)) return "windows";
  if (/Linux|X11/i.test(hint)) return "linux";
  return "other";
}

export function OsWarning({
  platform = detectPlatform(),
}: {
  platform?: DesktopPlatform;
}) {
  if (platform === "mac") {
    return (
      <Callout tone="info" title="Opening a new version on macOS">
        This build isn&rsquo;t notarized by Apple, so macOS blocks it the first
        time you open it. Close the warning, open System Settings &gt; Privacy
        &amp; Security, and choose Open Anyway next to OpenAdapt. On macOS 14 or
        earlier, you can instead Control-click OpenAdapt in Applications and
        choose Open.
      </Callout>
    );
  }
  if (platform === "windows") {
    return (
      <Callout tone="info" title="Opening a new version on Windows">
        This build isn&rsquo;t code-signed, so Windows SmartScreen may show
        &ldquo;Windows protected your PC&rdquo;. Choose More info, then Run
        anyway.
      </Callout>
    );
  }
  if (platform === "linux") {
    return (
      <Callout tone="info" title="Installing a new version on Linux">
        Linux doesn&rsquo;t check who made this build. Before you install a new
        version, compare its SHA-256 checksum with SHA256SUMS on the release
        page. Make an AppImage executable first with chmod +x.
      </Callout>
    );
  }
  return null;
}
