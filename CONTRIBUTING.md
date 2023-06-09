# Contributing to Project

Welcome to OpenAdapt! We appreciate your interest in contributing. Before getting started, please take a moment to review the guidelines below.

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

The scope specifies the location of the commit change. For example: `$location`, `$browser`, `$compile`, `$rootScope`, `ngHref`, `ngClick`, `ngView`, etc. Use `*` when the change affects more than a single scope.

### Subject

The subject should be a succinct description of the change. Please follow these guidelines:
- Use the imperative, present tense (e.g., "change" not "changed" or "changes")
- Do not capitalize the first letter
- Do not end with a dot (.)

## Body

The body should provide the motivation for the change and contrast it with the previous behavior. Use the imperative, present tense (e.g., "change" not "changed" or "changes").

The body or footer can begin with **BREAKING CHANGE:** followed by a short description to create a major release. For example, a major release would increment the version number from `0.1.0` to `1.1.0`.

## Version Bump

We use semantic versioning for our releases. The version number is automatically determined based on the commit types mentioned above. Here's how the version numbers are incremented:

- **MAJOR** version for incompatible changes or major features
  - Increments the major version number (e.g., 1.0.0 -> 2.0.0)

- **MINOR** version for new features or backward-compatible changes
  - Increments the minor version number (e.g., 1.0.0 -> 1.1.0)

- **PATCH** version for backward-compatible bug fixes or small changes
  - Increments the patch version number (e.g., 1.0.0 -> 1.0.1)

Thank you for your contributions to the Project!