import subprocess

result = subprocess.run(["git", "status"], capture_output=True, text=True)

if "branch is up to date" not in result.stdout:
    subprocess.run(["git", "stash"])
    subprocess.run(["echo", "changes", "stashed"])
    subprocess.run(["git", "pull"])
    subprocess.run(["echo", "updated", "OpenAdapt"])

subprocess.run(["python3", "-m", "app.main"], capture_output=True, text=True)
