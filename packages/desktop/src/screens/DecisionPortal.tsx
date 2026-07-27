// Decision portal — Desktop's half of the mobile attended-decision loop.
//
// This screen is lifecycle and pairing only. It shows where the portal is
// published, mints one-use QR pairings, displays the matching code the phone
// must also show, and lists paired devices. It renders no question, no
// evidence, and no action: those belong to openadapt-flow and are shown on the
// phone, not here.
import { useCallback, useEffect, useState } from "react";
import { CMD, engineInvoke, engineTry } from "../lib/engine";
import { Button, Callout, Card, CardHead, EmptyState, Pill } from "../ui/primitives";

type Ingress = {
  configured?: boolean;
  mode?: string;
  bind_host?: string;
  loopback_only?: boolean;
  public_origin?: string;
  reachable_from_phone?: boolean;
  error?: string;
};

type Device = {
  session_id: string;
  device_label: string;
  approved: boolean;
  expires_in_s: number;
};

type PortalStatus = {
  running: boolean;
  console_alive?: boolean;
  ingress: Ingress;
  port?: number;
  devices: Device[];
  error?: string | null;
};

type Pairing = {
  pairing_id: string;
  url: string;
  expires_in_s: number;
  reachable_from_phone: boolean;
  qr_svg?: string | null;
  note?: string;
};

type PairingStatus = {
  state: "pending" | "claimed" | "approved" | "expired" | "cancelled";
  expires_in_s: number;
  attempts_remaining: number;
  device_label?: string | null;
};

const STOPPED: PortalStatus = { running: false, ingress: {}, devices: [] };

