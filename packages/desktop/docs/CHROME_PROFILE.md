# Chrome profiles for signed-in portals

Some portals sign in with SSO or two-factor authentication, so a fresh browser
starts every recording and run at the sign-in page. OpenAdapt Desktop can open
Google Chrome on a separate profile that Desktop manages. You sign in there
once, and Chrome keeps those sign-ins. Later recordings and runs attach to that
window and start already signed in.

This isn't your everyday Chrome profile. The first time, the new profile is
empty and you sign in to each site in it.

## How to use it

1. In a Browser target, select **Use a Chrome profile**.
2. Read the notice, name the profile, select **I've read this and agree**, and
   select **Agree**.
3. Select **Open in Chrome**. Desktop opens Google Chrome on the profile at the
   page address you entered and fills **Signed-in browser** with that window's
   local address.
4. Sign in to the site in that window, then record or run as usual.
5. Select **Close Chrome window** when you're done. **Delete profile** turns
   the profile off and deletes its folder, including its sign-ins.

## What Desktop does and doesn't do

- Desktop creates the profile folder under its own data folder
  (`chrome-profiles/<id>/user-data`), readable only by your user account. It
  refuses Chrome's own profile folders.
- Desktop starts only the official Google Chrome build, from its usual install
  location. It refuses Chrome for Testing and Chromium builds.
- Desktop starts Chrome with `--user-data-dir` set to that folder and
  `--remote-debugging-port=0`, so Chrome picks a free local port. It passes no
  keychain or password-store flag, so Chrome stores the profile's data the way
  it always does.
- Desktop never reads, copies or exports the profile's cookies or saved
  passwords, and it never calls the macOS keychain. The only files it reads in
  the profile are Chrome's `DevToolsActivePort` and its lock file.
- Desktop closes only the Chrome process it started, through the process handle
  it holds. It sends `Browser.close` to that process's own debugging address,
  and ends the process only if that fails. It never closes Chrome by name.

## When Desktop refuses

Desktop refuses before anything is attached, and closes a window it started
that it can't verify, in these cases:

| Code | When |
| --- | --- |
| `consent_required` | You haven't agreed to the notice, or the notice changed since you agreed. |
| `profile_in_use` | The profile is already open in a Chrome window Desktop didn't open (`SingletonLock` on macOS, `lockfile` on Windows), or Chrome passed the window to a Chrome that was already running and exited. |
| `attach_unverified` | Chrome didn't report a debugging address, or `/json/version` on that address doesn't name the browser Desktop started (another port, another browser ID, not loopback, or not Google Chrome). |
| `default_profile_refused` | The folder is one of Chrome's own profile folders. |
| `chrome_not_installed` | Google Chrome isn't in its usual install location. |

## Why this mechanism

Three options were considered.

- **Attach to the everyday profile.** Chrome ignores
  `--remote-debugging-port` when it runs on its default profile folder, so this
  isn't supported, and it would expose every site you're signed in to.
- **Copy the everyday profile.** A copy duplicates Chrome's cookie and password
  stores, which is an export of both. On macOS, using the copy also needs the
  keychain entry Chrome encrypts them with. Desktop does neither.
- **A Chrome extension with native messaging.** It needs a store listing or an
  enterprise policy, a native host registered for each browser and operating
  system, and its own consent path. It's an always-installed component that
  reaches no more than a separate profile does.

A separate, Desktop-managed profile on the installed Chrome works the same way
on Windows and macOS, reaches only the sites you signed in to there, and keeps
every credential inside Chrome.

## The debugging port

While the profile is open, Chrome accepts local debugging connections on its
port. Desktop uses that connection to record and run. Other programs running as
you on this computer could connect too, which is why the notice asks you to
close the window when you're done, and why Desktop closes it when Desktop
quits.

## Tests

- `tests/test_engine/test_chrome_profile.py`: consent, every refusal, the
  launch arguments, owned-only close and a static check that the module has no
  cookie, password or keychain call. No Chrome starts.
- `tests/test_e2e/test_chrome_profile_real.py`: real Google Chrome against a
  synthetic sign-in page on loopback. It checks that the sign-in is kept across
  a close and reopen (from the server side), that a second Desktop refuses the
  open profile, and that another Chrome window stays open when Desktop closes
  its own. It runs with `OPENADAPT_REAL_CHROME=1` on the GitHub-hosted macOS and
  Windows runners (`desktop-test.yml`) and on the Windows lab.
