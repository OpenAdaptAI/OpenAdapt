"""
Script for generating Git commit messages using LLMs.

Usage:

    $ python -m openadapt.contrib.git_message [path/to/repo]  # default to current directory
"""

import fire
import docker

system_message = """
You are a git commit message generator.
You are shown the (possibly truncated) output of git commands like `git status` and `git diff -p`.
Your goal is to a meaningful and concise git commit message that can be copied and pasted into a terminal.
Your completions must be formatted precisely in one of three ways:

1. `<<<COMMAND>>> git <git subcommand>`:
Run git commands (and optionally process the output using other shell commands).
These will be run against the repo being committed to, and you will be provided with the result in the next prompt.

2. `<<<FRAGMENT>>> <git commit message fragment>`:
Generate a fragment of the final git commit message representing the full diff (if the diff has been truncated).
I will include all of your fragments in my replies.

3. `<<<MESSAGE>>> <git commit message>`:
Generate a meaningful git commit message once you have seen the whole diff
(i.e. the prompt does not end with "-----num_chars_removed={num_chars_removed}-----").
This will be the end of the session.

Do not reply in any other format. All of my prompts will be formatted as:
``
<<<FRAGMENTS>>>
[<fragment_0>]

[<fragment_1>]

[...]
<<<COMMAND>>>
<initial command | COMMAND completion>
<<<COMMAND_RESULT>>>
<output from COMMAND>
``
"""
command = "git status"

def generate_git_commit_message(repo_path="."):

    # pip install docker
    client = docker.from_env()
    container = client.containers.run("ubuntu", detach=True)
    fragments = []
    commit_message = None
    while True:
        assert command.startswith("git"), command
        # MUST be run inside Docker to avoid shell injection
        command_result = container.exec_run(command)

        # this should generate a prompt in the format described in the system_message,
        # and (if necessary) truncate it and append the number of characters truncated:
        # "-----num_chars_removed={num_chars_removed}-----"
        # (such that the result fits into the context window)
        prompt = truncate_and_append_num_chars_removed(fragments, command, command_result)

        completion = get_completion(system_message, prompt)  # do not track message history
        completion_parts = completion.split(" ")
        completion_type = completion_parts[0]
        completion_content = completion_parts[1:]
        if completion_type == "COMMAND":
            command = completion_content
        elif completion_type == "FRAGMENT":
            fragment = completion_content
            fragments.append(fragment)
        elif completion_type == "MESSAGE":
            commit_message = completion_content
            break
    print(commit_message)



if __name__ == "__main__":
    fire.Fire(generate_git_commit_message)