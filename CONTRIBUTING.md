# How to contribute

We would love to implement your contributions to this project! We simply ask that you observe the following guidelines.  

## Code Style

This project follows the [Google Python Style Guide](https://google.github.io/styleguide/pyguide.html), and for imports, we like to put put external imports in alphabetical order first, a new line, and then local imports.

## Creating Issues
In order to effectively communicate any bugs or request new features, please select the appropriate form. If none of the options suit your needs, you can click on "Open a blank issue" located at the bottom.

## Testing
[GitHub Actions](https://github.com/MLDSAI/OpenAdapt/actions/new) are automatically run on each pull request to ensure consistent behaviour and style. The Actions are composed of PyTest, [black](https://github.com/psf/black) and [flake8](https://flake8.pycqa.org/en/latest/user/index.html).

You can run these tests on your own computer by downloading the depencencies in requirements.txt and then running pytest in the root directory. 

## Pull Request Format

To speed up the review process, please use the provided pull request template and create a draft pull request to get initial feedback. 

The pull request template includes areas to explain the changes, and a checklist with boxes for code style, testing, and documenttation.

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
