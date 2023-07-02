# How to contribute

We would love to implement your contributions to this project! We simply ask that you observe the following guidelines.  

## Code Style

This project follows the [Google Python Style Guide](https://google.github.io/styleguide/pyguide.html),
with the following modifications:
- imports are ordered first by group (in order of built-in, external, local), then lexicographically
- import groups are separated by newlines

For example:

```
# built-in
from functools import partial
from pprint import pformat
import json

# external
from loguru import logger
import numpy as np

# local
from openadapt import models
```
## Creating Issues
In order to effectively communicate any bugs or request new features, please select the appropriate form. If none of the options suit your needs, you can click on "Open a blank issue" located at the bottom.

## Testing
[GitHub Actions](https://github.com/MLDSAI/OpenAdapt/actions/new) are automatically run on each pull request to ensure consistent behaviour and style. The Actions are composed of PyTest, [black](https://github.com/psf/black) and [flake8](https://flake8.pycqa.org/en/latest/user/index.html).

You can run these tests on your own computer by downloading the depencencies in requirements.txt and then running pytest in the root directory. 

## Pull Request Format

To speed up the review process, please use the provided pull request template and create a draft pull request to get initial feedback. 

The pull request template includes areas to explain the changes, and a checklist with boxes for code style, testing, and documenttation.

## Commit Message Format

Each commit message consists of a **header**, a **body**, and a **footer**. The header has a special format that includes a **type**, a **scope**, and a **subject**:

```
<type>(<scope>): <subject>
<BLANK LINE>
<body>
<BLANK LINE>
<footer>
```

The **header** is mandatory, and the **scope** of the header is optional. The commit message lines should not exceed 100 characters in length.

### Type

The type must be one of the following:

- **feat**: A new feature
  - Increments the minor version number (e.g., 1.0.0 -> 1.1.0)

- **fix**: A bug fix
  - Increments the patch version number (e.g., 1.0.0 -> 1.0.1)

- **docs**: Documentation-only changes
  - Does not affect the version number

- **style**: Changes that do not affect the code's meaning (e.g., white-space, formatting)
  - Does not affect the version number

- **refactor**: A code change that neither fixes a bug nor adds a feature
  - Does not affect the version number

- **perf**: A code change that improves performance
  - Does not affect the version number

- **test**: Adding missing or correcting existing tests
  - Does not affect the version number

- **chore**: Changes to the build process, auxiliary tools, or libraries
  - Does not affect the version number

### Scope

The scope specifies the location of the commit change. For example: `db`, `crud`, `strategies`, `utils`, etc. Use `*` when the change affects more than a single scope.

### Subject

The subject should be a succinct description of the change. Please follow these guidelines:
- Use the imperative, present tense (e.g., "change" not "changed" or "changes")
- Do not capitalize the first letter
- Do not end with a dot (.)

### Body

The body should provide the motivation for the change and contrast it with the previous behavior. Use the imperative, present tense (e.g., "change" not "changed" or "changes").

The body or footer can begin with **BREAKING CHANGE:** followed by a short description to create a major release. For example, a major release would increment the version number from `0.1.0` to `1.1.0`.

## Submitting Changes

1. Fork the current repository
2. Make a branch to work on, or use the main branch
3. Push desired changes onto the branch in step 2
4. Submit a pull request with the branch in step 2 as the head ref and the MLDSAI/OpenAdapt main as the base ref
     - Note: may need to click "compare across forks"
5. Update pull request by resolving merge conflicts and responding to feedback
     - this step may not be necessary, or may need to be repeated
     - see instructions on how to resolve poetry.lock and pyproject.toml conflicts below
6. Celebrate contributing to OpenAdapt!

### How to resolve poetry.lock and pyproject.toml conflicts:
How to fix conflicts with poetry.lock and poetry.toml:
1. Edit the poetry.toml file to include the correct dependencies
2. Run ```poetry lock``` to update the poetry.lock file
3. Run ```poetry install``` to ensure the dependencies are installed
4. Commit updated poetry.lock and poetry.toml files
