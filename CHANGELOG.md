# CHANGELOG



## v0.11.0 (2023-08-29)

### Feature

* feat: add capture.py - also fixes audio recording (#362)

* merge

* Create capture.py

* Update capture.py

* Update capture.py

* it&#39;s finally fixed

* add dependencies

* comment

* move code + use config.CAPTURE_DIR_PATH

* remove debug lines

* Update capture.py

* OpenAdaptCapture -&gt; Capture

* add camera

* Let&#39;s have this off by default.

* hotfix

* fix

* linting

* Create capture.py

* windows

* cleanup + lint

* Update _windows.py

* add audio + new windows recording

* screen_recorder.free_resources()

* Update _windows.py

* isort

* add playback recording

* Update replay.py

* Update replay.py

* Update README.md

* Revert &#34;Update README.md&#34;

This reverts commit 706410358d5e47ceccb34b77d82d5ffd0f3821a8.

* Update README.md

* Revert &#34;Revert &#34;Update README.md&#34;&#34;

This reverts commit 0fe81563e88417d61012e24e1dc55befa960c2ed.

* Update README.md

* run pre-commit

* Update pyproject.toml

* Update openadapt/replay.py

* Update openadapt/replay.py

* Update openadapt/replay.py

* Update replay.py

* update poetry.lock

---------

Co-authored-by: Richard Abrich &lt;richard.abrich@gmail.com&gt;
Co-authored-by: Richard Abrich &lt;richard.abrich@mldsai.com&gt; ([`581b9b8`](https://github.com/OpenAdaptAI/OpenAdapt/commit/581b9b831fe208e143169e90a1e7a02cd1a2c68d))


## v0.10.0 (2023-08-29)

### Feature

* feat: use sentry.io (#386)

* sentry

* check for main branch

* Update openadapt/config.py

* Update openadapt/config.py

* ERROR_REPORTING_ENABLED

* Update config.py

* update poetry.lock

* Update openadapt/config.py

---------

Co-authored-by: Richard Abrich &lt;richard.abrich@gmail.com&gt; ([`90bfe8f`](https://github.com/OpenAdaptAI/OpenAdapt/commit/90bfe8fd8068ad0d684b04f7ba3f06a9121d32d5))


## v0.9.0 (2023-08-28)

### Chore

* chore: add invalid configuration assertion in naive.py (#471)

* Update naive.py

* simplify logical expression

* github action check ([`6040fae`](https://github.com/OpenAdaptAI/OpenAdapt/commit/6040fae7c4504f2339255b54b1b87d94ac183b9d))

* chore: Silence pytest DeprecationWarnings (#407)

* Update pyproject.toml

* tomlkit

* Update config.py

* remove config ([`212452c`](https://github.com/OpenAdaptAI/OpenAdapt/commit/212452ca99d4895b86f7005536e186f39097c10d))

* chore: validate functionality of status checks (#477)

* chore: validate functionality of status checks

* test removing install script testing

* try removing docstring form module to see if its picked up

* add reference to pull request

* add removed docstring back for app __init__ file

* add missing check out statement ([`1485ae4`](https://github.com/OpenAdaptAI/OpenAdapt/commit/1485ae4518cc0deacf24c476a9315256b439f2bd))

### Feature

* feat: enhanced visualize.py with NiceGUI (#406)

* Create visualize1.py

* changes

* fix actions + add dark mode + remove unused code

* Update visualize1.py

* Update visualize1.py

* bump ^ nicegui == 1.2.24

* splitter

* add plot

* updated

* add search button

* isort

* formatting

* make plot dynamic

* Update visualize1.py

* make logo dynamic

* comments

* missed one

* fixed plot bug

* + max table children, deprecate old visualize

* Update visualize.py

* Update config.py

* Update config.py

* Update config.py

* refactor: show -&gt; view_file

* Update visualize.py

* Update visualize.py

* downgrade fastapi

* update config

* linting

* remove debug print

* disabled on_select

* Update visualize.py

* fix visualization with tray

* merge privacy api

* Update visualize.py

* pre-commit

* Update poetry.lock ([`8031a9d`](https://github.com/OpenAdaptAI/OpenAdapt/commit/8031a9d445d9826ec43a46619a5c6ec883eb7497))

### Refactor

* refactor: failing actions (#474)

* add missing dicstrings

* make docstring a bit longer

* fix all flake8 errors

* Add documentation on status checks

* Update README.md

---------

Co-authored-by: Mustafa Abdulrahman &lt;mus2003.abdul@gmail.com&gt;
Co-authored-by: Richard Abrich &lt;richard.abrich@gmail.com&gt; ([`09f4e71`](https://github.com/OpenAdaptAI/OpenAdapt/commit/09f4e715c5c956fd0b3e597bb51efd478a2b2181))

* refactor: style errors from productivity measurement PR (#480)

* check for long gaps in recording

* display basic productivity data in html page similar to visualize

* show images for each window change

* show productivity info for each window

* WIP find tasks/cycles of ActionEvents

* WIP find tasks/cycles of ActionEvents

* WIP implementing brent&#39;s algorithm

* implemented brent&#39;s algorithm

* add task duration info + style changes to display

* comments

* floyd&#39;s algorithm and basic implementation of counting errors

* implement longest repeated non-overlapping substring algorithm

* removed unused code

* small bug fixes

* no empty window titles

* small fixes

* add title

* edge case

* make longest repeated substring recursive

* changed window/tab switching

* add number of actions in a task

* add comments

* make sure no non-repeating tasks are found

* fix off by 1 error with window event screenshots

* remove unused functions, comments, and some refactoring

* docstrings

* black

* Undo changing PLOT_PERFORMANCE

* style changes ([`2089e74`](https://github.com/OpenAdaptAI/OpenAdapt/commit/2089e7435fd32c480d5265dd1b4fab2b1c97e742))

### Unknown

* Update README.md ([`250949f`](https://github.com/OpenAdaptAI/OpenAdapt/commit/250949f18ad6c72df16bce2ab6d32764c0af3a82))

* Productivity measurement (#234)

* check for long gaps in recording

* display basic productivity data in html page similar to visualize

* show images for each window change

* show productivity info for each window

* WIP find tasks/cycles of ActionEvents

* WIP find tasks/cycles of ActionEvents

* WIP implementing brent&#39;s algorithm

* implemented brent&#39;s algorithm

* add task duration info + style changes to display

* comments

* floyd&#39;s algorithm and basic implementation of counting errors

* implement longest repeated non-overlapping substring algorithm

* removed unused code

* small bug fixes

* no empty window titles

* small fixes

* add title

* edge case

* make longest repeated substring recursive

* changed window/tab switching

* add number of actions in a task

* add comments

* make sure no non-repeating tasks are found

* fix off by 1 error with window event screenshots

* remove unused functions, comments, and some refactoring

* docstrings

* black

* Undo changing PLOT_PERFORMANCE ([`56d22a8`](https://github.com/OpenAdaptAI/OpenAdapt/commit/56d22a8fe0e532f671020c8393d07c6b072acac4))


## v0.8.1 (2023-08-21)

### Chore

* chore(*): change installation of spacy weights to runtime (#462)

* remove spacy from
 manual steup instruciton in README.md

* remove spacy
 installion from both the install scripts

* add todo

* test runtime code for spacy installtion

* pyetst passes even if spacy model is not installed

* addressing:

https://github.com/OpenAdaptAI/OpenAdapt/pull/462#issuecomment-1673807055

* add spacy-trnasformers

address comment:
https://github.com/OpenAdaptAI/OpenAdapt/pull/462#issuecomment-1673807055

* skip all the tests in test_scrub if
 spacy miodel is ont installed

* format ([`479937e`](https://github.com/OpenAdaptAI/OpenAdapt/commit/479937ee623fee58587ff24380ea6eb3549ab480))

### Fix

* fix: enhance publish action and authors in pyproject.toml (#463)

* fix: enhance publish action and authors in pyproject.toml

* modify release-and-publish.yml

* change author name to OpenAdapt.AI Team

* use pull_request_target to trigger status checks for any git branch ([`c7813c2`](https://github.com/OpenAdaptAI/OpenAdapt/commit/c7813c2b42e963041a671b9c9c2057c9ea203609))

### Refactor

* refactor: add Privacy design API (#439)

* add Privacy API design code

* add Presidio.scrub_text

* update script to remove linting error

* add test script

* try to fix the pytest error

* try to fix failing test_scrub_api.py

* fixed the pytest error

* change the test file to use the providers code

* refactor presidio_scrub

* fix pytest test_presidio_scrub_text

* fix pytest

* update the scrub_image method header

* add module docstrings

* update base.py

* update providers

* update providers

* add noqa

* update flake8 to
ignore flake8 F821 undefined name &#39;...&#39; error

* remove noqa from line now

* update flake8:
fix undefined error in pylint and falek8

* update privacy api

* fix all pylint errors

* fix all pytlint errors

* fix all pylint errors in base.py

* now pytest passes with 6 warnings

* update visualize to
 use the privacy api providers code

* now visualization works with new Privacy API

* change scrub ebaled to False

* fix pytest

* remove scrbu.py old moidule

* format test script

* format base.py and presidio.py

* remove scrub module

* fix merge

* add spacy runtime code

* remove pytestmark unused variable

* tests get skippped if spacy is not installed

* update utils

* format for final commit ([`5151cb5`](https://github.com/OpenAdaptAI/OpenAdapt/commit/5151cb557599fb3958b04f9e6955229f3cd91b1e))


## v0.8.0 (2023-08-10)

### Chore

* chore: resolve failing publish github actions (#458)

* check if modified flake8 works

* Add release github action

* Add oa-pynput and oa-atomacos and refactor accordingly

* Use simpler publish github action and rename to release-and-publish.yml

* add preview check in main.yml

* add documentation on publishing direct dependencies

* chore: resolve publishing github action errors ([`7b57505`](https://github.com/OpenAdaptAI/OpenAdapt/commit/7b575059d45a2a697fcb2b88f5921a2cc80cf430))

### Feature

* feat: Implement System Tray Icon (#300)

* add tray icon

* move to tray.py

* add notifications &amp; guards

* pyinstaller currently broken

* Update tray.py

* Update cards.py

* hide icon in taskbar + minor improvements

* add dropdown for visualize

* Update tray.py

* Update tray.py

* add py6tray + notifier

* BYE BYE PYSTRAY !!!

hello dependency inversion?

* os.sep + fix console + other

* use path.join

* run -&gt; _run

* add replays

* fix conflicting action items

* fix database is locked + threading

* type annotations

* Update cards.py

* Create visualize1.py

* Revert &#34;Create visualize1.py&#34;

This reverts commit 753f6919e7566b89686922c4e6b2a4fddf33ef04.

* fix typo

* fix merge conflicts : still broken

* fix

* Revert &#34;fix&#34;

This reverts commit 9d844467a9a9f16e219175eb60734b709c6f7b76.

* Revert &#34;fix merge conflicts : still broken&#34;

This reverts commit 82e62d95093e6114653f59b8a7ea94233efc60e1.

* Update pyproject.toml

* styling / linting

* Update tray.py

* Update build.py

* return vals

* Update build.py

* show app

* changes ([`9f2a04e`](https://github.com/OpenAdaptAI/OpenAdapt/commit/9f2a04e8580ae5db62426aeba67eecb89ff09b62))

### Unknown

* fix pytest (#460) ([`1049589`](https://github.com/OpenAdaptAI/OpenAdapt/commit/10495890195acf3bc6a51731f0471fbef31835d7))


## v0.7.1 (2023-08-10)

### Chore

* chore: add pypi action and oa-atomacos and oa-pynput packages (#456) ([`a62d7f3`](https://github.com/OpenAdaptAI/OpenAdapt/commit/a62d7f3898438b4716e31695d32120b228f84d21))

* chore: suppress identical warnings (#389)

* chore: suppress identical warnings

* add max_num_warnings_per_second to limit number of allowed warnings per cond

* remove MAX_NUM_REPEAT_WARNINGS and check for max num warnings per second in utils.py

* Update openadapt/config.py

* address linting errors and set new variable

* use class for filter_log_messages and track message_timestamps with instance variable

* create logging.py to replace logging class using namespace

* test github actions with empty commit

* replaced variable name with MESSAGES_TO_FILTER

* Update openadapt/logging.py

---------

Co-authored-by: Richard Abrich &lt;richard.abrich@gmail.com&gt; ([`7648210`](https://github.com/OpenAdaptAI/OpenAdapt/commit/764821039e3a03cb4dc3ec80b744f61a48c71461))

### Fix

* fix: pypi direct dependency failure (#459)

* remove trf from toml and then ran `poetry update`

* update all neccessary files ([`d638469`](https://github.com/OpenAdaptAI/OpenAdapt/commit/d638469e238eb5fb3d386ca2ad8c64542a10c6c9))


## v0.7.0 (2023-07-28)

### Feature

* feat: scrub toggle for gui (#375)

* add scrub toggle + write dark_mode to env

* Update config.py

* Update util.py

* Update config.py

* address comments

* Update config.py

* run isort

* from first

* Update openadapt/config.py

* Update openadapt/config.py

* Update openadapt/config.py

* Update openadapt/config.py

* Update openadapt/config.py

* Update openadapt/config.py

* add env file path (also where did the toggle go??)

* Update config.py

* Update config.py

* Update config.py

* isort

* Update util.py

* linted

---------

Co-authored-by: Richard Abrich &lt;richard.abrich@gmail.com&gt; ([`1e96a4f`](https://github.com/OpenAdaptAI/OpenAdapt/commit/1e96a4f599b9790509dad865ea3c7b1b254fb19b))


## v0.6.1 (2023-07-28)

### Fix

* fix: modify flake8 config (#429)

* style: modify flake8 config

* add platform check for macOS in pyobjc-framework-avfoundation version

* remove file exclusion in .flake8 and add ignore comment in openadapt/strategies/__init__.py

* resolve lint errors from recent merge ([`f03be2f`](https://github.com/OpenAdaptAI/OpenAdapt/commit/f03be2f9cf100b5bc8dfa34cf76fd60b6eef9f62))


## v0.6.0 (2023-07-26)

### Feature

* feat(crud): compute and save screenshot diff (#367)

* feat(crud): Compute and save screenshot diff

* Add 2 columns in screenshot table to store png_diff_data and png_mask_diff_data.

* CRUD now supports calculation and save screenshots diff data on the flight.

* feat(config): Add SAVE_SCREENSHOT_DIFF environment variable

* SAVE_SCREENSHOT_DIFF indicates that 2 neighbors screenshot will be compared and the difference will be saved to db

* feat(crud): add missing import after merge

* refactor(crud): add missing type annotations

* refactor(crud): add missing type annotations ([`9189bca`](https://github.com/OpenAdaptAI/OpenAdapt/commit/9189bca7aef05ef7801545cc0a55bca54898820a))


## v0.5.8 (2023-07-25)

### Fix

* fix(install): improve powershell script (Issue #372) (#409)

* fix: git reinstallation even if it si present

* fix: Error Message:

Remove-Item : Cannot remove the item at &#39;C:\Users\Krish Patel\hi&#39; because it is in
use.
At line:1 char:9
+         Remove-Item -LiteralPath $setupdir -Force -Recurse
+         ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    + CategoryInfo          : InvalidOperation: (:) [Remove-Item], PSInvalidOperation
   Exception
    + FullyQualifiedErrorId : InvalidOperation,Microsoft.PowerShell.Commands.RemoveIt
   emCommand

* fix: issue that poetry shell gives
&#34;..runnning scripts is disabled on the command line&#34;

* fix: bug #4 issue
 to support multiple version of python (acc. to OpenAdapt standards)

* add start mesage to know
 why sometimes the installtion command exits

* fix the edge case where the new terminal PWD
 is not set to the OpenAdapt folder that was installed

* Update install/install_openadapt.ps1

---------

Co-authored-by: Richard Abrich &lt;richard.abrich@gmail.com&gt; ([`fb3ae1e`](https://github.com/OpenAdaptAI/OpenAdapt/commit/fb3ae1e4b4991776f9c89791636e20c9c7a689a5))


## v0.5.7 (2023-07-25)

### Chore

* chore: format changes with black, and google python docstring, install script testing (#289)

* ran black

* use poetry for install

* Add caching to main.yml for faster github action checks

* Add missing job to main.yml

* try ubuntu latest instead

* change readme type to string instead of array in pyproject.toml

* Update poetry version in main.yml

* try macos

* commented out tests

* see if black command works

* see if black command runs properly

* use black github action

* fix parsing

* use supported black github action

* ignore venv as well

* change args for flake8 action

* try flake8 exclude

* manually add and use flake8

* change docstring keyword

* Chain commands for installing flake8

* exclude .venv

* merged latest changes and add download instruction

* remove import comment

* try python command with ubuntu

* add tesseract-ocr

* add homebrew option

* add cache for tesseract

* add ntlk command with poetry

* ran black

* Add .flake8 and add flake8 to poetry

* Add max length to flake8, add windows job with new install script, and address linting errors

* add some ignore errors in flake8

* Adress more flake8 lint errors

* Change max line length

* ran black

* resolve new changes and ran black

* remove &#34;import nltk; nltk.download(&#39;punkt&#39;)&#34; command

* resolve lint errors in record.py

* remove unnecessary commas

* Address D415, sentence/comma missing on first line error

* Resolve F403

* Address D200, single line doctstrings

* Addressed E731, F841, D205 errors

* resolve F401, unused imports

* Address return type annotations

* Add type annotations for function arguments

* ran black

* Address &#39;Multi-line docstring summaries should start at the first line&#39;

* Revert main.yml to test if all checks pass

* fix syntax issue in main.yml

* Use cache for install poetry dependencies

* Revert main.yml and convert black action to oneliner with poetry

* Address lint errors for newly merged changes

* Try caching tesseract install

* test if tesseract is cached

* try cache brew package

* try cache brew again

* Remove failing tesseract cache

* Address lint errors from recent merge

* replace test command

* address comments on annotations and ran black

* change todo

* Shorten word line max length to 88 for comments

* try executing script

* keep windows runner as todo

* comment poetry shell in install_opendapt.sh

* add error flag in mac install script to avoid poetry shell in github actions

* try to see if this will pass

* update main.yml

* Address incorrect function annotations in record.py

* Address annotation errors in _windows.py

* Addressed more general annotations

* Address incomplete annotations in util.py

* run isort and other precommit hooks with custom black config on all files

* Update openadapt/crud.py

* replaced more &#39;Any&#39; type annotations

* resolve lint errors from recent merge

* ran black

* resolve circular imports, and address Any annotations

---------

Co-authored-by: Aaron &lt;57018940+0dm@users.noreply.github.com&gt;
Co-authored-by: Richard Abrich &lt;richard.abrich@gmail.com&gt; ([`c815924`](https://github.com/OpenAdaptAI/OpenAdapt/commit/c81592425b4efefdb48b818508f7debfc3ac95c3))

### Fix

* fix: issues after latest merge (#424)

* merge

* hotfix

* fix: resolve annotation errors and restore incorrectly removed code in lint PR

* fix

* Update models.py

* run black

* Update openadapt/models.py

---------

Co-authored-by: Mustafa Abdulrahman &lt;mus2003.abdul@gmail.com&gt;
Co-authored-by: Richard Abrich &lt;richard.abrich@gmail.com&gt; ([`2d4e8c6`](https://github.com/OpenAdaptAI/OpenAdapt/commit/2d4e8c6fee245bfdc4726f113b27367a36ae96c7))


## v0.5.6 (2023-07-20)

### Chore

* chore: add preview option to black pre-commit hook and update README (#405) ([`6a42eb7`](https://github.com/OpenAdaptAI/OpenAdapt/commit/6a42eb762b39b70313ecbe5014377c376ccc26e9))

* chore: configure isort with black profile and add black precommit hook (#384)

* chore: configure isort with black profile and add black precommit hook

* update black version

* add advanced isort settings to match CONTRIBUTING.md ([`2e7496b`](https://github.com/OpenAdaptAI/OpenAdapt/commit/2e7496b64445de965d6274e83987edb83f73772e))

### Documentation

* docs: replace MLDSAI with OpenAdaptAI in README.md (#402) ([`750cf1d`](https://github.com/OpenAdaptAI/OpenAdapt/commit/750cf1d327bf22453da1d2f0cc1505bbc52765f4))

* docs: update README (installation table) (#398) ([`1212198`](https://github.com/OpenAdaptAI/OpenAdapt/commit/121219866942df82ab8624e4580b254ca5b9c2b0))

* docs: update readme (take transpose of installation table) (#397) ([`fe39c73`](https://github.com/OpenAdaptAI/OpenAdapt/commit/fe39c73999b7221524442a11e0814b02faca78ce))

* docs: Update README.md (#390) ([`8745969`](https://github.com/OpenAdaptAI/OpenAdapt/commit/8745969ec9481348932146a46e91153f333c948d))

### Fix

* fix(merge_consecutive_keyboard_events): Fix reference before assignment (#411)

Co-authored-by: Seyed Morteza Hosseini &lt;seyedmortezahosseini@Seyeds-MacBook-Pro.local&gt; ([`e136b28`](https://github.com/OpenAdaptAI/OpenAdapt/commit/e136b28731d09745a23adb11cc5309ec86b7fdd9))


## v0.5.5 (2023-07-17)

### Fix

* fix: loguru exception handling for invalid recordings (#361)

* raise ValueError

* improve exception handling

* Update crud.py

* use assert

* reorganize catch

* Update openadapt/replay.py

* Update openadapt/visualize.py

* Update openadapt/events.py

* Update openadapt/crud.py

---------

Co-authored-by: Richard Abrich &lt;richard.abrich@gmail.com&gt; ([`7aba45b`](https://github.com/OpenAdaptAI/OpenAdapt/commit/7aba45be73cb3cc4807f08ebc7bf36a9e9e5c2fb))


## v0.5.4 (2023-07-17)

### Fix

* fix(utils): prevent duplicate log messages (#339)  (#366)

* fix(utils): prevent duplicate log messages (#339)

Acquire a lock in utils.configure_logging. Otherwise this function has
a race where two threads can both call logger.remove(), and then both
call logger.add(), creating two identical sinks.

* refactor(record): remove redundant configure_logging calls

configure_logging is already called at the module level so there&#39;s no
need to call it with identical arguments in various functions.

* fixup! fix(utils): prevent duplicate log messages (#339) ([`cf1a782`](https://github.com/OpenAdaptAI/OpenAdapt/commit/cf1a7825692b787e72eb4459c302371dab31e952))

* fix: add test fixtures (#356)

* fix: add test fixtures

* revert test_summary.py ([`0ee5397`](https://github.com/OpenAdaptAI/OpenAdapt/commit/0ee5397ea1ab4b324e5fd699b858fe30892a3491))


## v0.5.3 (2023-07-17)

### Fix

* fix: support more objective-c objects (#323)

* Update _macos.py

* Update _macos.py

* Update _macos.py

* add bug report option

* Update _macos.py

* undo formatting

* refactor

* split

* remove debug line

* Update _macos.py

* Update _macos.py

* Update _macos.py

* Update _macos.py

* reorganize

* Update _macos.py

* Update _macos.py

* nonetype

* Update _macos.py ([`be0774f`](https://github.com/OpenAdaptAI/OpenAdapt/commit/be0774fc8e5004830c7b0a578432f04b849ff68b))


## v0.5.2 (2023-07-17)

### Chore

* chore: add isort as pre-commit hook (#319)

* Add precommit hooks for isort, check-yaml, end-of-file-fixer, trailing-whitespaces

* Updated README on setting up precommit hooks

* Add updated poetry.lock

* Fix step numbering of pre-commit section in README.md ([`72da341`](https://github.com/OpenAdaptAI/OpenAdapt/commit/72da3414d782e2da75a818971572e003920ae0e0))

### Documentation

* docs: Create Use Case issue template (#324)

* initial draft

* new line issue

* update link

* address comment

* add numbers ([`20fb5a3`](https://github.com/OpenAdaptAI/OpenAdapt/commit/20fb5a3a3a1ab9969c7cb401f01a6e581980de7b))

* docs: update `install` section of README.md (#344)

* run `poetry lock --no-update`

* fix: update CONTRIBUTING.md

fix typo in 3 words and make pytest code word

* fix: add required dependencies

* add seup.md

* add setup.md

* remove typo

* rename temporarily

* rename to SETUP.md

* update README to include more documentation on

-- when someone should use the install script vs. follow the steps here?

* remove requirements.txt

* add link to installation

* rename typo: prerequisite

* link to start: https://openadapt.ai/#start

* reomve .venv as requirements.txt is no longer used

* update installation instructions

* update the Windows Installtion command (in favour of):

1. https://github.com/OpenAdaptAI/OpenAdaptWeb/pull/30/files#r1252126510
2. https://github.com/OpenAdaptAI/OpenAdaptWeb/pull/30/files#r1252128457

* addressing comment:
https://github.com/OpenAdaptAI/OpenAdapt/pull/344#discussion_r1252173873

* address the comment:

https://github.com/OpenAdaptAI/OpenAdapt/pull/344#discussion_r1252175219

* address the comment:

https://github.com/OpenAdaptAI/OpenAdapt/pull/344/files#r1252175602

* add .venv to address the comment:

https://github.com/OpenAdaptAI/OpenAdapt/pull/344/files#r1252172932

and

https://github.com/OpenAdaptAI/OpenAdapt/pull/289/files#r1252150636 ([`755dd55`](https://github.com/OpenAdaptAI/OpenAdapt/commit/755dd555a6cda5bb99ac21893a7cbefb4c73bf0b))

### Fix

* fix: windows.get_active_window() (#333)

* return top_window of the active app

* remove runtime exception

* handle runtime error

* handle COMerror

* add logger.warning

* add logger.warning

* add get_properties

* remove unnecessary imports

* remove -&gt; Desktop

* monkey patching

* fix get_properties

* monkey patch __class__

* fix monkey patching

* format with black ([`d3f07c9`](https://github.com/OpenAdaptAI/OpenAdapt/commit/d3f07c9892bbf9b6682788486d89c64cf2443537))

### Unknown

* update `config.SCRUB_ENABLED` to False (#373) ([`3f32883`](https://github.com/OpenAdaptAI/OpenAdapt/commit/3f328830380468e95932e1fab9c2c390112069a3))


## v0.5.1 (2023-07-04)

### Fix

* fix: add pympler and psutil ([`f853839`](https://github.com/OpenAdaptAI/OpenAdapt/commit/f8538398d3c0da51dbd6fb56f3e891c7558df6fb))


## v0.5.0 (2023-07-03)

### Feature

* feat: add progress bar in `record.py` and `visualize.py`

* run `poetry lock --no-update`

* add alive-progress via poetry and in code

* add progress bar in visualization

* add a check for MAX_EVENT = None

* update the title for the Progress bAr
 (better for USer pov)

* update the requirement.txt

* ran ` black --line-length 80 &lt;file&gt;`
on record.py and visualize.py

* remove all progress bar from record

* add tqdm progress bar in recrod.py

* add tqdm for visualiztion

* remove alive-progress

* consistent tqdm api

--add dynamic_cols: to enable adjustments when window is resized

Order:
--total
-description
--unit
--Optional[bar_format]
--colour
--dynamic_ncols

* Update requirements.txt

Co-authored-by: Aaron &lt;57018940+0dm@users.noreply.github.com&gt;

* Address comemnt:
https://github.com/MLDSAI/OpenAdapt/pull/318#discussion_r1244598202

* remove incorrect indent

* remove rows

* try to fix distorted table in html

* add custom queue class

* lint --line-length 80

* fix `NotImplementedError` for MacOs
-- using custom MyQueue class

* rename custom -&gt; thirdparty_customization

* rename to something useful

* address comments

* rename dir to customized_imports

* rename to extensions
https://github.com/MLDSAI/OpenAdapt/pull/318#discussion_r1246073970

---------

Co-authored-by: Aaron &lt;57018940+0dm@users.noreply.github.com&gt; ([`3e12fd4`](https://github.com/OpenAdaptAI/OpenAdapt/commit/3e12fd4910f41da17506fae75f3d459c5b26e42b))


## v0.4.0 (2023-07-03)

### Feature

* feat(install): download and install MacOS dependencies

* add messages

* add download python line

* created cleanup func

* helper functions created

* partially down python downloading

* python installation script complete

* added git (and brew)

* added git (and brew)

* add tesseract and refactor

* works on current macbook

* install python with brew

* install python with brew

* check if pyenv is needed

* merge2

* merge2

* simplify brew call

* finished testing

* Less output

* split line

* Revert adding svg file
This reverts commit 5601407207b085c254cf86b2f425be2eddd741c1.

* remove unnecessary things

* remove ps1 edit

* remove ps1 edits

* make the ps1 the same

* Remove space

* add poetry and aesthetic changes

* small changes

* add brew to path if needed

* switch to pip3

* switch to pip3.10 ([`25b70c0`](https://github.com/OpenAdaptAI/OpenAdapt/commit/25b70c0af2d33c92003b574c56f55b027321b162))


## v0.3.1 (2023-07-03)

### Fix

* fix(window): prevent SegmentationFault ([`cf0fc0b`](https://github.com/OpenAdaptAI/OpenAdapt/commit/cf0fc0bebb984b1bc3b9f61920bf8d048361b220))


## v0.3.0 (2023-07-02)

### Feature

* feat(record): memory profiling

* tracemalloc

* pympler

* todo

* changed position of tracemalloc stats collection

* updated requirements.txt

* memory leak fix and cleanup

* removed todo

* changed printing to logging

* alphabetical order

* changes to tracemalloc usage

* plot memory usage

* memory writer terminates with performance writer

* add MemoryStat table to database

* remove todo

* switch from writing/reading memory using file to saving/retrieving from database

* add memory legend to performance plot

* prevent error from child processes terminating

* style changes

* moved PLOT_PERFORMANCE to config.py

* only display memory legend if there is memory data

* moved memory logging into function

* removed unnecessary call to row2dicts

* rename memory_usage to memory_usage_bytes

* replaced alembic revision

* remove start_time_deltas; minor refactor

* fix indent

---------

Co-authored-by: Krish Patel &lt;65433817+KrishPatel13@users.noreply.github.com&gt;
Co-authored-by: Richard Abrich &lt;richard.abrich@mldsai.com&gt;
Co-authored-by: Richard Abrich &lt;richard.abrich@gmail.com&gt; ([`ef0d5eb`](https://github.com/OpenAdaptAI/OpenAdapt/commit/ef0d5ebcf400c216c0b16c7ffb552391544b0a0c))


## v0.2.0 (2023-07-02)

### Documentation

* docs: contributing

* Update README.md

* Update pull_request_template.md

* Add import order

* Add Poetry conflicts

* Add Richard&#39;s better import order explanation: ([`5b9f735`](https://github.com/OpenAdaptAI/OpenAdapt/commit/5b9f735da42c0bcdda2e47747f2acabb0f5f45fd))

### Feature

* feat: stop listener

* created listener for &#34;oa.stop&#34; sequence

* fixed issue with comparing objects of diff types

* moved list of sequences to config.STOP_SEQUENCES and changed code to accomadate multiple stop sequences, + minor changes to naming and logging

* moved list of stop sequences to config.STOP_SEQUENCES

* filter out stop sequence in crud.get_action_events

* combined keyboard listeners for macOS compatability

* style changes

* code cleanup

* special char support

* change to config.STOP_STRS and split by character in record.py and crud.py

* black

* add todo and fix special char functionality

* fix filter_stop_sequences

* added SPECIAL_CHAR_STOP_SEQUENCES and STOP_SEQUENCES that combines STOP_STRS and SPECIAL_CHAR_STOP_SEQUENCES

* STOP_SEQUENCES moved to config.py

* black

* black ([`385963c`](https://github.com/OpenAdaptAI/OpenAdapt/commit/385963ce098fbb9bdbe7adce8be97ea6213b0c20))

### Unknown

* Update config.py (#332) ([`aa49489`](https://github.com/OpenAdaptAI/OpenAdapt/commit/aa49489c0d8d116cae9bcd5ab9747cef52096b15))


## v0.1.0 (2023-06-29)

### Chore

* chore(release): Use PAT token to bypass branch protection for release github action (#326)

* feat(release): Initial development version 0.1.0

* use PAT token to bypass branch protection for release github action ([`d456bc2`](https://github.com/OpenAdaptAI/OpenAdapt/commit/d456bc2c7e888d76258a9c966a69becba1660f1d))

### Documentation

* docs: Add note on making major release in CONTRIBUTING.md ([`e097189`](https://github.com/OpenAdaptAI/OpenAdapt/commit/e0971897c9210a614cc65c1e1e69d510bb7a61ed))

### Feature

* feat(install): install tesseract, remove vcredist (windows)

* test only tesseract

* test only tesseract

* test tesseract only

* uncomment the last line

* try to redirect

* change the description for failed message

* add OCR

* test python

* format function calling

* Update Description

* prepare for git test

* test Git

* update the VSCppRedistCMD

* using Write-Host

* Use &#39;;&#39; for the return command

* full test

* add description for all arguments

* test tesseract on pc with out it

* fix order for the installtion

* uncomment the remove-gitinstaller

* Stop deletion of Openadapt if the pytest fails

* add the Cleanupfailure parameter to the apt comman

* make user oriented messages

* rewrite comments

* do not exit on pytest fail

* remove VS C++ Redistributable

* remove VS C++ installtion consts

* fix pytest fail exit

* redirect the output to null

* rename the param

* remove unneccessary line

* try a new powershell window for poetry shell

* add colours to messages

* try to fix NSIS error

* fix file name

* different approach to fix NSIS

* different approach to fix tesseract

* try to fic NSIS error

* try to fix NSIS error

* remove line

* remove uneccssary comment

* remove unsupported dcostrings

* remove unused arugs

* redirect output to null

* ran `poetry lock --no-update` to fix the warning

* Address comment:
https://github.com/MLDSAI/OpenAdapt/pull/312#discussion_r1244105576

* Address comment:
https://github.com/MLDSAI/OpenAdapt/pull/312#discussion_r1244073425

* Address comment:
https://github.com/MLDSAI/OpenAdapt/pull/312#discussion_r1244070600

* imporve resuability and readability

* remove redirection to null ([`ddb7d78`](https://github.com/OpenAdaptAI/OpenAdapt/commit/ddb7d78d28dfbfb4c7004d1feede67ce743e916d))

* feat(release): Initial development version 0.1.0 (#325) ([`5ab0e01`](https://github.com/OpenAdaptAI/OpenAdapt/commit/5ab0e011cf42b61fd8ebc41035e29c14583fd7de))

### Unknown

* Merge pull request #178 from Mustaballer/publishing-with-pypi

Automatic Publishing with PyPi ([`a37b62f`](https://github.com/OpenAdaptAI/OpenAdapt/commit/a37b62f32b30fe13484846408fea28fa0c344fd0))

* Update CONTRIBUTING.md referring to comments ([`84b623e`](https://github.com/OpenAdaptAI/OpenAdapt/commit/84b623e466f0cb26ac0726d295a0930be64c70bc))

* Merge pull request #306 from dianzrong/expand-on-demo-replay-strategy

Fix Summary test ([`d56dd57`](https://github.com/OpenAdaptAI/OpenAdapt/commit/d56dd572b106a9ccb46022e9a85ecbfc1f12357f))

* Merge pull request #229 from angelala3252/feat/tracing

Implement tracing ([`5cdc8fd`](https://github.com/OpenAdaptAI/OpenAdapt/commit/5cdc8fd16d717cbe4fe7e32f855e2034a72fd8f7))

* Merge pull request #315 from MLDSAI/feat/max_table_str_len

max_table_str_len ([`c92e2e6`](https://github.com/OpenAdaptAI/OpenAdapt/commit/c92e2e606ee0f6b766e67473e95d074f8c1cb207))

* max_table_str_len ([`4051804`](https://github.com/OpenAdaptAI/OpenAdapt/commit/40518049c92a5ab56774f49e2ebcc5c2169c12dd))

* Merge pull request #201 from dianzrong/update

Implement Updating ([`27b89b7`](https://github.com/OpenAdaptAI/OpenAdapt/commit/27b89b7925b85b5157af02449cbeee1ddebd2dd3))

* Merge pull request #313 from MLDSAI/feat/obfuscate_config_logs

config.obfuscate ([`495a280`](https://github.com/OpenAdaptAI/OpenAdapt/commit/495a2804baf02f13a3e433ec3cb3ed1037cce3ea))

* don&#39;t obfuscate defaults ([`abac4e5`](https://github.com/OpenAdaptAI/OpenAdapt/commit/abac4e50acb728679248cdb160621b8361ab6236))

* remove publish action and rename to release.yml ([`9b340ee`](https://github.com/OpenAdaptAI/OpenAdapt/commit/9b340eebc1ca439a659bd781fc234d7e36809927))

* config.obfuscate ([`79c41f5`](https://github.com/OpenAdaptAI/OpenAdapt/commit/79c41f5af2a79a4739d42a696bb37eb98374f1dc))

* comments ([`a87e202`](https://github.com/OpenAdaptAI/OpenAdapt/commit/a87e20247926993821c147232d014f0cfd779e86))

* Merge pull request #310 from KrishPatel13/feature/install

Add Tesseract OCR in for powershell script ([`ac27a79`](https://github.com/OpenAdaptAI/OpenAdapt/commit/ac27a7996df90101860111c238272510509b6747))

* remove python3.10 as it works on mac not on window ([`52b0f58`](https://github.com/OpenAdaptAI/OpenAdapt/commit/52b0f58b7f24db2524ce3a21668e2ccca9e05a77))

* for cosistency remove the paranthesis ([`47019d6`](https://github.com/OpenAdaptAI/OpenAdapt/commit/47019d664afba550c034f78db2e0467aa3ff729e))

* refresh the path everytime ([`55b288a`](https://github.com/OpenAdaptAI/OpenAdapt/commit/55b288ad3b0e7a7365e1e73afe7c47d33a237884))

* remove uneccessary code ([`16c18e5`](https://github.com/OpenAdaptAI/OpenAdapt/commit/16c18e5c521a9460ef1db812b59978b5098dd25b))

* ran format document ([`acef0be`](https://github.com/OpenAdaptAI/OpenAdapt/commit/acef0bebdb33efb5a1948f760534a18347828bb9))

* add function for Vs C++ redist

and beautify hte code for better readability ([`2029cc4`](https://github.com/OpenAdaptAI/OpenAdapt/commit/2029cc4501939ba328407181e4f29b1d32a3a70d))

* made a function for git installtion ([`babe791`](https://github.com/OpenAdaptAI/OpenAdapt/commit/babe791d7d0010133b853daed8516ff8b16d6d3b))

* remove redundant if condition and also fails on windows:
as python3.10 is not a valid command ([`388ce67`](https://github.com/OpenAdaptAI/OpenAdapt/commit/388ce6756b811ccc826a0fd5ce791e6c1859b063))

* simplify the refresh command ([`ab56341`](https://github.com/OpenAdaptAI/OpenAdapt/commit/ab56341d9a98643db4b143d0721fc88ec7a781cc))

* Organize Tesseract Installation ([`d3f479e`](https://github.com/OpenAdaptAI/OpenAdapt/commit/d3f479e98e2782c5337a283ac0f69201906971bf))

* Merge pull request #292 from KrishPatel13/feature/scrub_mp4

Scrubbing for mp4 files ([`fee2c72`](https://github.com/OpenAdaptAI/OpenAdapt/commit/fee2c72e6b67fac5f84a9bd91c2d78dcc8e901a6))

* organize imports ([`f2671f7`](https://github.com/OpenAdaptAI/OpenAdapt/commit/f2671f7ba6715e5874fbec986ba9d01d62ba4d0e))

* Merge branch &#39;main&#39; into feature/scrub_mp4 ([`e81ac67`](https://github.com/OpenAdaptAI/OpenAdapt/commit/e81ac67f8c71c8d32d3f442450e68915148ef0bf))

* Merge pull request #307 from 0dm/add-python-levenshtein ([`dd36aec`](https://github.com/OpenAdaptAI/OpenAdapt/commit/dd36aecbc2ab54c757ebb5fdc125926343e2ca1d))

* remove typo ([`bf412a3`](https://github.com/OpenAdaptAI/OpenAdapt/commit/bf412a37bac35a77176fa5edaaabeeae4a183ddb))

* add Poetry to .ps1 script ([`45f8978`](https://github.com/OpenAdaptAI/OpenAdapt/commit/45f8978d487a0731a07877d9556d4eb391805c7d))

* Add Tesseract OCR in ps1 ([`d078183`](https://github.com/OpenAdaptAI/OpenAdapt/commit/d078183063f56ecda832c9e99dc12b09c76bcb63))

* add guard for module ([`4de7536`](https://github.com/OpenAdaptAI/OpenAdapt/commit/4de753678ec6bc3b6129c55c840e38287b0e91a1))

* tpyo in docstring ([`61831c8`](https://github.com/OpenAdaptAI/OpenAdapt/commit/61831c82987c69c38abee2778fabfb4dcf5341b0))

* add mmoviepy in requirements ([`9f3cc39`](https://github.com/OpenAdaptAI/OpenAdapt/commit/9f3cc3941a2ad9d5d95480bc7368561e4fb502ad))

* add the following
--scrub_all_entities
--crop_start_time
--crop_end_time
--playback_speed_multiplier ([`d760390`](https://github.com/OpenAdaptAI/OpenAdapt/commit/d760390cb72cc975c514af662a8354a16e46a57b))

* add moviepy via poetry ([`a6a352b`](https://github.com/OpenAdaptAI/OpenAdapt/commit/a6a352bb040b697040c63019e3a0d95bb36457b8))

* Update requirements.txt ([`95ee413`](https://github.com/OpenAdaptAI/OpenAdapt/commit/95ee413bb8b66cfe835d43945934f5ed74475980))

* Add run app ([`90cfaa9`](https://github.com/OpenAdaptAI/OpenAdapt/commit/90cfaa9dc58b989316dd7b9c44d20cef4af7f567))

* add python-Levenshtein ([`8f386de`](https://github.com/OpenAdaptAI/OpenAdapt/commit/8f386defb3998aaec092528b51586914551eb617))

* Reformat ([`a35bbea`](https://github.com/OpenAdaptAI/OpenAdapt/commit/a35bbeaaab830e7491cfbcc1f67d5ad653ad0bd3))

* Fix typo ([`fc44273`](https://github.com/OpenAdaptAI/OpenAdapt/commit/fc44273d1681a36414ca94e799eee073920ab169))

* merged ([`0c95a2d`](https://github.com/OpenAdaptAI/OpenAdapt/commit/0c95a2d05c4dd994021b47737a567f74ca3ecc0f))

* small fixes ([`e3178fe`](https://github.com/OpenAdaptAI/OpenAdapt/commit/e3178fe17c1732b1c19a21597583b508eb60121e))

* small fixes ([`dfe46e8`](https://github.com/OpenAdaptAI/OpenAdapt/commit/dfe46e80fc1db65a756d065144cb0f389ddd9fd8))

* Merge branch &#39;update&#39; of https://github.com/dianzrong/puterbot into update ([`754c921`](https://github.com/OpenAdaptAI/OpenAdapt/commit/754c921f935a9951ebf85b9585a23a51d2a9f248))

* after initial test ([`2f7debc`](https://github.com/OpenAdaptAI/OpenAdapt/commit/2f7debcc52c1d2491a7a8d8ed7fc21f06ba0769e))

* after initial test ([`18b0e4c`](https://github.com/OpenAdaptAI/OpenAdapt/commit/18b0e4c5e3cd2edccbefbeec228d0aa6460012d5))

* add newlines after docstrings ([`60dceb2`](https://github.com/OpenAdaptAI/OpenAdapt/commit/60dceb2716e0f097f715dcc1ef66c68aab43f112))

* better log ([`655a968`](https://github.com/OpenAdaptAI/OpenAdapt/commit/655a96807a929b4b7d8674047aa92d1623577111))

* log exception ([`4b9c68a`](https://github.com/OpenAdaptAI/OpenAdapt/commit/4b9c68a5e6dc154eef2bb09dc004c56175b9e01c))

* log exception ([`8686418`](https://github.com/OpenAdaptAI/OpenAdapt/commit/8686418e05aada677c9434974630415d11552fb0))

* merge ([`9f067a4`](https://github.com/OpenAdaptAI/OpenAdapt/commit/9f067a48257e53ebe632e4e846e4421e4c5c1234))

* Merge branch &#39;MLDSAI:main&#39; into expand-on-demo-replay-strategy ([`18a1d29`](https://github.com/OpenAdaptAI/OpenAdapt/commit/18a1d2924b0198396cd03ddbe009f660f3a32854))

* add checker for punkt ([`4e5866d`](https://github.com/OpenAdaptAI/OpenAdapt/commit/4e5866d907a714834beaea34a9c990daa451e609))

* Merge branch &#39;MLDSAI:main&#39; into feature/scrub_mp4 ([`b25c102`](https://github.com/OpenAdaptAI/OpenAdapt/commit/b25c102cbec4d6a420f7ba948b3a0b0ee455e820))

* add checker for punkt ([`4bcb474`](https://github.com/OpenAdaptAI/OpenAdapt/commit/4bcb474b4cdf7d95bed21f795c206c9466eb972e))

* add check for tokenizer ([`0ed8dfd`](https://github.com/OpenAdaptAI/OpenAdapt/commit/0ed8dfda4bd9bb354a8cc4449877663675acd7c8))

* Merge pull request #298 from KrishPatel13/fix/pywinauto

add pywinauto using poetry ([`5c443a9`](https://github.com/OpenAdaptAI/OpenAdapt/commit/5c443a977b9ce07ebefa006e0d322a1b8d3604ed))

* temporary changes to
time performance of specific lines ([`683c3cf`](https://github.com/OpenAdaptAI/OpenAdapt/commit/683c3cfb9306d3bc6fb560b9427208b32b6a3bea))

* Merge branch &#39;MLDSAI:main&#39; into feature/scrub_mp4 ([`8b23e20`](https://github.com/OpenAdaptAI/OpenAdapt/commit/8b23e200d371a0777ae463757a1e4b6b7ea252e8))

* Merge branch &#39;fix/pywinauto&#39; of https://github.com/KrishPatel13/OpenAdapt into fix/pywinauto ([`fc9755f`](https://github.com/OpenAdaptAI/OpenAdapt/commit/fc9755f54503da5970cc6bc666ba6b8fbe503791))

* add marker for pywinauto and
ran poetry update ([`ee0a8e2`](https://github.com/OpenAdaptAI/OpenAdapt/commit/ee0a8e258f50ab2ab7b343212b7a68928d2aa80f))

* Merge branch &#39;MLDSAI:main&#39; into fix/pywinauto ([`224cde1`](https://github.com/OpenAdaptAI/OpenAdapt/commit/224cde1cad750ce0daec6702a974ebaded03134f))

* Merge pull request #297 from dianzrong/expand-on-demo-replay-strategy

Refactored test to use custom replay strategy ([`767e362`](https://github.com/OpenAdaptAI/OpenAdapt/commit/767e362810d327627873cc6e7367bf4fd8f934d7))

* Merge pull request #299 from MLDSAI/abrichr-patch-3

Update README.md ([`3b72e81`](https://github.com/OpenAdaptAI/OpenAdapt/commit/3b72e81caaee044df1fdc74f554aea449fa38997))

* Update README.md ([`a7ea91d`](https://github.com/OpenAdaptAI/OpenAdapt/commit/a7ea91d98ac84708b849a79a4cee2155855f02d9))

* ran black ([`ee26831`](https://github.com/OpenAdaptAI/OpenAdapt/commit/ee268317bed793ab888dc5a62e07f18859b65e1f))

* Typo ([`5263afa`](https://github.com/OpenAdaptAI/OpenAdapt/commit/5263afa216460b751b3483caf52bd907aa35223f))

* diff strategy name ([`84a7de7`](https://github.com/OpenAdaptAI/OpenAdapt/commit/84a7de71c382107ab20d405a67b8b9597ac8ca84))

* linted ([`e976414`](https://github.com/OpenAdaptAI/OpenAdapt/commit/e976414bcaad492ffc8095f4d817d89927a5f359))

* add pywinauto using poetry ([`1682be3`](https://github.com/OpenAdaptAI/OpenAdapt/commit/1682be3ee95dcbc4604dcf3586651303c910ad47))

* added custom ReplayStrategy to tests ([`0a9ee9d`](https://github.com/OpenAdaptAI/OpenAdapt/commit/0a9ee9d24638f3a8edd0d84e550009b1d2af5ea3))

* Addressed comment:
https://github.com/MLDSAI/OpenAdapt/pull/292#discussion_r1235435386 ([`93b04d1`](https://github.com/OpenAdaptAI/OpenAdapt/commit/93b04d1ac6193c297703c94bcfdb70f03faec9bb))

* Addressed comment:
https://github.com/MLDSAI/OpenAdapt/pull/292/files/3e3c8b33a5b7c985bb6be23adcdac4edea8b1636#r1235436174 ([`f43cdef`](https://github.com/OpenAdaptAI/OpenAdapt/commit/f43cdefe7bd130db098e8148597cb3bcceae5b61))

* Addressed comment:
https://github.com/MLDSAI/OpenAdapt/pull/292#discussion_r1235436360 ([`84a5162`](https://github.com/OpenAdaptAI/OpenAdapt/commit/84a516226823c33c631af5b0dbceceb32afe6c66))

* addressed
https://github.com/MLDSAI/OpenAdapt/pull/292#discussion_r1235432965 ([`00fb7d6`](https://github.com/OpenAdaptAI/OpenAdapt/commit/00fb7d6077d71fe5cac385c049db6ab1e808bd71))

* remove the sample files and resources dir ([`6cb6217`](https://github.com/OpenAdaptAI/OpenAdapt/commit/6cb6217e61655d2b2adcec242c51e581fef05e4b))

* added documentation ([`ed26aad`](https://github.com/OpenAdaptAI/OpenAdapt/commit/ed26aad56183d74473c5d81e442e08df2b41e45c))

* Merge branch &#39;MLDSAI:main&#39; into update ([`b961eab`](https://github.com/OpenAdaptAI/OpenAdapt/commit/b961eabeeaa1822ab114b2edd4524f888624f6f4))

* remove unrequired import from requirements.txt ([`3e3c8b3`](https://github.com/OpenAdaptAI/OpenAdapt/commit/3e3c8b33a5b7c985bb6be23adcdac4edea8b1636))

* formatting and test ([`e93eece`](https://github.com/OpenAdaptAI/OpenAdapt/commit/e93eece99915f00621c1f89fd6cd9414a74a4655))

* minor change to scrub.py ([`4b194d4`](https://github.com/OpenAdaptAI/OpenAdapt/commit/4b194d429a9a65e37d90ebf5177fd46bb67ddb7e))

* add scrub under scripts/ to scrub mp4 ([`3ff2346`](https://github.com/OpenAdaptAI/OpenAdapt/commit/3ff234603d8e98055e4a69c5fa561bee455a280d))

* add ffmpeg-python in requirements.txt and
add sample mp4 video under resources/ ([`4dc03bf`](https://github.com/OpenAdaptAI/OpenAdapt/commit/4dc03bf2bf5362cec9e859cffc9c1e419e309290))

* style changes ([`fac1efb`](https://github.com/OpenAdaptAI/OpenAdapt/commit/fac1efb4e026bad59df92e40951d2c2e3dc42823))

* renamed @logging decorator to @trace, style changes ([`db24c49`](https://github.com/OpenAdaptAI/OpenAdapt/commit/db24c49d6d83631b825b6706c3efa316f9ac5030))

* Update __init__.py ([`04d407d`](https://github.com/OpenAdaptAI/OpenAdapt/commit/04d407d2846cbf2025075f30453371562400668a))

* Merge branch &#39;main&#39; into publishing-with-pypi ([`6c12e36`](https://github.com/OpenAdaptAI/OpenAdapt/commit/6c12e36c340c13a5c9cf0e01de195056e206a4d6))

* Merge pull request #283 from MLDSAI/abrichr-patch-2 ([`aa31545`](https://github.com/OpenAdaptAI/OpenAdapt/commit/aa3154557c65529849a9333de19171a9e7312fa3))

* Update README.md ([`8cc3860`](https://github.com/OpenAdaptAI/OpenAdapt/commit/8cc3860a6d3f8516db64f543f07d9a2636904650))

* Merge pull request #282 from MLDSAI/abrichr-patch-2

Update README.md ([`cf0c339`](https://github.com/OpenAdaptAI/OpenAdapt/commit/cf0c3395303a1cb4cc4293f111c1fd69881957f2))

* Update README.md ([`66c3d70`](https://github.com/OpenAdaptAI/OpenAdapt/commit/66c3d7061eb1d0a66052294c7f3d5c56902ce737))

* Merge pull request #207 from 0dm/clear-data

added openadapt.renew ([`15a5cca`](https://github.com/OpenAdaptAI/OpenAdapt/commit/15a5ccae1e81aa098734cecefa6d57dcb48ea4ed))

* Merge pull request #268 from 0dm/poetry

Use Poetry for dependencies ([`bde3816`](https://github.com/OpenAdaptAI/OpenAdapt/commit/bde381684cc51c71d5e8169edaaca808352c0521))

* remove license and update readme image ([`c6f30f1`](https://github.com/OpenAdaptAI/OpenAdapt/commit/c6f30f11a574a0a9d4b3e0896ac83c92a147516f))

* Merge pull request #204 from jesicasusanto/feat/windowstate_for_windows

Windowstate for windows ([`f837d62`](https://github.com/OpenAdaptAI/OpenAdapt/commit/f837d623dd717628db7c74093907560451d6695f))

* reorder authors alphabetically, and add placeholder license ([`983781c`](https://github.com/OpenAdaptAI/OpenAdapt/commit/983781c8d578f599d9e2655ebaab16f02a5e90fd))

* adjusted imports, added to readme ([`5a75be9`](https://github.com/OpenAdaptAI/OpenAdapt/commit/5a75be93af444b59765eb786505ff0b6c0c5e9a9))

* add spacy files ([`e9d053a`](https://github.com/OpenAdaptAI/OpenAdapt/commit/e9d053a2d05bb9538c0130597e3c47e36e3c89ee))

* update for nicegui ([`b20b060`](https://github.com/OpenAdaptAI/OpenAdapt/commit/b20b060ec51cd22b738746a80bc101a726669a79))

* Merge remote-tracking branch &#39;upstream/main&#39; into poetry ([`17a12ad`](https://github.com/OpenAdaptAI/OpenAdapt/commit/17a12adbeae6d593e7adb235f83c4ee34e1e009c))

* add noqa + newline after docstring ([`482ab56`](https://github.com/OpenAdaptAI/OpenAdapt/commit/482ab56694aa4606814e696a6a0425d51f34207f))

* add docstring to main ([`a8fa74f`](https://github.com/OpenAdaptAI/OpenAdapt/commit/a8fa74f20d05cc1474f85ceeb33c7914dae52f97))

* serialize_rect -&gt; dictify_rect ([`580c294`](https://github.com/OpenAdaptAI/OpenAdapt/commit/580c294966a2b473caa9c994f02fa7b92f07a22a))

* split docstrings to be &lt;= 80 chars per line ([`8f0b179`](https://github.com/OpenAdaptAI/OpenAdapt/commit/8f0b179dbaf37266459459e094a69093e6ad0b7d))

* Merge branch &#39;publishing-with-pypi&#39; of https://github.com/Mustaballer/puterbot into publishing-with-pypi ([`567ea8f`](https://github.com/OpenAdaptAI/OpenAdapt/commit/567ea8f64360269926cf630e0153222b80443693))

* Update author list pyproject.toml ([`1a65973`](https://github.com/OpenAdaptAI/OpenAdapt/commit/1a659732e2f62969f98220cd706fd27dc3d33068))

* Merge pull request #266 from dianzrong/add-contributing-md

Update Contributing Templates ([`8653d0c`](https://github.com/OpenAdaptAI/OpenAdapt/commit/8653d0cdf3bcb263989a0817f4475c2e9e93edc9))

* Merge pull request #215 from Mustaballer/github-actions-lint-tests

Github actions for automatic testing and linting ([`0cd5c54`](https://github.com/OpenAdaptAI/OpenAdapt/commit/0cd5c54fd1c300706d172dc7046ccfe557bcfea6))

* Merge pull request #272 from KrishPatel13/feature/semantic_scrub

Feature/semantic scrub ([`a11b76b`](https://github.com/OpenAdaptAI/OpenAdapt/commit/a11b76baf849537bf0cb6c7c8fa27187402f0c3f))

* add comment for SCRUB_COLOR and
remove unused import ([`e2bcbca`](https://github.com/OpenAdaptAI/OpenAdapt/commit/e2bcbca2b7a4f29c528d35dccc8f78c0f84c7aac))

* removed test_window ([`09680c6`](https://github.com/OpenAdaptAI/OpenAdapt/commit/09680c6b602f03d6848ce8ef42d0c01a2f1ca64b))

* Merge branch &#39;main&#39; into clear-data ([`ab0c6b3`](https://github.com/OpenAdaptAI/OpenAdapt/commit/ab0c6b39c5f13c402652c1e1ade6cbf675f46fc2))

* fix visualizatoin-&gt;red color ([`93b32b8`](https://github.com/OpenAdaptAI/OpenAdapt/commit/93b32b8bcc4c55e9fffa1268d741578ca3ab1940))

* Merge pull request #169 from jesicasusanto/feat/sam_mixin

add sam_mixin.py ([`8fd1af7`](https://github.com/OpenAdaptAI/OpenAdapt/commit/8fd1af73a3a460275c2d3fd474c38d4f2dbb3790))

* Merge branch &#39;main&#39; into feat/sam_mixin ([`e3b4056`](https://github.com/OpenAdaptAI/OpenAdapt/commit/e3b4056510a650701d601d1b984e677c5932ef16))

* Merge pull request #67 from dianzrong/expand-on-demo-replay-strategy

SummaryReplayStrategyMixin ([`2c0e860`](https://github.com/OpenAdaptAI/OpenAdapt/commit/2c0e860d6029336788a604360383ceaf886953ec))

* Merge pull request #135 from 0dm/add-GUI

GUI + Installer ([`3e1ebb9`](https://github.com/OpenAdaptAI/OpenAdapt/commit/3e1ebb94eda47f251e2c0b9f10e4146e97c7458e))

* turn values into placeholders ([`40fd85e`](https://github.com/OpenAdaptAI/OpenAdapt/commit/40fd85ea2748f32d9fb477db816113a2ab06865f))

* fix visualization error:

color must be int or single element tuple ([`ab4427c`](https://github.com/OpenAdaptAI/OpenAdapt/commit/ab4427c40ddd083c07dbb2e9275e4860ee945799))

* add scrub_cahr for force_scrubbing key char ([`438b22f`](https://github.com/OpenAdaptAI/OpenAdapt/commit/438b22f8206f303f882c2f05d733ca40921a54be))

* Remove SCRUB_CHAR from config ([`b6ab737`](https://github.com/OpenAdaptAI/OpenAdapt/commit/b6ab7371c19ccc5bf97f2deaaa92328b458fce47))

* Merge branch &#39;main&#39; into github-actions-lint-tests ([`eba635e`](https://github.com/OpenAdaptAI/OpenAdapt/commit/eba635e3f15feee0b1049a7fd6a77d3f0c729520))

* Aesthetic changes to PR template ([`e5973d4`](https://github.com/OpenAdaptAI/OpenAdapt/commit/e5973d4e7de485c10c3569778afa2c0e69bef4e7))

* Removed operators from scrub and
changed the test_scrub ([`d03aae7`](https://github.com/OpenAdaptAI/OpenAdapt/commit/d03aae77a99041c29de56566fc2b7d0c3da5c904))

* Add .idea to gitignore ([`5a28170`](https://github.com/OpenAdaptAI/OpenAdapt/commit/5a28170d7e4f31916e376d8b8785f219d8d47445))

* Merge branch &#39;main&#39; into publishing-with-pypi ([`cbdf887`](https://github.com/OpenAdaptAI/OpenAdapt/commit/cbdf887f9fbf71dcd1499f4cb65efdbca1fe26ef))

* Merge branch &#39;MLDSAI:main&#39; into feat/windowstate_for_windows ([`43ead42`](https://github.com/OpenAdaptAI/OpenAdapt/commit/43ead422f11337381000778d1c8430ddada2ed12))

* Merge branch &#39;MLDSAI:main&#39; into feat/sam_mixin ([`a0f17c3`](https://github.com/OpenAdaptAI/OpenAdapt/commit/a0f17c31108d1b667f792a06af7467d15b77edd4))

* undo changes ([`f6e0caf`](https://github.com/OpenAdaptAI/OpenAdapt/commit/f6e0cafc13bc041eb94c18873f7d68b9bf485b85))

* fix conflict ([`347eb3d`](https://github.com/OpenAdaptAI/OpenAdapt/commit/347eb3dfdf4387904da3bcce291eabc2bdd6fb9a))

* fix merge conflict ([`ba4348a`](https://github.com/OpenAdaptAI/OpenAdapt/commit/ba4348a067e3db35c8beb0e8bbac06a35a9bdcd3))

* Update pyproject.toml ([`8871555`](https://github.com/OpenAdaptAI/OpenAdapt/commit/88715551d2afcae0a0c501ac86705e13ddbeb965))

* Revert &#34;merged changes&#34;

This reverts commit 2066147107c63379ac20cdc95ba3d3fa63fe2603. ([`12c9243`](https://github.com/OpenAdaptAI/OpenAdapt/commit/12c9243ff5fc7b40fd1bca34c03465c54074e0ed))

* Revert &#34;remove uneccesary files and&#34;

This reverts commit 806e717049ae4ad76843134c907fa604534b5b28. ([`fb170e1`](https://github.com/OpenAdaptAI/OpenAdapt/commit/fb170e1f06479a03912e285f70d7baec4b8b72f4))

* Revert &#34;Fix for Visualization&#34;

This reverts commit 6d717f535a15867727065e48d0b7e92c3904204e. ([`7fc3439`](https://github.com/OpenAdaptAI/OpenAdapt/commit/7fc3439491b93f3c0d579783b1c3992ed5b516bd))

* comments ([`330d999`](https://github.com/OpenAdaptAI/OpenAdapt/commit/330d9999c19956d6eb788e3ca97dbe5358f309f9))

* Update pyproject.toml ([`d814d8a`](https://github.com/OpenAdaptAI/OpenAdapt/commit/d814d8a93dc5b9147edef0830c871b315b2b6055))

* added scripts ([`1eb2085`](https://github.com/OpenAdaptAI/OpenAdapt/commit/1eb20858f40e5494ee41192987b8541439a98174))

* Update pyproject.toml ([`853fef2`](https://github.com/OpenAdaptAI/OpenAdapt/commit/853fef2a290bc24ad3f7afe372052abf3e4451ac))

* create pyproject ([`2e6b487`](https://github.com/OpenAdaptAI/OpenAdapt/commit/2e6b48772fc683dccacd4ae3c3e74653a622ef35))

* Request test output ([`ff8614a`](https://github.com/OpenAdaptAI/OpenAdapt/commit/ff8614a7120d0a205a5cb47d1725bb3e0eb2f370))

* Merge branch &#39;main&#39; into feat/tracing ([`70bf019`](https://github.com/OpenAdaptAI/OpenAdapt/commit/70bf019d4c7361954c46adf103dd7f7115d6f199))

* Merge pull request #248 from dianzrong/add-contributing-md

Make OS Option Optional ([`6a77a3a`](https://github.com/OpenAdaptAI/OpenAdapt/commit/6a77a3adf72378c60be4c3f985c3a6a6b39e755a))

* Merge pull request #263 from MLDSAI/abrichr-patch-2

Update README.md ([`1095399`](https://github.com/OpenAdaptAI/OpenAdapt/commit/1095399db4b5180b61d360ca3a4790ef7350b725))

* Update README.md ([`dd1725b`](https://github.com/OpenAdaptAI/OpenAdapt/commit/dd1725bed56f939856e47c8c317243c355c66cf5))

* Add value to feature request ([`1cd9acc`](https://github.com/OpenAdaptAI/OpenAdapt/commit/1cd9acc57885b75d1b361846ccffda73c75cb228))

* Simpler bug form ([`3cd2876`](https://github.com/OpenAdaptAI/OpenAdapt/commit/3cd28761033277c320249cd722ae310e0ac41dc9))

* Add &lt;br&gt; ([`047afce`](https://github.com/OpenAdaptAI/OpenAdapt/commit/047afce4ed927e599b8cc60311e054ef0ad21b9d))

* Update bug_form.yml ([`9fbd59e`](https://github.com/OpenAdaptAI/OpenAdapt/commit/9fbd59eb36b0c31be61d9d1cbfc83273c93e2f95))

* remove optional sections ([`27072ff`](https://github.com/OpenAdaptAI/OpenAdapt/commit/27072ffcb9745d10455114632e9b1e7d04a353c0))

* Attempt to add new lines ([`9630394`](https://github.com/OpenAdaptAI/OpenAdapt/commit/963039448be9fdd35340ebb92436e3cff058101b))

* Remove optional required sections ([`6d3ead0`](https://github.com/OpenAdaptAI/OpenAdapt/commit/6d3ead0193ca3fa14e90d043624bc19f90bbd084))

* Merge remote-tracking branch &#39;upstream/main&#39; into add-GUI ([`794925c`](https://github.com/OpenAdaptAI/OpenAdapt/commit/794925cb9e7da19cee5fd9060aed4b965400d4ba))

* remove uneccesary files and
add .idea in .gitignore ([`806e717`](https://github.com/OpenAdaptAI/OpenAdapt/commit/806e717049ae4ad76843134c907fa604534b5b28))

* Fix for Visualization ([`6d717f5`](https://github.com/OpenAdaptAI/OpenAdapt/commit/6d717f535a15867727065e48d0b7e92c3904204e))

* merged changes ([`2066147`](https://github.com/OpenAdaptAI/OpenAdapt/commit/2066147107c63379ac20cdc95ba3d3fa63fe2603))

* Merge pull request #256 from MLDSAI/abrichr-patch-2

Update README.md ([`8c1c84b`](https://github.com/OpenAdaptAI/OpenAdapt/commit/8c1c84ba006cd97ce445902ea0def696097cf7aa))

* Update README.md ([`1123353`](https://github.com/OpenAdaptAI/OpenAdapt/commit/1123353d509db1c161983e2bc9073fcadab49f0c))

* Merge pull request #254 from MLDSAI/abrichr-patch-2

Update README.md ([`fe255ce`](https://github.com/OpenAdaptAI/OpenAdapt/commit/fe255ceae7508dd648e6d0c5adc60ccc0775324c))

* Update README.md ([`af72ed6`](https://github.com/OpenAdaptAI/OpenAdapt/commit/af72ed6d5bc14de571e89a9725a60bdec77dbb2b))

* Merge pull request #253 from MLDSAI/abrichr-patch-2

Update README.md ([`a1b5daa`](https://github.com/OpenAdaptAI/OpenAdapt/commit/a1b5daa05fa743b9d890a9ca8a620be665f3acde))

* Update README.md ([`da1dd46`](https://github.com/OpenAdaptAI/OpenAdapt/commit/da1dd46810a668e311db7b60a6a3e3369b72b5f9))

* Update new feature template ([`717a1c1`](https://github.com/OpenAdaptAI/OpenAdapt/commit/717a1c145970202600909d92eab1d865fb336219))

* Make OS optional ([`fbe5008`](https://github.com/OpenAdaptAI/OpenAdapt/commit/fbe500830e24ef6c876152f3827566af1df20312))

* Merge pull request #247 from MLDSAI/abrichr-patch-2

Update README.md ([`912566a`](https://github.com/OpenAdaptAI/OpenAdapt/commit/912566a3f65fc7f18ef2e0f2bb9c9e310bd5965f))

* Update README.md ([`0818046`](https://github.com/OpenAdaptAI/OpenAdapt/commit/0818046adeb04216896ecb65a7c5b011451bd750))

* Merge pull request #245 from MLDSAI/abrichr-patch-1

Update README.md ([`f1e3ca2`](https://github.com/OpenAdaptAI/OpenAdapt/commit/f1e3ca236e21bbfe344049cf524a9c1d0c9bffc3))

* Update README.md ([`685b20c`](https://github.com/OpenAdaptAI/OpenAdapt/commit/685b20ce9488bf00976a30d4c15f9b5fbe633ace))

* Update README.md ([`19fbab4`](https://github.com/OpenAdaptAI/OpenAdapt/commit/19fbab464f3590473a828aa5876f66b68086f4ce))

* Update README.md ([`0306a38`](https://github.com/OpenAdaptAI/OpenAdapt/commit/0306a385ff907ac1c8c90df68238c87975314954))

* Update README.md ([`6524683`](https://github.com/OpenAdaptAI/OpenAdapt/commit/6524683073143856a423d3fa8996393ba671381a))

* Merge remote-tracking branch &#39;upstream/main&#39; into clear-data ([`b88fa55`](https://github.com/OpenAdaptAI/OpenAdapt/commit/b88fa554bcdaab524a2028624cb724ebf9a40de1))

* add todo for future improvement ([`4fb0ba5`](https://github.com/OpenAdaptAI/OpenAdapt/commit/4fb0ba5de771c262b959ec368dfe8495b18ad80e))

* Merge remote-tracking branch &#39;upstream/main&#39; into add-GUI ([`c54103c`](https://github.com/OpenAdaptAI/OpenAdapt/commit/c54103c9b4bdad630c6fbb3e1f075f6b855cda7b))

* Merge pull request #161 from dianzrong/add-contributing-md

Create CONTRIBUTING.md ([`f5cbdd2`](https://github.com/OpenAdaptAI/OpenAdapt/commit/f5cbdd2cf1e71a957dcf5ea5f2110609777b3a85))

* Merge pull request #241 from 0dm/scrubbing-changes

Scrubbing changes ([`180a774`](https://github.com/OpenAdaptAI/OpenAdapt/commit/180a7745c341b56de9bc642c75551111bfb5860a))

* Merge pull request #243 from KrishPatel13/update/install

Add TODO in Installation Scripts ([`7ce0479`](https://github.com/OpenAdaptAI/OpenAdapt/commit/7ce0479e7c0772efe7ef753ea74379aaf03a64ee))

* add link in TODO ([`6369256`](https://github.com/OpenAdaptAI/OpenAdapt/commit/6369256993e4965b4b63b79c38a07cf55b1f335c))

* add TODO:
Tesseract OCR installation ([`5f6610e`](https://github.com/OpenAdaptAI/OpenAdapt/commit/5f6610ec9c93c4429c516dd7e48da1e87c4d8941))

* Correct typo ([`386c14f`](https://github.com/OpenAdaptAI/OpenAdapt/commit/386c14f738fe61597ff9cd46608cdadaca190272))

* resolve merge conflicts ([`df22ff8`](https://github.com/OpenAdaptAI/OpenAdapt/commit/df22ff833ba0a1bddace5fbe4337888ddb3651a6))

* Merge pull request #142 from AvidEslami/crop_active_window

Crop Screenshot to fit the Bounds of a WindowEvent (#108 ([`e3fd5c7`](https://github.com/OpenAdaptAI/OpenAdapt/commit/e3fd5c77aad994663d7f691f0a55844b18c545dc))

* Merge branch &#39;main&#39; into clear-data ([`b5fdc81`](https://github.com/OpenAdaptAI/OpenAdapt/commit/b5fdc8194eab905a219861fae6904398e3f4344b))

* add black ([`5501191`](https://github.com/OpenAdaptAI/OpenAdapt/commit/5501191fc6818898f2eea4e166a72ec33fe83b07))

* requested changes ([`76b1396`](https://github.com/OpenAdaptAI/OpenAdapt/commit/76b139685b198ae7c539ceb3672c6f16adc26c23))

* fix window data to have hierarchy ([`4a58ad4`](https://github.com/OpenAdaptAI/OpenAdapt/commit/4a58ad4bf7b7a355d5e9784424665f8efda02985))

* changes ([`b20a4d9`](https://github.com/OpenAdaptAI/OpenAdapt/commit/b20a4d93c70da86d56acfea0601f35f9dd89d876))

* false by default ([`9975a09`](https://github.com/OpenAdaptAI/OpenAdapt/commit/9975a0955fb520822b235dd5d548e33afa7601d7))

* Merge remote-tracking branch &#39;upstream/main&#39; into silence-warnings ([`3cd8daa`](https://github.com/OpenAdaptAI/OpenAdapt/commit/3cd8daabb18e877975850c4d0696b0d58e165a8c))

* Merge remote-tracking branch &#39;upstream/main&#39; into add-GUI ([`df78d57`](https://github.com/OpenAdaptAI/OpenAdapt/commit/df78d5781336167c2eb272776180fae9d92b228c))

* Add github action bot name and email ([`1ff6435`](https://github.com/OpenAdaptAI/OpenAdapt/commit/1ff6435c47f3e0545041b75255053d96ea50cbbe))

* Merge pull request #239 from KrishPatel13/main

Fix Pytest ([`b92309c`](https://github.com/OpenAdaptAI/OpenAdapt/commit/b92309cedd928943d3cbb9aa4e76d50a14eeb6d7))

* Fix Pytest ([`d399970`](https://github.com/OpenAdaptAI/OpenAdapt/commit/d399970c58d64708b4d8c21c70ef1b936f03b0f5))

* rename localfilepicker ([`bbb3fca`](https://github.com/OpenAdaptAI/OpenAdapt/commit/bbb3fca54e30737a1dbffc0ac39e87901705bdad))

* Merge remote-tracking branch &#39;upstream/main&#39; into add-GUI ([`0666809`](https://github.com/OpenAdaptAI/OpenAdapt/commit/0666809e678f61bb2458cd0ff95d18219ae19bc6))

* doc: fix incorrect version bump example ([`281b77c`](https://github.com/OpenAdaptAI/OpenAdapt/commit/281b77c6dd0f1227b3977fdabd6e98d51b20a44b))

* enqueue log messages to make logging multiprocess-safe ([`eb3deb0`](https://github.com/OpenAdaptAI/OpenAdapt/commit/eb3deb0035c260ad0ed714e316a3870b6882d166))

* Merge pull request #211 from KrishPatel13/feature/scrub-final

feat(scrub): Scrubbing and Fix Atomacos ([`d784d37`](https://github.com/OpenAdaptAI/OpenAdapt/commit/d784d374826fec74a0dc7a2ac636ac56510958e5))

* Merge pull request #225 from 0dm/pickling_investigation

Use pickle.HIGHEST_PROTOCOL ([`45b77ce`](https://github.com/OpenAdaptAI/OpenAdapt/commit/45b77ce1ac05d085e1eae8e761f2ee5918713e02))

* ran black on scrub.py ([`5ceea9a`](https://github.com/OpenAdaptAI/OpenAdapt/commit/5ceea9a73d59bb782f41c2308677557652338afe))

* Merge branch &#39;MLDSAI:main&#39; into feat/windowstate_for_windows ([`1638d96`](https://github.com/OpenAdaptAI/OpenAdapt/commit/1638d96ed1d7940fe4f54b42cfc69f02dd48719e))

* add serialize_rect function ([`b5d9d7e`](https://github.com/OpenAdaptAI/OpenAdapt/commit/b5d9d7e4c04cf9120d1feb34694db749c0a084b4))

* addressed reviewed comments ([`075cfff`](https://github.com/OpenAdaptAI/OpenAdapt/commit/075cfff684da1a2c43ee7bdb535f6d8b8d03f38e))

* Add publish action to github, reformat CONTRIBUTING.md, set up git release config pyproject.toml ([`e63d455`](https://github.com/OpenAdaptAI/OpenAdapt/commit/e63d455879be169b48cb542931da4a4bf9e8204f))

* serialize RECT object ([`d0a0800`](https://github.com/OpenAdaptAI/OpenAdapt/commit/d0a0800a7ba76bcd71cf3bec43e5d298aa68ab1c))

* Merge branch &#39;MLDSAI:main&#39; into feature/scrub-final ([`23b5228`](https://github.com/OpenAdaptAI/OpenAdapt/commit/23b5228d6d38d342f8efb1cf3919ea606ef4691c))

* fix get_descendants_info performance ([`371b7c2`](https://github.com/OpenAdaptAI/OpenAdapt/commit/371b7c209dde35c155fde4d1f8e3c19a3a1131ce))

* allow for python3 -m openadapt.app ([`6789dcd`](https://github.com/OpenAdaptAI/OpenAdapt/commit/6789dcdef898b8490f9783ee9c2b5f2c040e4682))

* Merge branch &#39;silence-warnings&#39; into add-GUI ([`45a651f`](https://github.com/OpenAdaptAI/OpenAdapt/commit/45a651f308bc908a2cf89eac7f6b40e42cce0332))

* Merge remote-tracking branch &#39;upstream/main&#39; into add-GUI ([`cc0107f`](https://github.com/OpenAdaptAI/OpenAdapt/commit/cc0107f04e1382e3b79701a9eff8e30735362caf))

* Merge remote-tracking branch &#39;upstream/main&#39; into clear-data ([`b922155`](https://github.com/OpenAdaptAI/OpenAdapt/commit/b9221554db6281e8c9c0e29059e3b390b5be6e3a))

* Merge remote-tracking branch &#39;upstream/main&#39; into silence-warnings ([`cf3b7ce`](https://github.com/OpenAdaptAI/OpenAdapt/commit/cf3b7cee589e3de2bcc89fad81304f827bfbb4fd))

* Merge remote-tracking branch &#39;upstream/main&#39; into pickling_investigation ([`9ea3953`](https://github.com/OpenAdaptAI/OpenAdapt/commit/9ea39536a87ae70b838a61c20e0601237354e43c))

* Merge branch &#39;MLDSAI:main&#39; into feat/tracing ([`3da5b64`](https://github.com/OpenAdaptAI/OpenAdapt/commit/3da5b64411d4f1d3c1fad00baec33cabc4ec660e))

* removed indentation since not necessary with logging ([`65fb226`](https://github.com/OpenAdaptAI/OpenAdapt/commit/65fb226e9a0ef7136bfb838a5ea5c5a85cfae5aa))

* added tracing for most functions (except those that would be redundant) ([`e325c2e`](https://github.com/OpenAdaptAI/OpenAdapt/commit/e325c2e0716044402db97a0c9fdd3473d73f9c62))

* Merge branch &#39;publishing-with-pypi&#39; of https://github.com/Mustaballer/puterbot into publishing-with-pypi ([`84c2e14`](https://github.com/OpenAdaptAI/OpenAdapt/commit/84c2e14d63bbe315d2ff91219638a00af9a5f740))

* Merge branch &#39;main&#39; into publishing-with-pypi ([`89386f3`](https://github.com/OpenAdaptAI/OpenAdapt/commit/89386f3d03c570aec9ea63975fa46dcddc15493e))

* use repository_username and repository_password instead of pypi_token in release.yml ([`e19dcfd`](https://github.com/OpenAdaptAI/OpenAdapt/commit/e19dcfd77cf34cbe940b571f5e273b2437a41cf0))

* Merge pull request #217 from KrishPatel13/install

Add openadapt/install ([`7db7cc9`](https://github.com/OpenAdaptAI/OpenAdapt/commit/7db7cc9215c30b0594e08245cc97fa74a0760d48))

* add pickle dumps state ([`28b409f`](https://github.com/OpenAdaptAI/OpenAdapt/commit/28b409fac4d36ac608d9babc0a090ffef1310db1))

* use highest_protocol ([`76fb5ec`](https://github.com/OpenAdaptAI/OpenAdapt/commit/76fb5ec18652f0fd34952e7a1336c7a4189d0326))

* fix typo ([`c36f13e`](https://github.com/OpenAdaptAI/OpenAdapt/commit/c36f13e2087a0bf5c33bccdf440179a38c90826f))

* Add CONTRIBUTING.md ([`871d1e7`](https://github.com/OpenAdaptAI/OpenAdapt/commit/871d1e7d20513fb4afac19fea1e2b12d5102da62))

* merge ([`9b64cc7`](https://github.com/OpenAdaptAI/OpenAdapt/commit/9b64cc7affe1e6cdfdbc6abb0adb400f020e7faf))

* better checks if changes are needed ([`8e66364`](https://github.com/OpenAdaptAI/OpenAdapt/commit/8e663645c1942ef49bd2dec11897cff0b7e232ee))

* Check if stashing is needed ([`d564bf4`](https://github.com/OpenAdaptAI/OpenAdapt/commit/d564bf4e7f6780c72ae664ca2599aeceb9bf53b4))

* check if stashing is needed ([`cf9afa6`](https://github.com/OpenAdaptAI/OpenAdapt/commit/cf9afa6e5c457586cdd2950a47d1adc00185c58a))

* added pull request template ([`bdb3ba8`](https://github.com/OpenAdaptAI/OpenAdapt/commit/bdb3ba81fdfa3f5ce6c650838b72543cef90bae8))

* add issue template to contributing ([`92889d9`](https://github.com/OpenAdaptAI/OpenAdapt/commit/92889d96da5aa8d9fdb8e6c0ad136ab3e82ac5c3))

* new feature template and add config to templates ([`6821222`](https://github.com/OpenAdaptAI/OpenAdapt/commit/682122278fd7464402ded7f0a9adfd68ebb49a4a))

* remove print ([`192217a`](https://github.com/OpenAdaptAI/OpenAdapt/commit/192217a0a72da63dabd58b25628610dafe579092))

* add docstrings ([`feaf63a`](https://github.com/OpenAdaptAI/OpenAdapt/commit/feaf63a7bc7effcf652dc0adfcf2d18047dfa1a2))

* change docstring ([`5d92e22`](https://github.com/OpenAdaptAI/OpenAdapt/commit/5d92e22bf0ee8fa9daa0621252de1cae8c9ffd32))

* remove ws, reduce market point size ([`d582549`](https://github.com/OpenAdaptAI/OpenAdapt/commit/d582549c8ad4c43964587befbd641b8d8d16949a))

* update gitignore ([`38cf23c`](https://github.com/OpenAdaptAI/OpenAdapt/commit/38cf23c975cd4d368c4e6684360bf3dd8e35d5bd))

* sync branch ([`7778049`](https://github.com/OpenAdaptAI/OpenAdapt/commit/777804900fb21cfb8a20bbd82881ab8648ae9899))

* update .gitignore and requirements.txt ([`f7e2517`](https://github.com/OpenAdaptAI/OpenAdapt/commit/f7e251753c3d64518283f54e57be0b4c95b1dd79))

* fix inaccurate mask by adding more input points ([`90be2d1`](https://github.com/OpenAdaptAI/OpenAdapt/commit/90be2d1763789dcae8220504367a141c224cfe26))

* black sam.py ([`abcd31d`](https://github.com/OpenAdaptAI/OpenAdapt/commit/abcd31d033e65961bb0b6447dde3ca9f663450af))

* remove unnecessary lines of code ([`ad39a80`](https://github.com/OpenAdaptAI/OpenAdapt/commit/ad39a80b45e59449a1c2e33efb98a5262f75b4f7))

* added test for to check for duplicate descendants ([`fe0c267`](https://github.com/OpenAdaptAI/OpenAdapt/commit/fe0c26764b5b386b895a0b8743fba825d66c7d7d))

* def get_descendants_info ([`40ed70b`](https://github.com/OpenAdaptAI/OpenAdapt/commit/40ed70bf309056bdc02cd4c235ceb9c567e67dcf))

* use regex for excluding src and alembic directories ([`74e58c2`](https://github.com/OpenAdaptAI/OpenAdapt/commit/74e58c271ed7a1942f2c125f8d2c9827758e87e0))

* exclude src from black check ([`9c2daa0`](https://github.com/OpenAdaptAI/OpenAdapt/commit/9c2daa0ce90119d143745e3c047781cc2511424a))

* Try Krish approach for failing dependency in requirements.txt ([`2460ca3`](https://github.com/OpenAdaptAI/OpenAdapt/commit/2460ca3d58e84dd16fc590c544df6ae5b215055f))

* add version to black, and moved around in requirements.txt ([`ce320b6`](https://github.com/OpenAdaptAI/OpenAdapt/commit/ce320b691a707322490012b1511d177b5227fc3c))

* use config file ([`8c70f6f`](https://github.com/OpenAdaptAI/OpenAdapt/commit/8c70f6f5471f8c63edd84b5633735536042e1469))

* use config.DB_FPATH ([`b7ed43e`](https://github.com/OpenAdaptAI/OpenAdapt/commit/b7ed43e5c5e09e1ab43bfa8afe0f11eaa2c2ee6b))

* Update CONTRIBUTING.md with issues ([`c9fab2f`](https://github.com/OpenAdaptAI/OpenAdapt/commit/c9fab2f9b06fc6427a17f6c43710078ef76762a5))

* renamed the template folder ([`d595c35`](https://github.com/OpenAdaptAI/OpenAdapt/commit/d595c35865c960575fc31bd3c97269db346373f1))

* Rename bug_form.yaml to bug_form.yml ([`598efe0`](https://github.com/OpenAdaptAI/OpenAdapt/commit/598efe0ed7b12634eb4685c271abf37e80587ee6))

* Initial bug form ([`22dd66d`](https://github.com/OpenAdaptAI/OpenAdapt/commit/22dd66d99f21c813a1350f01a64bb0501af69010))

* improve formatting ([`bbbfde9`](https://github.com/OpenAdaptAI/OpenAdapt/commit/bbbfde9892a23109b8e202f99f14cf8ccd3b6352))

* refactor openadapt to OpenAdapt ([`853331b`](https://github.com/OpenAdaptAI/OpenAdapt/commit/853331bdd6b88675df7de77889bda48213cf9309))

* puterbot -&gt; openadapt ([`aa6e5a3`](https://github.com/OpenAdaptAI/OpenAdapt/commit/aa6e5a389be08aed607d081703c20e122d06b332))

* remove optional scrubbing for images for now ([`ad01102`](https://github.com/OpenAdaptAI/OpenAdapt/commit/ad011025daa1aaf4c83aafe026c9ab20069eab1d))

* organized the requirements.txt ([`392a465`](https://github.com/OpenAdaptAI/OpenAdapt/commit/392a465dde6a4a55c65710b9113d40fc6863f73b))

* created mock unit test ([`e505888`](https://github.com/OpenAdaptAI/OpenAdapt/commit/e50588851a7d61967d8ed4868eb1f9e978919db4))

* fixed recording stop button ([`a680c4d`](https://github.com/OpenAdaptAI/OpenAdapt/commit/a680c4dd1fe85c64d47a03766880976ebbcea6a3))

* save &amp; sync switch states ([`e3732a3`](https://github.com/OpenAdaptAI/OpenAdapt/commit/e3732a3bfaa109fb3d0c21fe3cd44ac26efca5ec))

* suggestions implemented ([`a61c812`](https://github.com/OpenAdaptAI/OpenAdapt/commit/a61c812899d3a65b44a092f1ba5357b2559093e0))

* remove unused file ([`56d3cff`](https://github.com/OpenAdaptAI/OpenAdapt/commit/56d3cff2cd7b57e786edbfc572e06dad97bbaf68))

* fic utils.py L477 Todo ([`f495c4c`](https://github.com/OpenAdaptAI/OpenAdapt/commit/f495c4c68b33dc8f731517e893579cb3605b1d72))

* add openadapt/install
add install_openadapt.ps1 (from OpenAdaptAi/install repo)
add install_openadapt.sh (from OpenAdaptAi/install repo)
update spacy model from _lg to _trf ([`760be83`](https://github.com/OpenAdaptAI/OpenAdapt/commit/760be832fc4e250363ebf34de4065d5162e17732))

* fix main ([`c4edfef`](https://github.com/OpenAdaptAI/OpenAdapt/commit/c4edfef8bbeafa1a67519121964eb9f8107cd0a1))

* change state to include all props ([`86766ef`](https://github.com/OpenAdaptAI/OpenAdapt/commit/86766eface00a7896234ac69dccbce2cc39e5920))

* Merge branch &#39;clear-data&#39; into add-GUI ([`f165494`](https://github.com/OpenAdaptAI/OpenAdapt/commit/f1654940f424844300c5fab2a372fa69052ad60f))

* remove commented line ([`93e9ba1`](https://github.com/OpenAdaptAI/OpenAdapt/commit/93e9ba12cff6ba773135f3ea998040fa0b4abf73))

* added changes from review, fixed pyinstaller ([`77818ec`](https://github.com/OpenAdaptAI/OpenAdapt/commit/77818ec30440f71422b935adda511ee445dacde9))

* Exclude migration files in black check and lint checks ([`d4c4b7a`](https://github.com/OpenAdaptAI/OpenAdapt/commit/d4c4b7aa1403d9ae326f581a0bbdc67c63aeafae))

* code reformating ([`389d373`](https://github.com/OpenAdaptAI/OpenAdapt/commit/389d373403b7d42f0da4e8bad984265d3eb8ff51))

* add scrubbing features ([`dab2967`](https://github.com/OpenAdaptAI/OpenAdapt/commit/dab2967bfde00b025e0148eeba3beaf8df700fa0))

* Copied main.yml from closed PR, and replaced reST doctsring style with Google Python Style ([`a9a062a`](https://github.com/OpenAdaptAI/OpenAdapt/commit/a9a062addf31820810e3541e9530328c2a5093a2))

* scrubbing in diff images ([`37968e5`](https://github.com/OpenAdaptAI/OpenAdapt/commit/37968e5b79fe217d9fc674b228a05e3eb0e7427f))

* refactor

added some comments and moved to scripts/reset_db.py ([`b728c98`](https://github.com/OpenAdaptAI/OpenAdapt/commit/b728c98a7c97496dcc5c1dc636f6ff47170303c1))

* remove blank lines ([`318c344`](https://github.com/OpenAdaptAI/OpenAdapt/commit/318c34441e140525c0884da31acc7f25f7bb9ba9))

* resolves a TODO in utils
Moved DIRNAME_PERFORMANCE_PLOTS = &#34;performance&#34; in config ([`f72de4c`](https://github.com/OpenAdaptAI/OpenAdapt/commit/f72de4c604d2bb556cfafd2811636b4f5c46fa77))

* Added function docstrings

fix typo is_seperated ([`d5b303d`](https://github.com/OpenAdaptAI/OpenAdapt/commit/d5b303dce17f49ed1caabab8ce27d0007b328272))

* moved the text_sep, prefix and suffix
from models.py to config
also move language to config
also moves the masking char to scrub ([`c69caf3`](https://github.com/OpenAdaptAI/OpenAdapt/commit/c69caf31901dce72ea43c719e9fa7ced158a3061))

* Added Testing Section ([`8ec83b0`](https://github.com/OpenAdaptAI/OpenAdapt/commit/8ec83b0d44f2640127995d5e474181417dd7cda4))

* purely summarizes now ([`f197253`](https://github.com/OpenAdaptAI/OpenAdapt/commit/f19725346066757d38bd1a6e15b945ca6b4f7679))

* Update Readme
convert back all hyphenated and scrubbed text back to hyphenated form ([`99bbb9a`](https://github.com/OpenAdaptAI/OpenAdapt/commit/99bbb9ab6974176a38ad4527d108909c2809a26f))

* ran black, flake-8 and pylint ([`89430e2`](https://github.com/OpenAdaptAI/OpenAdapt/commit/89430e23be4c04a27be71a1976e300932a92e971))

* 255 -&gt; (255,)
single element tuple ([`80edc68`](https://github.com/OpenAdaptAI/OpenAdapt/commit/80edc68f73f2f0d14ad5c07f7dc3f3118bc497d4))

* implement get_active_element_state(x, y) ([`113933e`](https://github.com/OpenAdaptAI/OpenAdapt/commit/113933e9a5dad4588681cacb03195c1b1dd6a911))

* Merge branch &#39;feat/windowstate_for_windows&#39; of https://github.com/jesicasusanto/PAT into feat/windowstate_for_windows ([`37da667`](https://github.com/OpenAdaptAI/OpenAdapt/commit/37da6671afd6e061990741eed4ff6bfaefd6720c))

* implement get_window_state() ([`97a9eba`](https://github.com/OpenAdaptAI/OpenAdapt/commit/97a9eba63d4c934287906b70c6c18cf0dbb1f9c1))

* add .db-journal in .gitignore ([`03c1fc0`](https://github.com/OpenAdaptAI/OpenAdapt/commit/03c1fc0c9a907c10ed77802c66268fdfbaac1c3e))

* fixed
255 -&gt; (255,0,0) in config due to failing test ([`faab021`](https://github.com/OpenAdaptAI/OpenAdapt/commit/faab021df63d88a0393c9863a11b1906d4515b17))

* Scrubbing Before Visualization Final Version ([`1296995`](https://github.com/OpenAdaptAI/OpenAdapt/commit/1296995dbfb8cedd619543e25ca4aa052a5d2376))

* make action_event a required parameter ([`df80ac0`](https://github.com/OpenAdaptAI/OpenAdapt/commit/df80ac02c078f891aa1b15fcc52a2b3d1ea50f23))

* Refactored start.py to increase Python usage ([`c5ff8b7`](https://github.com/OpenAdaptAI/OpenAdapt/commit/c5ff8b7ce526e110454ff886aa638811e66a62a3))

* encapsulated into method &#34;run_app&#34; ([`25d82b5`](https://github.com/OpenAdaptAI/OpenAdapt/commit/25d82b5cf5696924d58ad6038df433332cd448a5))

* Added semantic release tool to pyproject.toml and updated github action ([`97863ee`](https://github.com/OpenAdaptAI/OpenAdapt/commit/97863ee540ded5381f80c55a2987a1af586063f9))

* switch approach to incorporate scale_ratios ([`376cc66`](https://github.com/OpenAdaptAI/OpenAdapt/commit/376cc66632f089bbf9f06abe1dd3b9c9b557d3ff))

* last line may need to be changed ([`fe77bb9`](https://github.com/OpenAdaptAI/OpenAdapt/commit/fe77bb9f089cc7cc6f1ad78500dd8ad4e2622701))

* Changed the update file to be a python file ([`b29905a`](https://github.com/OpenAdaptAI/OpenAdapt/commit/b29905ab6d27c0175c2b63248f5f253b21d608a1))

* added openadapt.renew ([`f6fba8a`](https://github.com/OpenAdaptAI/OpenAdapt/commit/f6fba8a40788f1757b31fc78d1a5a7dbcb8d485e))

* trying to fix the scrubbing configurations ([`b4a3b60`](https://github.com/OpenAdaptAI/OpenAdapt/commit/b4a3b60baa821df17568583fe3e0656e6f0bf6d2))

* Add scrub module
Add test_scrub module
Add Scrubbing config in config.py
Add presidio and required packages in requirements.txt ([`ce67fd1`](https://github.com/OpenAdaptAI/OpenAdapt/commit/ce67fd194385319b6cc1e43a0f9d08d382380947))

* allow window_event to be passed, if not passed retrieve from action_event relationship ([`2cafcf3`](https://github.com/OpenAdaptAI/OpenAdapt/commit/2cafcf31e33ec849b32d3da40e9abf78b9db60eb))

* Merge branch &#39;MLDSAI:main&#39; into crop_active_window ([`69642a4`](https://github.com/OpenAdaptAI/OpenAdapt/commit/69642a4dc98b415635c9ce851047c1899b61cd22))

* Merge branch &#39;MLDSAI:main&#39; into feat/windowstate_for_windows ([`e26c201`](https://github.com/OpenAdaptAI/OpenAdapt/commit/e26c2015635a13e02d00e2d17fec7ea3935952c7))

* implement get_active_window_meta() ([`3ec8b1f`](https://github.com/OpenAdaptAI/OpenAdapt/commit/3ec8b1f508fb4a7ccae5f5b888109b474847af6d))

* Create update.sh

Initial commit ([`b909b8d`](https://github.com/OpenAdaptAI/OpenAdapt/commit/b909b8d7019f673ea97a3269dd047a5a6be8cc8e))

* remove newline ([`e044d13`](https://github.com/OpenAdaptAI/OpenAdapt/commit/e044d134925a95b22dc7b23b35ec6f579c7fd3b6))

* more configurable ([`400370c`](https://github.com/OpenAdaptAI/OpenAdapt/commit/400370c44ddf6835724a6f9483f502172b14daf8))

* Merge branch &#39;silence-warnings&#39; into add-GUI ([`33f8cc9`](https://github.com/OpenAdaptAI/OpenAdapt/commit/33f8cc91bf8ffbe011e746afc7c6bdc66ce2e603))

* added filter for loguru ([`b534bc4`](https://github.com/OpenAdaptAI/OpenAdapt/commit/b534bc4f9fc38724b3058ffcb40727e4efdba90f))

* add SHOW_PLOTS ([`8ea9773`](https://github.com/OpenAdaptAI/OpenAdapt/commit/8ea9773416dadbe32a10293b3d63e4d8477039cd))

* add RESIZE_RATIO ([`f221598`](https://github.com/OpenAdaptAI/OpenAdapt/commit/f22159801e8651ed3b7575135a8b2a0ea7ed3d5d))

* undo whitespace ([`ce3bcbf`](https://github.com/OpenAdaptAI/OpenAdapt/commit/ce3bcbf958b8179220d4a8ceb0e258763c157341))

* removed code for testing ([`e63ae6b`](https://github.com/OpenAdaptAI/OpenAdapt/commit/e63ae6b736dd258957dc7a29889ded2d09364c7d))

* removed code for testing ([`56ae587`](https://github.com/OpenAdaptAI/OpenAdapt/commit/56ae587dc32074c9e645ec0d9c288ccd216f9890))

* fixed sam.py ([`a0989b6`](https://github.com/OpenAdaptAI/OpenAdapt/commit/a0989b6d26771516c08d858cd138b485b04e49a6))

* fixed indent and added visualization ([`ba40792`](https://github.com/OpenAdaptAI/OpenAdapt/commit/ba40792b65434a1d6874b02a7316ec191a08b166))

* Remove unnecessary changes ([`6b8a564`](https://github.com/OpenAdaptAI/OpenAdapt/commit/6b8a56420f0d2729c802f08d5b566bc0f9dc50ad))

* Merge pull request #191 from MLDSAI/abrichr-patch-1

Update config.py ([`2bbe036`](https://github.com/OpenAdaptAI/OpenAdapt/commit/2bbe036845cff2bf5668a3bdd00aec7fc90dbc18))

* Update config.py ([`d615950`](https://github.com/OpenAdaptAI/OpenAdapt/commit/d6159500aa3e1d7669f9fd4f8323dfa2d53cd2fe))

* Merge pull request #122 from MLDSAI/feat/save-event-performance

Save performance data to db ([`5ce11ea`](https://github.com/OpenAdaptAI/OpenAdapt/commit/5ce11ea96ee72f698710c1f2b4652f84dd2bdf01))

* migration ([`04cb8db`](https://github.com/OpenAdaptAI/OpenAdapt/commit/04cb8dbe70fe0b091a882829819b143c1e20c8b2))

* fix ([`d2eaed3`](https://github.com/OpenAdaptAI/OpenAdapt/commit/d2eaed3a43e3eaca2b78ee60cbde380872f3dfcf))

* fix ([`60de470`](https://github.com/OpenAdaptAI/OpenAdapt/commit/60de4701021950547041640e5c4a41ab60cfe0d1))

* fixes ([`e0a1b23`](https://github.com/OpenAdaptAI/OpenAdapt/commit/e0a1b2365ecd30653e281898804f79b21ede3053))

* Merge branch &#39;main&#39; into feat/save-event-performance ([`b576793`](https://github.com/OpenAdaptAI/OpenAdapt/commit/b576793e267c317934fc7c3f531be6b166b33ddb))

* Merge branch &#39;main&#39; into feat/save-event-performance ([`f31d094`](https://github.com/OpenAdaptAI/OpenAdapt/commit/f31d0940290a823128f8cc8964b3b9abf82e9fa6))

* fixes ([`4aca87d`](https://github.com/OpenAdaptAI/OpenAdapt/commit/4aca87d4f34cc59309dcd0c6e3a6f41faa3f14e8))

* Merge remote-tracking branch &#39;upstream/main&#39; into add-GUI ([`bfd4af7`](https://github.com/OpenAdaptAI/OpenAdapt/commit/bfd4af73f7278f24ca155ce4ccdfa6dd20ceded2))

* add PerformanceStat alembic migration script ([`7130b49`](https://github.com/OpenAdaptAI/OpenAdapt/commit/7130b49207b7305d64e451212bc822a944b9572a))

* Merge pull request #182 from MLDSAI/abrichr-patch-1

Update README.md ([`08dac61`](https://github.com/OpenAdaptAI/OpenAdapt/commit/08dac61f2c468aa09725caacb1e4db0004b25725))

* Update README.md ([`a0e8b71`](https://github.com/OpenAdaptAI/OpenAdapt/commit/a0e8b718f8ff9a58ca7069fbf74f7dc9b2a322ad))

* Merge pull request #120 from atineoSE/macOS_improvements

MacOS improvements ([`20976c5`](https://github.com/OpenAdaptAI/OpenAdapt/commit/20976c55861981555c5e0034743df98befa79dee))

* documentation ([`e1d4496`](https://github.com/OpenAdaptAI/OpenAdapt/commit/e1d44961cd6b677c38e3e7b7e7a36f51e99050c1))

* Merge remote-tracking branch &#39;upstream/main&#39; into add-GUI ([`c5cb169`](https://github.com/OpenAdaptAI/OpenAdapt/commit/c5cb1692718ffea5574f0dfc46362a0997572d6d))

* Merge branch &#39;main&#39; into macOS_improvements ([`0d40517`](https://github.com/OpenAdaptAI/OpenAdapt/commit/0d4051734a16fad3467a678078ac31b29b9cb278))

* Merge pull request #179 from MLDSAI/abrichr-patch-1

Update README.md ([`75041fc`](https://github.com/OpenAdaptAI/OpenAdapt/commit/75041fc817f32b70f3bc03cf5a56329a760dbbf6))

* Update README.md ([`c27fc3b`](https://github.com/OpenAdaptAI/OpenAdapt/commit/c27fc3bf3b1b3e18dd63b95d72c42cbf87139835))

* Updated README with hosted image instead of relative image for visualize.py ([`f498c7d`](https://github.com/OpenAdaptAI/OpenAdapt/commit/f498c7dc21a71123cf7c797725771c1a73edab80))

* Added github actions for automatic publishing when releasing a new version ([`059651e`](https://github.com/OpenAdaptAI/OpenAdapt/commit/059651e976f4aaf9d9b9b5c7859be7ca207f927e))

* Create pyproject.toml config using Test PyPi ([`686505d`](https://github.com/OpenAdaptAI/OpenAdapt/commit/686505dfadd344028782fd7151d2070ed9204512))

* Merge pull request #176 from MLDSAI/fix/window

wrap with try/except ([`2777019`](https://github.com/OpenAdaptAI/OpenAdapt/commit/277701932da3b53576e1566a81d6e94e653a92ff))

* import pickle ([`be6e7e0`](https://github.com/OpenAdaptAI/OpenAdapt/commit/be6e7e0bc74f68f89c6fce94718d9b6272dc4bd6))

* revert; try pickling ([`3fcbb17`](https://github.com/OpenAdaptAI/OpenAdapt/commit/3fcbb1745fdc159c0d6dc2c1efe9005b523aebcc))

* try except in trigger_action_event ([`cba2573`](https://github.com/OpenAdaptAI/OpenAdapt/commit/cba257397064726831606744aed43bdacc885754))

* wrap with try/except ([`a457f50`](https://github.com/OpenAdaptAI/OpenAdapt/commit/a457f50556509125b90bbf8e3d199daefebdb658))

* Merge remote-tracking branch &#39;upstream/main&#39; into add-GUI ([`8fc3d40`](https://github.com/OpenAdaptAI/OpenAdapt/commit/8fc3d407ad82c029711840185555575b058d5413))

* remove unnecessary imports ([`a87f667`](https://github.com/OpenAdaptAI/OpenAdapt/commit/a87f66703a7f732e4b5dae69950ec240f8502cc7))

* remove unnecessary imports ([`95c9aa9`](https://github.com/OpenAdaptAI/OpenAdapt/commit/95c9aa9422f0d1e848320f09a0f93dd6264c74fb))

* add bbox functions ([`66a28a8`](https://github.com/OpenAdaptAI/OpenAdapt/commit/66a28a8136ea8d9cb1f4dfe4673fba8103f79418))

* Merge branch &#39;MLDSAI:main&#39; into feat/sam_mixin ([`6b6b287`](https://github.com/OpenAdaptAI/OpenAdapt/commit/6b6b2871b50afdccb6e9dc35415cd16f7bc7cdb6))

* Merge pull request #151 from MLDSAI/feat/window_state

Window state ([`4ea4ba8`](https://github.com/OpenAdaptAI/OpenAdapt/commit/4ea4ba83241793a85df8c424bb248047ebca749b))

* merge main ([`ec13a78`](https://github.com/OpenAdaptAI/OpenAdapt/commit/ec13a782c872caed47e477afe70c1d28b5279262))

* fix ([`2692811`](https://github.com/OpenAdaptAI/OpenAdapt/commit/269281181fcfed5bfb8f1021a3213f6b891379e8))

* wip ([`222b294`](https://github.com/OpenAdaptAI/OpenAdapt/commit/222b29444b62c984087a5d5684899ba089f78a36))

* remove test changes ([`e8eab59`](https://github.com/OpenAdaptAI/OpenAdapt/commit/e8eab593cfc779f858fa5aabc0d8ce879bc1babd))

* remove unused import ([`6677df3`](https://github.com/OpenAdaptAI/OpenAdapt/commit/6677df3202aa2bbecb52815c59c4c8f8d16e6f0f))

* fix ([`98abc16`](https://github.com/OpenAdaptAI/OpenAdapt/commit/98abc16f2a4917a2ec9fb2c95fb7093048362926))

* ForceFloat ([`97e0838`](https://github.com/OpenAdaptAI/OpenAdapt/commit/97e0838610e3efc4fe874fda52aef4803381c0a6))

* wip ([`cc1dfea`](https://github.com/OpenAdaptAI/OpenAdapt/commit/cc1dfeadcb2071aaf738bdc22d16ceef29571562))

* added basic temporary test ([`8634267`](https://github.com/OpenAdaptAI/OpenAdapt/commit/8634267badddc152b73206f91410c0f85fef6c70))

* config.RECORD_READ_ACTIVE_ELEMENT_STATE, .REPLAY_STRIP_ELEMENT_STATE ([`15f7c31`](https://github.com/OpenAdaptAI/OpenAdapt/commit/15f7c317ffb04602c6c3e944e84b2d6edd5b002d))

* merged changes ([`579feea`](https://github.com/OpenAdaptAI/OpenAdapt/commit/579feea567cf8a21fc63e923266a8ef007816ca0))

* replace other func with get_screenshot_bbox ([`0cbeb7e`](https://github.com/OpenAdaptAI/OpenAdapt/commit/0cbeb7e98129efd1c4401b0b891296dada0413cc))

* Merge branch &#39;feat/sam_mixin&#39; of https://github.com/jesicasusanto/PAT into feat/sam_mixin ([`27f65e7`](https://github.com/OpenAdaptAI/OpenAdapt/commit/27f65e7fa55a8788b1fbbee4d2d6ee6a7e93048a))

* fix sam_mixin.py ([`bb707a6`](https://github.com/OpenAdaptAI/OpenAdapt/commit/bb707a6089d06a3be352b9deadc8db0ea1364991))

* Update util.py ([`1551c56`](https://github.com/OpenAdaptAI/OpenAdapt/commit/1551c56681876b6cd0e77396caacc54e0fc29df5))

* Update util.py ([`6237304`](https://github.com/OpenAdaptAI/OpenAdapt/commit/6237304b99a433c0443ec24add5a920cc51ae5df))

* Merge branch &#39;MLDSAI:main&#39; into feat/sam_mixin ([`413f0c9`](https://github.com/OpenAdaptAI/OpenAdapt/commit/413f0c935fec40df7b0a8298fced2ce401a365d8))

* Merge pull request #159 from dianzrong/touchpad

Updated README to note trackpad/touchpad limitations ([`fe8be21`](https://github.com/OpenAdaptAI/OpenAdapt/commit/fe8be21ea2da6898ca6d051762e47f59b2a3bb56))

* add sam_Mixin ([`8f2d67e`](https://github.com/OpenAdaptAI/OpenAdapt/commit/8f2d67e340c40f66e1b8bc29d49bee7a51375806))

* create sam_mixin ([`63a569f`](https://github.com/OpenAdaptAI/OpenAdapt/commit/63a569f494c6db97cf3a589f8c9a78e8f1d04629))

* modularization + fixes ([`f9b7029`](https://github.com/OpenAdaptAI/OpenAdapt/commit/f9b70298497a0e54480cef494f37e504348d588a))

* Add Github Action info

Added the exact linter used in the GitHub Action ([`23ca868`](https://github.com/OpenAdaptAI/OpenAdapt/commit/23ca868a89201d8654aae4b53c24a1ce43c34a77))

* implemented pyinstaller ([`fa43c57`](https://github.com/OpenAdaptAI/OpenAdapt/commit/fa43c57b088c571da047bb85ba151d35a2e167fc))

* added file picker for import

todo: work on export ([`03b5295`](https://github.com/OpenAdaptAI/OpenAdapt/commit/03b5295588cca194bec1a8750c03ab5439b8822b))

* Merge remote-tracking branch &#39;upstream/main&#39; into add-GUI ([`062cde9`](https://github.com/OpenAdaptAI/OpenAdapt/commit/062cde9d71cc94405373aafb8acc59222f9dada2))

* Create CONTRIBUTING.md

Rough draft ([`20abe2a`](https://github.com/OpenAdaptAI/OpenAdapt/commit/20abe2a848face4dad010cb9e2888941617c2ae9))

* Add instructions for troubleshooting ([`cf1bee4`](https://github.com/OpenAdaptAI/OpenAdapt/commit/cf1bee48e932df0b4281be0c3497cbc532a2dfaf))

* Improve description ([`1346f09`](https://github.com/OpenAdaptAI/OpenAdapt/commit/1346f0982327f78e4f4748d6bfb6916d01d58743))

* Add information on how to set up permissions on macOS ([`a7c0ad9`](https://github.com/OpenAdaptAI/OpenAdapt/commit/a7c0ad9dd8dd20e27cdc6773012800b915ad0a3a))

* Ignore packages in editable mode ([`e0df478`](https://github.com/OpenAdaptAI/OpenAdapt/commit/e0df4782045d822c779defb31e8a678f1006f04a))

* Add missing dependencies and import pynput in editable mode ([`907b6de`](https://github.com/OpenAdaptAI/OpenAdapt/commit/907b6de7145c9b69b1f1333a74e7f0cda3442d15))

* deprecate old ui, add import/export recording ([`c8ad9fe`](https://github.com/OpenAdaptAI/OpenAdapt/commit/c8ad9fe31b91ea483719ac23e687ecbb7c686847))

* Reformatted limitations in the README and added that most touchpad and trackpad gestures aren&#39;t supported ([`88aecca`](https://github.com/OpenAdaptAI/OpenAdapt/commit/88aecca63742f7baaecbd1dd149155d8709bd6ad))

* working StatefulReplayStrategy ([`fc8cb2a`](https://github.com/OpenAdaptAI/OpenAdapt/commit/fc8cb2a1c83f42db60a0e849f3ce7f5085983442))

* share button

could use it for exporting? or just sharing openadapt ([`a95661f`](https://github.com/OpenAdaptAI/OpenAdapt/commit/a95661fd72aa95b4032f23cd292d865c0c0b9193))

* align dialog buttons ([`2bee6dd`](https://github.com/OpenAdaptAI/OpenAdapt/commit/2bee6dd77d8367ee895096b45107f330f576ca62))

* wip ([`163308b`](https://github.com/OpenAdaptAI/OpenAdapt/commit/163308b584686816c9344b84ce35fd21ac4a816c))

* NiceGUI WIP

code needs a little bit of clean up but is functional ([`359fbaf`](https://github.com/OpenAdaptAI/OpenAdapt/commit/359fbaf371db2290e143415c5f47bf82627702e5))

* wip ([`f92f362`](https://github.com/OpenAdaptAI/OpenAdapt/commit/f92f362e2fa4e64e45c8adad538aa7981d67a95d))

* wip ([`c1c7615`](https://github.com/OpenAdaptAI/OpenAdapt/commit/c1c7615648b5b055e06def5382840dcfa873376b))

* wip ([`2542329`](https://github.com/OpenAdaptAI/OpenAdapt/commit/2542329cb66f0e6c03a145b1ccbaa1407637776d))

* Merge pull request #126 from 0dm/#109

#109 renamed all instances of puterbot to openadapt ([`754b854`](https://github.com/OpenAdaptAI/OpenAdapt/commit/754b8541389c05dab2a2aa0bc99f9a8e0da0ecf4))

* MAX_TABLE_CHILDREN ([`b8481ee`](https://github.com/OpenAdaptAI/OpenAdapt/commit/b8481ee018e5bb9c7049b7d80801964b7c3c7986))

* save window state (mac) ([`99dc235`](https://github.com/OpenAdaptAI/OpenAdapt/commit/99dc2355073ad5db3d20a86d3fc5ba7febddaa86))

* Merge branch &#39;MLDSAI:main&#39; into expand-on-demo-replay-strategy ([`1f09c07`](https://github.com/OpenAdaptAI/OpenAdapt/commit/1f09c0758590562aaa2b38a9cb47c245623b1ecb))

* Fixed initializer ([`13a930b`](https://github.com/OpenAdaptAI/OpenAdapt/commit/13a930b51578998688143ca37c52cc6ceb0a2d6a))

* small error with initializer ([`fbc2486`](https://github.com/OpenAdaptAI/OpenAdapt/commit/fbc24861037fbe44e20e23e66654bddde650d35a))

* Merge branch &#39;#109&#39; into add-GUI ([`09d1303`](https://github.com/OpenAdaptAI/OpenAdapt/commit/09d1303d52338b280e37457ba6aa75a76f73c44b))

* resolved merge conflict ([`8508583`](https://github.com/OpenAdaptAI/OpenAdapt/commit/850858375ea3195d7d8136d75c4aac38af6ce597))

* Merge remote-tracking branch &#39;upstream/main&#39; into #109 ([`3bd935c`](https://github.com/OpenAdaptAI/OpenAdapt/commit/3bd935c65384f778db27a531c0315a3e1699214d))

* Revert &#34;Merge branch &#39;main&#39; into #109&#34;

This reverts commit cb5373b6238b91c4576aca04228f3c416c1f5e6e, reversing
changes made to 796e35a2ccf7c12df3b20a5903355ab4d07ab68a. ([`605e3fb`](https://github.com/OpenAdaptAI/OpenAdapt/commit/605e3fbd66147e557222ded08d124475cc45890c))

* Merge branch &#39;main&#39; into #109 ([`cb5373b`](https://github.com/OpenAdaptAI/OpenAdapt/commit/cb5373b6238b91c4576aca04228f3c416c1f5e6e))

* Created tests for the helper functions in summary_mixin.py ([`3895474`](https://github.com/OpenAdaptAI/OpenAdapt/commit/38954743171f5fa68f125451f69bd1fe01ee2a66))

* Moved SummaryReplayStrategyMixin up ([`8aab461`](https://github.com/OpenAdaptAI/OpenAdapt/commit/8aab4615d436da1cc61b536afcc265e8793e44bd))

* Formatting changes and changed ascii text to only contain words ([`becb3a8`](https://github.com/OpenAdaptAI/OpenAdapt/commit/becb3a8ba1916d54cb128d9e9f5d246da4cf862d))

* Additional formatting changes ([`e0ddbe8`](https://github.com/OpenAdaptAI/OpenAdapt/commit/e0ddbe847e4652fff19b628a91f894d0d0b1bf2e))

* Used black and the Google Python style guide to imrpove formatting. ([`2dcf6cd`](https://github.com/OpenAdaptAI/OpenAdapt/commit/2dcf6cdccb1e2d98f3fffdf3e477e7b4c41c6f79))

* Removed finding the last screenshot ([`a0ead1f`](https://github.com/OpenAdaptAI/OpenAdapt/commit/a0ead1f383f9ff061ea4d02ea61be650ec507d02))

* Merge pull request #119 from 0dm/#103

#103 : Rename InputEvent(s) to ActionEvent(s) ([`27efb0a`](https://github.com/OpenAdaptAI/OpenAdapt/commit/27efb0af5d304caf67ca19acb3ea45c5078c2742))

* Update app.py

added logo button ([`8220fe4`](https://github.com/OpenAdaptAI/OpenAdapt/commit/8220fe41696d576c652d8fd5de096e4fee01c125))

* Add Functioning GUI

requires tkinter ([`c2db59d`](https://github.com/OpenAdaptAI/OpenAdapt/commit/c2db59d868bfff0e1b489c59a9e201f34b3abf9f))

* #103 : Refactor InputEvent =&gt; ActionEvent ([`7e537d4`](https://github.com/OpenAdaptAI/OpenAdapt/commit/7e537d47588ee897d7162a46a5b077af10fb1b8b))

* Revert &#34;resolved #103&#34;

This reverts commit b7c99373fb35e54f7e2efdafa09baa5aa818992c. ([`659b9d7`](https://github.com/OpenAdaptAI/OpenAdapt/commit/659b9d7201afe5aa4bd4561523629da3da9c3617))

* Add newline ([`0844f9c`](https://github.com/OpenAdaptAI/OpenAdapt/commit/0844f9c7960adadfc03a6c9571aec2b4a4b9b997))

* Merge remote-tracking branch &#39;upstream/main&#39; into #109 ([`796e35a`](https://github.com/OpenAdaptAI/OpenAdapt/commit/796e35a2ccf7c12df3b20a5903355ab4d07ab68a))

* Use _insert for writing perf stats to db ([`1564d56`](https://github.com/OpenAdaptAI/OpenAdapt/commit/1564d56bdd0b30d7e2d8351fd6604f7af927dd1a))

* Merge pull request #129 from MLDSAI/fix/prompt_truncation

fix prompt truncation; fix diff_mask ([`0ab8213`](https://github.com/OpenAdaptAI/OpenAdapt/commit/0ab8213e511127415045460f881cf691404c0e49))

* fix prompt truncation; fix diff_mask ([`dd045ef`](https://github.com/OpenAdaptAI/OpenAdapt/commit/dd045ef74f0159e1b2fe8c12c8c7b8425e2c3866))

* #109 renamed all instances of puterbot to openadapt ([`0e4e0ca`](https://github.com/OpenAdaptAI/OpenAdapt/commit/0e4e0caae143e5eeafdfce419af27f5867126d13))

* Revert performance plot to off by default ([`c1cb8de`](https://github.com/OpenAdaptAI/OpenAdapt/commit/c1cb8dedd7624ef1bd1df833617d4a52ba855fe4))

* Save event performances data. Closes #85 Fixes #93 ([`8ced5bd`](https://github.com/OpenAdaptAI/OpenAdapt/commit/8ced5bd6db4ff78945801f8e9391e56b0af02947))

* Merge pull request #121 from MLDSAI/abrichr-patch-1

Update README.md ([`4f22e89`](https://github.com/OpenAdaptAI/OpenAdapt/commit/4f22e890179583eaa7c0fdb9dd635d30b712700d))

* Update README.md ([`447969d`](https://github.com/OpenAdaptAI/OpenAdapt/commit/447969d056807da4985c3e33e1462aac864b1d7f))

* resolved #103

🔨 ([`b7c9937`](https://github.com/OpenAdaptAI/OpenAdapt/commit/b7c99373fb35e54f7e2efdafa09baa5aa818992c))

* Merge pull request #115 from MLDSAI/abrichr-patch-1

Update README.md ([`3f310d0`](https://github.com/OpenAdaptAI/OpenAdapt/commit/3f310d0b072d020c54336fef06e4341abe634c16))

* Update README.md ([`089295e`](https://github.com/OpenAdaptAI/OpenAdapt/commit/089295e296ce0e2a0d947554681433bca2037b97))

* Merge pull request #110 from MLDSAI/abrichr-patch-1

Update README.md ([`2aa96d2`](https://github.com/OpenAdaptAI/OpenAdapt/commit/2aa96d269f59986f7f94aeabbd06c2b0ff23b2c7))

* Update README.md ([`1566cdf`](https://github.com/OpenAdaptAI/OpenAdapt/commit/1566cdf79e810f984fe3d49c23a5cfa1219c8e1c))

* Merge remote-tracking branch &#39;upstream/main&#39; ([`b49d1cd`](https://github.com/OpenAdaptAI/OpenAdapt/commit/b49d1cd940efcf88b95a4db676649c47054060ee))

* Merge pull request #107 from MLDSAI/abrichr-patch-1

Update README.md ([`e944074`](https://github.com/OpenAdaptAI/OpenAdapt/commit/e944074b3a0a5751592d2a6919c48776b10b245b))

* Update README.md ([`e0ed5fa`](https://github.com/OpenAdaptAI/OpenAdapt/commit/e0ed5fa755d3f7eec6219cda9f08e7a79f75d2db))

* Merge pull request #106 from MLDSAI/abrichr-patch-1

Update README.md ([`9eb420d`](https://github.com/OpenAdaptAI/OpenAdapt/commit/9eb420dc4865203b8de5efc0c8e170dc5a83b6ad))

* Update README.md ([`793ef07`](https://github.com/OpenAdaptAI/OpenAdapt/commit/793ef07a3c18aec6704c9ffcaf0ba2ccf1f36e1e))

* Merge pull request #101 from MLDSAI/abrichr-patch-1

Update README.md ([`02b718c`](https://github.com/OpenAdaptAI/OpenAdapt/commit/02b718c9590d4fc1e88874e12b312096667f57c8))

* Update README.md ([`3f1e00f`](https://github.com/OpenAdaptAI/OpenAdapt/commit/3f1e00f8d76187d1e2906284151e951e1c2ea40a))

* Update demo.py ([`67396e3`](https://github.com/OpenAdaptAI/OpenAdapt/commit/67396e3306eb4f57581339c098fe580f46d0a0fd))

* wip fastapi

fastapi on localhost ([`cdaee2a`](https://github.com/OpenAdaptAI/OpenAdapt/commit/cdaee2a01dfe0325a246d17b2bf9f3d90a1d487d))

* Update README.md ([`29419e1`](https://github.com/OpenAdaptAI/OpenAdapt/commit/29419e15aa9488f270ab1b7a18272bc805c84a78))

* Integrated SummaryReplayStrategyMixin into the DemoReplayStrategy ([`e6c67fb`](https://github.com/OpenAdaptAI/OpenAdapt/commit/e6c67fb243512be5e8666f1f1e3463c65e93239b))

* Refactored into a into a SummaryReplayStrategyMixin. Am open to changing the function parameters of get_summary and what attributes SummaryReplayStrategyMixin has ([`2116cf6`](https://github.com/OpenAdaptAI/OpenAdapt/commit/2116cf6c93fccd285e7d5716acb1bc3184a3a151))

* Small type change ([`fad5430`](https://github.com/OpenAdaptAI/OpenAdapt/commit/fad5430322d33b283223b4f5a872fa316b348f29))

* Removed print statements ([`b897ac8`](https://github.com/OpenAdaptAI/OpenAdapt/commit/b897ac831c226023f1adc7d7cff57aa3f3a51d49))

* Added the needed libraries for DemoReplayStrategy ([`9c570fd`](https://github.com/OpenAdaptAI/OpenAdapt/commit/9c570fd4e6627fa96d6cf8e59edcf690d9019728))

* Changed the method in which the OCR and ASCII texts are summarized and compared. ([`0a53330`](https://github.com/OpenAdaptAI/OpenAdapt/commit/0a53330e8fe45fe7ae85cf00419c1924eed2b771))

* Changed the way window_changed was added ([`626c209`](https://github.com/OpenAdaptAI/OpenAdapt/commit/626c2099cfac8550aabf3500b842348f92952cf9))

* Fixed error ([`d4968b6`](https://github.com/OpenAdaptAI/OpenAdapt/commit/d4968b61fe63dd4a4fafcd831544081343744a22))

* Merge remote-tracking branch &#39;origin/main&#39; ([`33ee2d5`](https://github.com/OpenAdaptAI/OpenAdapt/commit/33ee2d5b5b34aca28f23ec08e48e30b4e2d71ff7))

* Used ASCII to check the summarization of the OCR text ([`f34cec2`](https://github.com/OpenAdaptAI/OpenAdapt/commit/f34cec23172ce52ad03ac33559a1f3722add6cc3))

* Merge branch &#39;MLDSAI:main&#39; into main ([`daaf679`](https://github.com/OpenAdaptAI/OpenAdapt/commit/daaf6797dcb7a53354f4096431d4e122c834d88f))

* Used the OCR text to note in the prompt to add a note at the front of the string to indicate whether the screen changed ([`f5e935d`](https://github.com/OpenAdaptAI/OpenAdapt/commit/f5e935d37eb719f178357159f3a77a24df5aef5e))

* Update README.md ([`17f8e64`](https://github.com/OpenAdaptAI/OpenAdapt/commit/17f8e64d53934af44b16f5eab989dc5fa6e4c991))

* Update README.md ([`d37b157`](https://github.com/OpenAdaptAI/OpenAdapt/commit/d37b157a2aa55393688fd2036af09358e8156352))

* Update README.md ([`02cfb5a`](https://github.com/OpenAdaptAI/OpenAdapt/commit/02cfb5a007a7b4241a1bcb8a01bb0caeae618564))

* Merge pull request #31 from MLDSAI/fix/sys_platform

pywin32 on windows only ([`76aa27b`](https://github.com/OpenAdaptAI/OpenAdapt/commit/76aa27b8bd4a995a254e4d2f7b647893b16abcc2))

* pywin32 on windows only ([`228a4b3`](https://github.com/OpenAdaptAI/OpenAdapt/commit/228a4b317c02c28812c215bb36785c9c3cd48ade))

* get_screenshot -&gt; take_screenshot ([`18327f0`](https://github.com/OpenAdaptAI/OpenAdapt/commit/18327f01ddd5ddb1511e2c173e9846aef5bd3676))

* Update README.md ([`8a563bb`](https://github.com/OpenAdaptAI/OpenAdapt/commit/8a563bb5951260a950dff32407edbd1ea6362d88))

* rename completion_history -&gt; result_history ([`6892cf4`](https://github.com/OpenAdaptAI/OpenAdapt/commit/6892cf4aff2d215f9e8dec42b43556f7a561d65c))

* add InputEvent.__str__; improve demo ([`549ef67`](https://github.com/OpenAdaptAI/OpenAdapt/commit/549ef6751042e585bc345b108b6e176779414813))

* Merge pull request #25 from MLDSAI/feat/ascii

add ascii_mixin.py ([`cb28427`](https://github.com/OpenAdaptAI/OpenAdapt/commit/cb28427dc694d1ad0cc99b7b4fefb7d728f0e219))

* update docstring ([`5c09b2d`](https://github.com/OpenAdaptAI/OpenAdapt/commit/5c09b2d867e77ea156fefca577c6ace9403fffc3))

* update docstring ([`6abfb4c`](https://github.com/OpenAdaptAI/OpenAdapt/commit/6abfb4cb61032c7379f6bfacc68ee2cc49ccd446))

* add ascii_mixin.py ([`c8d2006`](https://github.com/OpenAdaptAI/OpenAdapt/commit/c8d20067dffc26679588225c395fdba592efbae4))

* fix typo ([`1b84542`](https://github.com/OpenAdaptAI/OpenAdapt/commit/1b84542ffe54310aba5c81c3cd2bbf2f2bb4ebe3))

* upate README ([`af9c923`](https://github.com/OpenAdaptAI/OpenAdapt/commit/af9c923d8441d16af3b13e43c62796d5ca291493))

* fix type ([`e7ba06c`](https://github.com/OpenAdaptAI/OpenAdapt/commit/e7ba06c5df340dea7a050fbce51785a01cd83b4a))

* Merge pull request #23 from MLDSAI/refactor/screenshots

refactor screenshots ([`da13e5f`](https://github.com/OpenAdaptAI/OpenAdapt/commit/da13e5f5fbccb5d82ab77015d0a9afcbf76ccaa9))

* Screenshot.take_screenshot; fix fps ([`0657e43`](https://github.com/OpenAdaptAI/OpenAdapt/commit/0657e432048892a1b9e515c594b92916fdb38c12))

* refactor screenshots ([`f34c5f6`](https://github.com/OpenAdaptAI/OpenAdapt/commit/f34c5f67786ead7d5d8b56bc62d44957b24c3c7a))

* Merge pull request #21 from MLDSAI/feat/llm_ocr_demo

add llm_ocr_demo.py and related mixins ([`09e36f8`](https://github.com/OpenAdaptAI/OpenAdapt/commit/09e36f8ed876f26b63ab679dbe86d4248062813b))

* update README ([`61764bf`](https://github.com/OpenAdaptAI/OpenAdapt/commit/61764bf656ebc88a01e5c907f4788789dd417994))

* minor refactor ([`5c309ae`](https://github.com/OpenAdaptAI/OpenAdapt/commit/5c309ae481a8165282b46825c8125b5676d14ec9))

* add TODO ([`a899fa2`](https://github.com/OpenAdaptAI/OpenAdapt/commit/a899fa24cbaff33cbf610a0110d841e94d1b5104))

* improve log_fps ([`4afa820`](https://github.com/OpenAdaptAI/OpenAdapt/commit/4afa820a9f99ae4d15d3ad70c231bbbb475c7769))

* log_fps ([`0e058f6`](https://github.com/OpenAdaptAI/OpenAdapt/commit/0e058f6ed3ab907d49aea2ac35b90be411a759b3))

* docs; types ([`b4b51da`](https://github.com/OpenAdaptAI/OpenAdapt/commit/b4b51da89f910d1d7170cc146eda502d9ff2f0a1))

* update docstring ([`fc9de32`](https://github.com/OpenAdaptAI/OpenAdapt/commit/fc9de3227808b4e51e33515991ec8c289dbe82ab))

* add llm_mixin.py; ocr_mixin.py; llm_ocr_demo.py ([`12f0ff0`](https://github.com/OpenAdaptAI/OpenAdapt/commit/12f0ff0e8a5bb51137ac531116c8aeb8b2873e64))

* Merge pull request #20 from MLDSAI/fix/round_timestamps

add round_timestamps ([`601208c`](https://github.com/OpenAdaptAI/OpenAdapt/commit/601208cd4948984130a63948af8acff6d45792cf))

* NUM_TIMESTAMP_DIGITS=6 ([`74b2410`](https://github.com/OpenAdaptAI/OpenAdapt/commit/74b2410a29ccd146988a546948df6fa53e49d4ea))

* read/write properties instead of globals in NaiveReplayStrategy ([`5828baa`](https://github.com/OpenAdaptAI/OpenAdapt/commit/5828baabe9643eccb5f1bf1c1f8178d0b06d953d))

* add round_timestamps ([`2e2e2a7`](https://github.com/OpenAdaptAI/OpenAdapt/commit/2e2e2a74e559c166a3b4eb595d8fb8c20a07167a))

* Update README ([`fd7e825`](https://github.com/OpenAdaptAI/OpenAdapt/commit/fd7e825a820e54ab09d42ae5d9be2acc2576d3ea))

* update visualize.png ([`d486d6f`](https://github.com/OpenAdaptAI/OpenAdapt/commit/d486d6f9f609dd2093865b073f655b0a831d6a43))

* update README ([`eaa76a7`](https://github.com/OpenAdaptAI/OpenAdapt/commit/eaa76a74fd9d0c3df41c6ec1b29df0fa11f1ccd3))

* Merge pull request #18 from MLDSAI/feat/replay_strategies

Add puterbot/strategies ([`a75665b`](https://github.com/OpenAdaptAI/OpenAdapt/commit/a75665b643346df6a3d0f98b931cb8d0894e7e75))

* Merge branch &#39;main&#39; into feat/replay_strategies ([`76097e4`](https://github.com/OpenAdaptAI/OpenAdapt/commit/76097e4dd8fcf12698b1b06d5688053410343da3))

* add missing pywin32 ([`91942a3`](https://github.com/OpenAdaptAI/OpenAdapt/commit/91942a33dcf7521008cff5aef5ac60a46d41392c))

* update README ([`44cafc4`](https://github.com/OpenAdaptAI/OpenAdapt/commit/44cafc46e82990035e8c879f5293219a1ee8fb5d))

* Update README and docs ([`7969e98`](https://github.com/OpenAdaptAI/OpenAdapt/commit/7969e985c83a43780807a71d6d845049dcae0e18))

* add assets/visualize.png; add back_populates; update README ([`4461031`](https://github.com/OpenAdaptAI/OpenAdapt/commit/446103128fe9ce4a7ef92b7e5c5bf7906c902a7d))

* Add puterbot/strategies ([`a7ce3e8`](https://github.com/OpenAdaptAI/OpenAdapt/commit/a7ce3e8f351f59b6a3ba18d4b212a92175dca4ad))

* Update README.md ([`f8f3438`](https://github.com/OpenAdaptAI/OpenAdapt/commit/f8f3438b223aef8377e4da02e456c82e010d1199))

* Update README.md ([`6ec32b6`](https://github.com/OpenAdaptAI/OpenAdapt/commit/6ec32b65b419f2a99c456b27aa9497d53a7e4163))

* Merge pull request #16 from MLDSAI/feat/task_description

add Recording.task_description; recording.py types and documentation ([`0325fc3`](https://github.com/OpenAdaptAI/OpenAdapt/commit/0325fc347f55cb50499fbb91cb5d3e200f721acd))

* add Recording.task_description; recording.py types and documentation ([`0299a05`](https://github.com/OpenAdaptAI/OpenAdapt/commit/0299a05e3a7fb86bac2ace1ad2dd98b6411aafe9))

* Update README.md ([`93bfdf9`](https://github.com/OpenAdaptAI/OpenAdapt/commit/93bfdf9773c3566ce4fed7545201dcb9ee4c3107))

* Update README.md ([`b69a3b7`](https://github.com/OpenAdaptAI/OpenAdapt/commit/b69a3b7f0515fcc618804301e78c0c7db1e68ef4))

* Merge pull request #10 from MLDSAI/ag23

Update README ([`91c57f0`](https://github.com/OpenAdaptAI/OpenAdapt/commit/91c57f0d6647592284122c1f6832b4704843bf11))

* update README ([`4c745fa`](https://github.com/OpenAdaptAI/OpenAdapt/commit/4c745fa4d1469bb63b7f5b47e303b9b908c748d5))

* Update README ([`ca09082`](https://github.com/OpenAdaptAI/OpenAdapt/commit/ca09082a16008c602cf8a4190f35f4345e86f910))

* Update README ([`971dd36`](https://github.com/OpenAdaptAI/OpenAdapt/commit/971dd3619a62317998ac4e63d5f3da4433923db5))

* update README.md ([`426930f`](https://github.com/OpenAdaptAI/OpenAdapt/commit/426930f640c3fd1ca5900c54b519292287940c64))

* Update README.md ([`6f80aa0`](https://github.com/OpenAdaptAI/OpenAdapt/commit/6f80aa0bc83b04682143001eee8278a2daf8a614))

* update README.md ([`a9ed8e8`](https://github.com/OpenAdaptAI/OpenAdapt/commit/a9ed8e85b26ce6e47af29fa6634accdf3f6601a8))

* update README.md ([`3305fd1`](https://github.com/OpenAdaptAI/OpenAdapt/commit/3305fd161e263ff13478ec601355d109fdac0a71))

* remove legacy build instructions ([`c097825`](https://github.com/OpenAdaptAI/OpenAdapt/commit/c09782525a9e0303a4d3a2996ef2cf011081caca))

* update README.md ([`03c0964`](https://github.com/OpenAdaptAI/OpenAdapt/commit/03c09645a75326d86607486eef13faa023475200))

* Update README.md ([`fe2f22e`](https://github.com/OpenAdaptAI/OpenAdapt/commit/fe2f22ec64204dde16aadcad680501bc5dcdb14b))

* Update README.md ([`c674839`](https://github.com/OpenAdaptAI/OpenAdapt/commit/c6748393aff703ac0b3d7b5fa450f057c6a9f9d9))

* add test_events.py ([`c276e7b`](https://github.com/OpenAdaptAI/OpenAdapt/commit/c276e7bd0850e04254a56f86dfeef138cb15c2df))

* add alembic version ([`3e531fc`](https://github.com/OpenAdaptAI/OpenAdapt/commit/3e531fc68f67b32b26e74b76993fb88aab5ad934))

* add alembic LICENCE requirements.txt setup.py; fix README ([`b7e7b58`](https://github.com/OpenAdaptAI/OpenAdapt/commit/b7e7b581b0582c6bea8c073182efd07b2588a001))

* improve README ([`fb85635`](https://github.com/OpenAdaptAI/OpenAdapt/commit/fb856353cfbc6b4711829f20a986385871a8c059))

* Add Submitting an Issue section to README.md ([`6372a76`](https://github.com/OpenAdaptAI/OpenAdapt/commit/6372a76080b6ba949d657022368c0e1b5f0d4019))

* update puterbot/README.md ([`46a7621`](https://github.com/OpenAdaptAI/OpenAdapt/commit/46a76219dbde2672a87ab51790810f2086dc48fe))

* Update README.md ([`a57c92f`](https://github.com/OpenAdaptAI/OpenAdapt/commit/a57c92fd15f964ef17986dfa040f171459ab0354))

* Update README.md ([`9a9bf17`](https://github.com/OpenAdaptAI/OpenAdapt/commit/9a9bf17747a02842ecf29dc96e75498bfd4e2d0c))

* add puterbot/, .gitignore ([`22774ce`](https://github.com/OpenAdaptAI/OpenAdapt/commit/22774cef0d8b3f10ac07c98b402cfe6c03c6bbdb))

* add README.md ([`cd67c61`](https://github.com/OpenAdaptAI/OpenAdapt/commit/cd67c61e4b961d350d5385d996c285bea3851c8f))
