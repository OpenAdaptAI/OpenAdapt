// First screen. It says what OpenAdapt does in one sentence, shows the three
// steps, and offers two ways in. Neither needs an account, and the sample
// needs no operating-system permission. Cloud sign-in stays one click away.
import { ChoiceButton } from "../ui/simple";

const STEPS = [
  {
    title: "Show it",
    body: "Do the task on test data, in the app where you already do it.",
  },
  {
    title: "Check it",
    body: "Confirm what it changes and how it makes sure each save is right.",
  },
  {
    title: "Run it",
    body: "Give it a list. It works through it and asks you only when something doesn't match.",
  },
];

export function Welcome({
  onSample,
  onShow,
  onConnectCloud,
}: {
  onSample: () => void;
  onShow: () => void;
  onConnectCloud: () => void;
}) {
  return (
    <div className="content simple welcome">
      <section className="welcome-hero">
        <h1>Hand off the last step of a task</h1>
        <p className="welcome-lead">
          OpenAdapt enters approved information into the apps you already use,
          like EMRs, payer portals, and desktop or Citrix apps, and checks that
          it saved. When something doesn't match, it stops and asks you instead
          of guessing.
        </p>
      </section>

      <ol className="welcome-steps">
        {STEPS.map((step, index) => (
          <li key={step.title}>
            <span className="welcome-step-number" aria-hidden="true">
              {index + 1}
            </span>
            <div>
              <strong>{step.title}</strong>
              <span>{step.body}</span>
            </div>
          </li>
        ))}
      </ol>

      <div className="choice-row">
        <ChoiceButton
          tone="primary"
          title="See it work on a sample app"
          sub="About 2 minutes. Made-up patients. No account or permissions needed."
          onClick={onSample}
        />
        <ChoiceButton
          title="Show OpenAdapt your task"
          sub="Pick the app, then do the task while OpenAdapt watches."
          onClick={onShow}
        />
      </div>

      <p className="welcome-foot">
        OpenAdapt doesn't send your recordings or tasks anywhere unless you
        choose to share or upload them. Backup, sync, and security software on
        this computer can still copy its files.{" "}
        <button type="button" className="link-button" onClick={onConnectCloud}>
          Connect to OpenAdapt Cloud
        </button>
      </p>
    </div>
  );
}
