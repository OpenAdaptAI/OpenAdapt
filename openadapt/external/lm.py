import datetime
import os

LIMIT = 10

folders = ["Documents", "Desktop", "Downloads"]
ignore_patterns = [".DS_Store", ".git", ".vscode", "__pycache__", "node_modules"]

files = []

for folder in folders:
    for root, dirs, files_in_folder in os.walk(os.path.expanduser(f"~/{folder}")):
        dirs[:] = [
            d for d in dirs if not d.startswith(".") and d not in ignore_patterns
        ]
        for file in files_in_folder:
            if file in ignore_patterns:
                continue
            path = os.path.join(root, file)
            if os.path.islink(path):
                continue
            accessed_time = datetime.datetime.fromtimestamp(os.path.getatime(path))
            files.append((path, accessed_time))

files.sort(key=lambda x: x[1])

for path, accessed_time in files[-LIMIT:]:
    print(f"{accessed_time}: {path}")
print(len(files))