export function DecisionPortal() {
  const [status, setStatus] = useState<PortalStatus>(STOPPED);
  const [pairing, setPairing] = useState<Pairing | null>(null);
  const [scan, setScan] = useState<PairingStatus | null>(null);
  const [confirmCode, setConfirmCode] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setStatus(await engineTry<PortalStatus>(CMD.PORTAL_STATUS, {}, STOPPED));
  }, []);

  useEffect(() => {
    void refresh();
    const timer = setInterval(refresh, 5000);
    return () => clearInterval(timer);
  }, [refresh]);

  // Poll the open pairing so the operator sees the scanned device's code and
  // can compare it before approving. The five-minute deadline is enforced by
  // the engine; this countdown is only a hint.
  useEffect(() => {
    if (!pairing) return;
    let live = true;
    const tick = async () => {
      const next = await engineTry<PairingStatus | null>(
        CMD.PORTAL_PAIRING_STATUS,
        { pairing_id: pairing.pairing_id },
        null,
      );
      if (!live) return;
      setScan(next);
      if (next && (next.state === "expired" || next.state === "cancelled")) {
        setPairing(null);
      }
      if (next && next.state === "approved") {
        setPairing(null);
        void refresh();
      }
    };
    void tick();
    const timer = setInterval(tick, 1500);
    return () => {
      live = false;
      clearInterval(timer);
    };
  }, [pairing, refresh]);

  const act = async (fn: () => Promise<unknown>) => {
    setBusy(true);
    setError(null);
    try {
      await fn();
    } catch (thrown) {
      setError(String(thrown));
    } finally {
      setBusy(false);
      void refresh();
    }
  };

  const ingress = status.ingress || {};
  const published = ingress.reachable_from_phone === true;

  return (
    <div className="screen">
      <header className="page-head">
        <h2>Decisions on a phone</h2>
        <span className="page-sub">
          When a run halts, staff answer the question from their phone. The
          question, the evidence, and the outcome stay on this computer.
        </span>
      </header>

      {error && <Callout tone="warn">{error}</Callout>}

      <Card>
        <CardHead
          eyebrow="Portal"
          title={status.running ? "Running" : "Stopped"}
          sub={
            status.running
              ? `Listening on ${ingress.bind_host}:${status.port}`
              : "Start the portal to pair a phone."
          }
        />
        <div className="row">
          <Pill tone={status.running ? "ok" : "neutral"}>
            {status.running ? "running" : "stopped"}
          </Pill>
          <Pill tone={published ? "ok" : "neutral"}>
            {published ? "published to your network" : "this computer only"}
          </Pill>
          {status.running && status.console_alive === false && (
            <Pill tone="warn">decision service stopped</Pill>
          )}
        </div>
        <div className="row">
          {status.running ? (
            <Button
              variant="ghost"
              disabled={busy}
              onClick={() => act(() => engineInvoke(CMD.PORTAL_STOP, {}))}
            >
              Stop portal
            </Button>
          ) : (
            <Button
              variant="primary"
              disabled={busy}
              onClick={() => act(() => engineInvoke(CMD.PORTAL_START, {}))}
            >
              Start portal
            </Button>
          )}
        </div>
      </Card>

      {!published && (
        <Callout tone="info">
          The portal is available on this computer only. To let a phone reach
          it, publish it through your organization&rsquo;s own HTTPS or VPN
          ingress: set <code>portal_ingress_mode</code> to{" "}
          <code>customer_ingress</code>, set <code>portal_public_origin</code> to
          the https hostname your reverse proxy serves, and set{" "}
          <code>portal_ingress_acknowledged</code> to true. OpenAdapt will not
          widen its own network exposure for you.
          {ingress.error ? ` ${ingress.error}` : ""}
        </Callout>
      )}

      <Card>
        <CardHead
          eyebrow="Pair a phone"
          title="One code, one phone, five minutes"
          sub="Scan, then finish by typing the code the phone shows you."
        />
        {!status.running && <p className="page-sub">Start the portal first.</p>}
        {status.running && !pairing && (
          <Button
            variant="primary"
            disabled={busy}
            onClick={() =>
              act(async () =>
                setPairing(await engineInvoke<Pairing>(CMD.PORTAL_CREATE_PAIRING, {})),
              )
            }
          >
            Show pairing code
          </Button>
        )}
        {pairing && (
          <div className="pairing-panel">
            {pairing.qr_svg ? (
              // An inert data-URI image, rendered locally by the engine from
              // the pairing link. The link never leaves this machine, and an
              // image element cannot execute anything the encoder produced.
              <img
                className="pairing-qr"
                alt="Pairing QR code"
                src={pairing.qr_svg}
              />
            ) : (
              <p className="page-sub">
                Open this link on the phone: <code>{pairing.url}</code>
              </p>
            )}
            {scan?.state === "claimed" ? (
              <div className="stack">
                <p className="page-sub">
                  A device ({scan.device_label ?? "phone"}) scanned this code.
                  Type the code it is showing you. If the phone in your hand is
                  not showing a code, do not approve — cancel instead.
                </p>
                <input
                  className="pairing-input"
                  aria-label="Code shown on the phone"
                  autoComplete="off"
                  spellCheck={false}
                  maxLength={7}
                  placeholder="ABC-123"
                  value={confirmCode}
                  onChange={(event) => setConfirmCode(event.target.value)}
                />
                {scan.attempts_remaining < 3 && (
                  <span className="page-sub">
                    {scan.attempts_remaining} attempt
                    {scan.attempts_remaining === 1 ? "" : "s"} left.
                  </span>
                )}
              </div>
            ) : (
              <p className="page-sub">
                Scan this on the phone. It expires in five minutes and works once.
              </p>
            )}
            {pairing.note && <Callout tone="info">{pairing.note}</Callout>}
            <div className="row">
              <Button
                variant="primary"
                disabled={busy || scan?.state !== "claimed" || !confirmCode}
                onClick={() =>
                  act(async () => {
                    await engineInvoke(CMD.PORTAL_APPROVE_PAIRING, {
                      pairing_id: pairing.pairing_id,
                      confirm_code: confirmCode,
                    });
                    setPairing(null);
                    setScan(null);
                    setConfirmCode("");
                  })
                }
              >
                Approve this phone
              </Button>
              <Button
                variant="ghost"
                disabled={busy}
                onClick={() =>
                  act(async () => {
                    await engineInvoke(CMD.PORTAL_CANCEL_PAIRING, {
                      pairing_id: pairing.pairing_id,
                    });
                    setPairing(null);
                    setScan(null);
                    setConfirmCode("");
                  })
                }
              >
                Cancel
              </Button>
            </div>
          </div>
        )}
      </Card>

      <Card>
        <CardHead eyebrow="Paired phones" title="Devices" />
        {status.devices.length === 0 ? (
          <EmptyState
            title="No phones paired"
            body="Pair a phone above to answer decisions away from this computer."
          />
        ) : (
          <ul className="list">
            {status.devices.map((device) => (
              <li key={device.session_id} className="list-row">
                <span>{device.device_label}</span>
                <Pill tone={device.approved ? "ok" : "warn"}>
                  {device.approved ? "paired" : "awaiting approval"}
                </Pill>
                <Button
                  size="sm"
                  variant="danger"
                  disabled={busy}
                  onClick={() =>
                    act(() =>
                      engineInvoke(CMD.PORTAL_REVOKE_DEVICE, {
                        session_id: device.session_id,
                      }),
                    )
                  }
                >
                  Remove
                </Button>
              </li>
            ))}
          </ul>
        )}
      </Card>
    </div>
  );
}
