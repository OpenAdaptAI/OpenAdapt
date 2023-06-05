import subprocess

from openadapt.app.main import run_app

result = subprocess.run(["git", "status"], capture_output=True, text=True)

if "branch is up to date" not in result.stdout:
    
    if "Changes to be committed:" or \
        "Changes not staged for commit:" in result.stdout:
        subprocess.run(["git", "stash"])
        print("Changes stashed")
    
    subprocess.run(["git", "pull"])
    print("Updated the OpenAdapt App")

run_app() # start gui
